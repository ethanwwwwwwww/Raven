"""Symbol-level code navigation: definitions, references, and file outlines.

Why this exists: grep answers "where does this text appear", but the discipline
"fix every sibling of the defect" needs "where is this symbol DEFINED and where
is it USED" — and a textual match can neither exclude strings/comments nor
recognize `from x import y as z` alias usage. This tool layers two engines
behind one schema:

- tree-sitter (all supported languages): syntax-aware identifier matching.
  Occurrences inside strings and comments are structurally invisible, and each
  hit is classified as definition / import / call / reference by its syntax
  context. Purely syntactic — it cannot follow aliases or inheritance.
- jedi (Python only, ``action='references'``): semantic resolution that follows
  aliased imports, re-exports, and inheritance. Used when available; the
  tree-sitter listing is the backstop when jedi fails or times out.

Both engines parse the target repo's SOURCE in raven's own process — nothing is
imported or executed from the target environment, so this cannot repeat the
PYTHONPATH-pollution class of failure.

Candidate files are pre-filtered with the bundled ripgrep (see
``file_search.resolve_rg``) so only files that literally contain the symbol are
parsed; a deadline-capped walk is the rg-less fallback.
"""

import os
import re
import time
from pathlib import Path
from typing import Any

from loguru import logger

from raven.agent.tools.file_search import _IGNORE_DIRS, _denied_traversal_root, resolve_rg
from raven.agent.tools.filesystem import _FsTool
from raven.sandbox.interfaces import spill_output

# Extension -> tree-sitter-language-pack language name. Covers the SWE-bench
# Multilingual set (C/C++, Go, Java, JS/TS, PHP, Ruby, Rust) plus Python and
# common extras. Extending support = adding a row here.
_EXT_LANG = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".hxx": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".swift": "swift",
    ".lua": "lua",
}

# Leaf node types that carry an identifier-like name. Grammar vocabularies
# differ ("identifier", "type_identifier", "field_identifier", ruby "constant",
# php "name", kotlin "simple_identifier"), so match by suffix plus a small
# exact-name set rather than enumerating per language.
_IDENT_EXACT = {"name", "word", "constant", "variable_name", "simple_identifier"}

# Syntax contexts, matched against ANCESTOR node types (nearest first).
_IMPORT_RE = re.compile(r"import|use_declaration|using_directive|require|preproc_include")
_DEF_RE = re.compile(r"definition|declaration|declarator|_item$|_spec$")
# Definition-ish nodes worth listing in an outline (excludes local variable
# declarations, which would drown the skeleton in noise).
_OUTLINE_RE = re.compile(
    r"function|method|class|struct|enum|interface|trait|impl|module|namespace|_spec$|constructor"
)

_MAX_CANDIDATE_FILES = 300
_MAX_SHOWN_LINES = 200
_SCAN_DEADLINE_S = 60.0


def _is_identifier_leaf(node: Any) -> bool:
    return node.child_count == 0 and (node.type.endswith("identifier") or node.type in _IDENT_EXACT)


def _covers(field_node: Any, node: Any) -> bool:
    return field_node is not None and field_node.start_byte <= node.start_byte and node.end_byte <= field_node.end_byte


def _classify(node: Any) -> str:
    """Best-effort syntax context of one identifier occurrence.

    Walks up a few ancestors, nearest first, so a call argument inside an
    import statement's body classifies by its closest meaningful frame. Purely
    structural — no name resolution.
    """
    n = node
    for _ in range(4):
        parent = n.parent
        if parent is None:
            break
        t = parent.type
        if _IMPORT_RE.search(t):
            return "import"
        if _DEF_RE.search(t):
            for field in ("name", "declarator"):
                if _covers(parent.child_by_field_name(field), node):
                    return "definition"
        if "call" in t or t == "new_expression":
            for field in ("function", "name", "constructor"):
                if _covers(parent.child_by_field_name(field), node):
                    return "call"
        n = parent
    return "reference"


def _walk(tree: Any):
    """Iterative pre-order walk (recursion would overflow on minified files)."""
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


class SymbolsTool(_FsTool):
    """Definitions, references, and outlines over the shared fs path boundary."""

    # Parsing a few hundred files (or one jedi project scan on a large repo)
    # legitimately exceeds the registry's default ceiling; internal work is
    # additionally bounded by _SCAN_DEADLINE_S so a hit here means jedi, not
    # the scan loop.
    timeout_seconds = 150.0

    @property
    def name(self) -> str:
        return "symbols"

    @property
    def description(self) -> str:
        return (
            "Navigate code by symbol, syntax-aware: occurrences inside strings and "
            "comments are excluded, unlike grep. "
            "action='definition' locates where a function/class/type is defined. "
            "action='references' lists every usage, classified as definition/import/call/reference; "
            "for Python it additionally resolves aliased imports and inheritance semantically. "
            "action='outline' lists the definitions in one file with line numbers — cheaper than "
            "reading the file to find one function. "
            "Use this when you need ALL definitions or usages of a symbol (e.g. to fix every "
            "occurrence of a defective pattern); use grep for raw text (strings, comments, config). "
            "Results over the cap are saved in full to a file whose path is returned."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["definition", "references", "outline"],
                    "description": "What to look up",
                },
                "symbol": {
                    "type": "string",
                    "description": (
                        "Bare identifier to look up, e.g. 'resolve_expression' or 'QuerySet' "
                        "(no dots, parens, or paths). Required for definition/references."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Directory scope for definition/references (default: workspace root); "
                        "the file to outline for action='outline'."
                    ),
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        symbol: str | None = None,
        path: str = ".",
        **kwargs: Any,
    ) -> str:
        try:
            import tree_sitter_language_pack  # noqa: F401
        except ImportError:
            return (
                "Error: SYMBOLS_UNAVAILABLE — the tree-sitter engine is not installed in "
                "this raven environment. Use grep to search for the symbol as text instead."
            )
        try:
            base = self._resolve(path)
        except PermissionError as e:
            return f"Error: {e}"
        if not base.exists():
            return f"Error: path not found: {path}"

        if action == "outline":
            return self._outline(base, path)

        if not symbol:
            return f"Error: action='{action}' requires the symbol parameter."
        if not re.fullmatch(r"[^\W\d]\w*", symbol):
            return (
                f"Error: {symbol!r} is not a bare identifier. Pass a single name "
                "(e.g. 'bar', not 'foo.bar' or 'bar()'); scope with path= instead."
            )
        if base.is_dir() and _denied_traversal_root(base):
            return (
                f"Error: refusing to scan '{path}' — it resolves to a system root. "
                "Specify a narrower directory (e.g. the workspace or a project subtree)."
            )

        candidates, discovery_note = await self._candidate_files(base, symbol)
        if not candidates:
            return (
                f"No occurrences of '{symbol}' found under {path!r} in files with supported "
                f"extensions.{discovery_note} If it may appear in strings, comments, or other "
                "file types, use grep."
            )

        hits, scan_note = self._scan(candidates, base, symbol)
        if not hits:
            return (
                f"'{symbol}' appears textually under {path!r} but never as a code identifier "
                f"(only inside strings/comments, or in unparseable files).{scan_note} "
                "Use grep to see the textual occurrences."
            )

        if action == "definition":
            return self._render_definitions(symbol, hits, scan_note + discovery_note)
        return self._render_references(symbol, base, hits, scan_note + discovery_note)

    # ── candidate discovery ─────────────────────────────────────────────

    async def _candidate_files(self, base: Path, symbol: str) -> tuple[list[Path], str]:
        """Files under ``base`` that literally contain ``symbol``, capped.

        rg does the discovery when available (word-boundary, fixed string); the
        fallback is the same deadline-capped walk contract as grep's, and both
        report partiality instead of presenting a subset as the universe.
        """
        if base.is_file():
            return ([base] if base.suffix in _EXT_LANG else []), ""
        rg = resolve_rg()
        if rg is None:
            return self._walk_candidates(base, symbol)

        import asyncio

        args = [rg, "-l", "--fixed-strings", "-w", "--color=never"]
        for ext in _EXT_LANG:
            args += ["-g", f"*{ext}"]
        for d in _IGNORE_DIRS:
            args += ["-g", f"!{d}"]
        args += ["--", symbol, str(base)]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, _err = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            return self._walk_candidates(base, symbol)
        files = [Path(line) for line in out.decode("utf-8", "replace").splitlines() if line]
        note = ""
        if len(files) > _MAX_CANDIDATE_FILES:
            note = (
                f" [note: '{symbol}' occurs in {len(files)} files; only the first "
                f"{_MAX_CANDIDATE_FILES} were analyzed — narrow path= for full coverage]"
            )
            files = files[:_MAX_CANDIDATE_FILES]
        return files, note

    def _walk_candidates(self, base: Path, symbol: str) -> tuple[list[Path], str]:
        deadline = time.monotonic() + 20.0
        found: list[Path] = []
        cut = False
        for root, dirs, names in os.walk(base):
            if time.monotonic() > deadline or len(found) > _MAX_CANDIDATE_FILES:
                cut = True
                break
            dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS]
            for n in sorted(names):
                fp = Path(root) / n
                if fp.suffix not in _EXT_LANG:
                    continue
                try:
                    if symbol in fp.read_text("utf-8", errors="replace"):
                        found.append(fp)
                except OSError:
                    continue
        note = " [note: candidate discovery was cut short — results may be incomplete]" if cut else ""
        return found, note

    # ── tree-sitter scan ────────────────────────────────────────────────

    def _scan(self, files: list[Path], base: Path, symbol: str) -> tuple[list[dict], str]:
        from tree_sitter_language_pack import get_parser

        symbol_b = symbol.encode()
        deadline = time.monotonic() + _SCAN_DEADLINE_S
        hits: list[dict] = []
        parsed = 0
        for fp in files:
            if time.monotonic() > deadline:
                return hits, (
                    f" [note: analysis stopped at the {int(_SCAN_DEADLINE_S)}s limit after "
                    f"{parsed}/{len(files)} files — results are incomplete]"
                )
            lang = _EXT_LANG.get(fp.suffix)
            if lang is None:
                continue
            try:
                src = fp.read_bytes()
                tree = get_parser(lang).parse(src)
            except Exception as e:  # unparseable file or missing grammar wheel
                logger.debug("symbols: skipping {} ({})", fp, e)
                continue
            parsed += 1
            lines = src.splitlines()
            for node in _walk(tree):
                if not _is_identifier_leaf(node):
                    continue
                if src[node.start_byte : node.end_byte] != symbol_b:
                    continue
                row = node.start_point[0]
                snippet = lines[row].decode("utf-8", "replace").strip() if row < len(lines) else ""
                hits.append(
                    {
                        "file": fp,
                        "rel": self._rel(fp, base),
                        "line": row + 1,
                        "col": node.start_point[1],
                        "kind": _classify(node),
                        "snippet": snippet[:200],
                        "lang": lang,
                    }
                )
        return hits, ""

    @staticmethod
    def _rel(fp: Path, base: Path) -> str:
        root = base if base.is_dir() else base.parent
        try:
            return fp.relative_to(root).as_posix()
        except ValueError:
            return fp.as_posix()

    # ── rendering ───────────────────────────────────────────────────────

    def _render_definitions(self, symbol: str, hits: list[dict], note: str) -> str:
        defs = [h for h in hits if h["kind"] == "definition"]
        if not defs:
            others = len(hits)
            return (
                f"No definition of '{symbol}' found ({others} other occurrences exist — "
                f"run action='references' to see them).{note} The definition may live in a "
                "dependency outside this tree, or use a naming construct this engine "
                "does not classify; grep for it in that case."
            )
        out = [f"Definitions of '{symbol}' ({len(defs)}):"]
        for h in defs:
            out.append(f"  {h['rel']}:{h['line']}: {h['snippet']}")
        if note:
            out.append(note.strip())
        return self._finish(out, "symbols")

    def _render_references(self, symbol: str, base: Path, hits: list[dict], note: str) -> str:
        engine = "tree-sitter (syntactic)"
        py_defs = [h for h in hits if h["kind"] == "definition" and h["lang"] == "python"]
        if py_defs:
            semantic = self._jedi_references(base, py_defs[0])
            if semantic is not None:
                hits, engine = semantic, "jedi (semantic: aliases and inheritance resolved)"

        by_kind: dict[str, list[dict]] = {}
        seen: set[tuple[str, int, str]] = set()
        for h in hits:
            key = (h["rel"], h["line"], h["kind"])
            if key in seen:
                continue
            seen.add(key)
            by_kind.setdefault(h["kind"], []).append(h)

        files = {h["rel"] for h in hits}
        out = [f"References to '{symbol}' — {sum(len(v) for v in by_kind.values())} occurrences in {len(files)} files [engine: {engine}]:"]
        for kind in ("definition", "import", "call", "reference"):
            group = by_kind.pop(kind, [])
            if not group:
                continue
            out.append(f"{kind} ({len(group)}):")
            for h in group:
                out.append(f"  {h['rel']}:{h['line']}: {h['snippet']}")
        for kind, group in by_kind.items():  # future-proof: unmapped kinds still render
            out.append(f"{kind} ({len(group)}):")
            for h in group:
                out.append(f"  {h['rel']}:{h['line']}: {h['snippet']}")
        if note:
            out.append(note.strip())
        return self._finish(out, "symbols")

    def _jedi_references(self, base: Path, def_hit: dict) -> list[dict] | None:
        """Semantic Python references from the first definition site, or None.

        None (not an error string) on ANY failure: jedi is an upgrade, never a
        gate — the caller falls back to the syntactic listing it already has.
        """
        try:
            import jedi

            root = base if base.is_dir() else base.parent
            script = jedi.Script(path=str(def_hit["file"]), project=jedi.Project(str(root)))
            names = script.get_references(line=def_hit["line"], column=def_hit["col"], include_builtins=False)
        except Exception as e:
            logger.debug("symbols: jedi fallback ({})", e)
            return None
        if not names:
            return None
        hits: list[dict] = []
        for n in names:
            if n.module_path is None:
                continue
            try:
                snippet = n.get_line_code().strip()
            except Exception:
                snippet = ""
            hits.append(
                {
                    "file": Path(n.module_path),
                    "rel": self._rel(Path(n.module_path), base),
                    "line": n.line or 0,
                    "col": n.column or 0,
                    "kind": "definition" if n.is_definition() else "reference",
                    "snippet": snippet[:200],
                    "lang": "python",
                }
            )
        return hits or None

    def _outline(self, base: Path, path: str) -> str:
        from tree_sitter_language_pack import get_parser

        if not base.is_file():
            return f"Error: action='outline' takes a file, got a directory: {path}"
        lang = _EXT_LANG.get(base.suffix)
        if lang is None:
            supported = ", ".join(sorted({e for e in _EXT_LANG}))
            return f"Error: unsupported file type '{base.suffix}' for outline. Supported: {supported}"
        try:
            src = base.read_bytes()
            tree = get_parser(lang).parse(src)
        except Exception as e:
            return f"Error: could not parse {path}: {e}"

        lines = src.splitlines()
        out = [f"Outline of {path} ({lang}):"]

        def visit(node: Any, depth: int) -> None:
            for child in node.children:
                is_def = _OUTLINE_RE.search(child.type) and child.child_by_field_name("name") is not None
                if is_def:
                    row = child.start_point[0]
                    snippet = lines[row].decode("utf-8", "replace").strip() if row < len(lines) else ""
                    out.append(f"  {'  ' * depth}{row + 1}: {snippet[:160]}")
                    visit(child, depth + 1)
                else:
                    visit(child, depth)

        visit(tree.root_node, 0)
        if len(out) == 1:
            return f"No definitions found in {path} — the file may be data or script-only code."
        return self._finish(out, "symbols-outline")

    def _finish(self, out: list[str], spill_prefix: str) -> str:
        """Join, cap, and spill — the same no-silent-loss contract as grep."""
        if len(out) > _MAX_SHOWN_LINES:
            spilled = spill_output("\n".join(out), spill_prefix)
            saved = f" The COMPLETE result was saved to {spilled} — grep or read_file it." if spilled else ""
            shown = out[:_MAX_SHOWN_LINES]
            shown.append(
                f"\n(⚠️ showing first {_MAX_SHOWN_LINES} of {len(out)} lines — this is a PARTIAL "
                f"view, do not count from it.{saved})"
            )
            return "\n".join(shown)
        return "\n".join(out)
