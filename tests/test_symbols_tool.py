"""Unit tests for the symbols tool.

The tree-sitter layer is exercised on Python and Go fixtures (syntactic
classification, string/comment exclusion); the jedi layer on a Python package
with an aliased import — the case that motivates semantic resolution, because
a textual grep for the original name misses the alias's call sites.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import raven.agent.tools.symbols as sym
from raven.agent.tools.symbols import SymbolsTool


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text(
        "def target(x):\n"
        "    return x\n"
        "\n"
        "\n"
        "class Base:\n"
        "    def run(self):\n"
        "        return target(1)\n"
        "\n"
        "\n"
        "class Child(Base):\n"
        "    def run(self):\n"
        "        return target(2)\n"
    )
    (pkg / "use.py").write_text(
        "from pkg.mod import target as t2\n"
        "\n"
        "note = 'target practice'  # a target in a comment\n"
        "result = t2(3)\n"
    )
    (tmp_path / "main.go").write_text(
        "package main\n"
        "\n"
        "func Target() int {\n"
        "\treturn 1\n"
        "}\n"
        "\n"
        "func main() {\n"
        "\tTarget()\n"
        "}\n"
    )
    return tmp_path


def _tool(repo: Path) -> SymbolsTool:
    return SymbolsTool(workspace=repo, allowed_dir=repo)


# ── definition ──────────────────────────────────────────────────────────


async def test_definition_python(repo: Path):
    out = await _tool(repo).execute(action="definition", symbol="target")
    assert "pkg/mod.py:1" in out
    assert "def target(x):" in out
    # string/comment occurrences in use.py are structurally invisible
    assert "use.py" not in out


async def test_definition_go(repo: Path):
    out = await _tool(repo).execute(action="definition", symbol="Target")
    assert "main.go:3" in out
    assert "func Target()" in out


async def test_definition_missing_reports_other_occurrences(repo: Path):
    out = await _tool(repo).execute(action="definition", symbol="t2")
    # t2 is only ever an import alias + call, never a def/class
    assert "No definition" in out
    assert "references" in out


# ── references ──────────────────────────────────────────────────────────


async def test_references_python_resolves_alias(repo: Path):
    """The semantic case: usage sites of the ALIAS must surface when asking
    about the original name — this is what grep structurally cannot do."""
    out = await _tool(repo).execute(action="references", symbol="target")
    assert "use.py:1" in out  # the aliased import line
    assert "mod.py:7" in out and "mod.py:12" in out  # both call sites
    # the string literal and comment on use.py:3 must NOT appear
    assert "use.py:3" not in out


async def test_references_classifies_calls(repo: Path):
    out = await _tool(repo).execute(action="references", symbol="Target")
    assert "call" in out
    assert "main.go:8" in out
    assert "main.go:3" in out  # the definition is listed too


async def test_references_syntactic_fallback_when_jedi_unavailable(repo: Path, monkeypatch):
    """jedi is an upgrade, never a gate: with it broken the tree-sitter
    listing still answers, labeled as syntactic."""
    monkeypatch.setattr(sym.SymbolsTool, "_jedi_references", lambda self, base, d: None)
    out = await _tool(repo).execute(action="references", symbol="target")
    assert "tree-sitter (syntactic)" in out
    assert "import" in out
    assert "use.py:1" in out


# ── outline ─────────────────────────────────────────────────────────────


async def test_outline_lists_definitions_with_lines(repo: Path):
    out = await _tool(repo).execute(action="outline", path="pkg/mod.py")
    assert "1: def target(x):" in out
    assert "5: class Base:" in out
    assert "10: class Child(Base):" in out


async def test_outline_nests_methods_under_class(repo: Path):
    out = await _tool(repo).execute(action="outline", path="pkg/mod.py")
    lines = out.splitlines()
    class_line = next(ln for ln in lines if "class Base" in ln)
    method_line = next(ln for ln in lines if ln.strip().startswith("6:"))
    assert len(method_line) - len(method_line.lstrip()) > len(class_line) - len(class_line.lstrip())


async def test_outline_rejects_directory(repo: Path):
    out = await _tool(repo).execute(action="outline", path="pkg")
    assert out.startswith("Error")


async def test_outline_unsupported_extension(repo: Path):
    (repo / "data.txt").write_text("hello\n")
    out = await _tool(repo).execute(action="outline", path="data.txt")
    assert out.startswith("Error")
    assert ".py" in out  # names what IS supported


# ── input validation and empty results ──────────────────────────────────


async def test_references_requires_symbol(repo: Path):
    out = await _tool(repo).execute(action="references")
    assert out.startswith("Error")
    assert "symbol" in out


async def test_dotted_symbol_rejected_with_guidance(repo: Path):
    out = await _tool(repo).execute(action="references", symbol="pkg.target")
    assert out.startswith("Error")
    assert "bare identifier" in out


async def test_no_occurrences_points_at_grep(repo: Path):
    out = await _tool(repo).execute(action="references", symbol="zzz_nonexistent")
    assert "No occurrences" in out
    assert "grep" in out


async def test_walk_fallback_without_rg(repo: Path, monkeypatch):
    monkeypatch.setattr(sym, "resolve_rg", lambda: None)
    out = await _tool(repo).execute(action="definition", symbol="target")
    assert "pkg/mod.py:1" in out
