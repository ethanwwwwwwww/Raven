"""survey_sources: one structured deep-scan of every attached store, up front.

The recurring upstream failure in data tasks is starting to query before the
data system is understood: the wrong one of two synonym columns gets grouped,
a policy threshold gets matched to the wrong unit of measure, a join fans out
silently. All of these are visible in the data before the first real query --
if someone looks. This tool is that look: pure SQL probes, no LLM calls, run
once at the start; a compact map comes back and the full notes land in the
workspace for later reference.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from raven.agent.tools.base import Tool
from raven.plugin.context import PluginContext
from raven.plugin.data_agent.session import DataAgentSession

_SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "temp", "system"}
_MAX_TABLES = 24
_MAX_COLUMNS_PER_TABLE = 40
_TOP_VALUES = 4
_SOURCE_BUDGET_SEC = 60.0
_SUMMARY_CHARS = 4000
_NOTES_PATH = "/workspace/survey.md"
_SYNONYM_TOKEN_MIN = 5
_FD_MAX_PAIRS_PER_TABLE = 6
_FD_MAX_DISTINCT = 2000
_PLACEHOLDERS = {"na", "n/a", "none", "null", "unknown", "-", "--", "#", ""}
_BRACKET_RE = re.compile(r"^\s*\[.*\]\s*$")
_TEMPLATE_FAMILIES = 6
_TEMPLATE_MIN_ROWS = 20
_TEMPLATE_MIN_COVERAGE = 0.6
_NEAR_FD_PURITY = 0.95
_PHRASING_MIN_AVG_LEN = 40
_PHRASING_PREFIX_CHARS = 32
_FREE_TEXT_SHAPE_RATIO = 0.05


def _tokens(name: str) -> set[str]:
    parts = re.split(r"[_\W]+", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name))
    out = set()
    for p in parts:
        p = p.lower()
        if len(p) < _SYNONYM_TOKEN_MIN:
            continue
        # An 8-char stem folds inflections of one concept ("category" /
        # "categorical") into the same group.
        out.add(p[:8] if len(p) >= 8 else p)
    return out


class _TimeoutError(Exception):
    pass


class SurveySourcesTool(Tool):
    """Deterministic schema/value/relationship survey over all sources."""

    timeout_seconds = 600.0

    def __init__(self, session: DataAgentSession) -> None:
        self._session = session
        self._session_notes: list[str] = []

    @property
    def name(self) -> str:
        return "survey_sources"

    @property
    def description(self) -> str:
        return (
            "Run once, first: a deterministic survey of every attached store -- per-column "
            "types, distincts, nulls and top values; groups of columns whose names share a "
            "root (old/new or name/code variants of the same concept, with which one carries "
            "which values); dirty-value signatures (bracketed annotations, placeholders, "
            "mixed types); numeric columns classified as count-like, money-like or "
            "ratio-like; numbers encoded inside text, with their phrasing families and how "
            "to extract them fully; parent/child column pairs, exact and near (one concept "
            "stored at two grains); and same-name join candidates across "
            "tables. Returns a compact map and writes full notes to survey.md. Reading the "
            "map before the first real query is how column-choice and unit-of-measure "
            "mistakes get caught while they are still free."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def _q(self, sql: str, deadline: float, preview: int = 32) -> list:
        if time.monotonic() > deadline:
            raise _TimeoutError()
        resp = await asyncio.to_thread(self._session.sql, sql, preview=preview)
        # Attach failures and other kernel bootstrap notes ride on responses;
        # a survey that silently sees 0 tables is worse than one that says why.
        for note in resp.get("session_notes") or []:
            if note not in self._session_notes:
                self._session_notes.append(str(note))
        if not resp.get("ok"):
            raise RuntimeError(str(resp.get("error")))
        return resp.get("rows") or []

    async def execute(self) -> str:
        start = time.monotonic()
        hard_deadline = start + self.timeout_seconds - 30
        try:
            raw = await self._enumerate_tables(hard_deadline)
        except (RuntimeError, _TimeoutError) as exc:
            return f"[SURVEY ERROR] cannot enumerate tables: {exc}"
        import ast

        def _aslist(v: Any) -> list[str]:
            if isinstance(v, list):
                return [str(x) for x in v]
            try:
                return [str(x) for x in ast.literal_eval(str(v))]
            except (ValueError, SyntaxError):
                return []

        tables = [
            (str(c), str(s), str(t), _aslist(cn), _aslist(ct))
            for c, s, t, cn, ct in raw
            if str(s).lower() not in _SYSTEM_SCHEMAS and str(c).lower() not in _SYSTEM_SCHEMAS
        ]
        skipped_tables = max(0, len(tables) - _MAX_TABLES)
        tables = tables[:_MAX_TABLES]

        notes: list[str] = ["# Source survey (auto-generated, this attempt only)", ""]
        summary: list[str] = []
        col_index: dict[str, list[tuple[str, int]]] = defaultdict(list)

        for catalog, schema, table, col_names, col_types in tables:
            fq = f'"{catalog}"."{schema}"."{table}"'
            label = f"{catalog}.{table}" if schema == "main" else f"{catalog}.{schema}.{table}"
            deadline = min(hard_deadline, time.monotonic() + _SOURCE_BUDGET_SEC)
            try:
                total = int((await self._q(f"SELECT COUNT(*) FROM {fq}", deadline))[0][0])
            except (RuntimeError, _TimeoutError) as exc:
                notes.append(f"## {label}: [unreadable: {exc}]")
                continue
            cols, dropped = self._pick_columns(list(zip(col_names, col_types)))
            notes.append(f"## {label} -- {total} rows, {len(col_names)} columns")
            summary.append(f"{label}: {total} rows, {len(col_names)} cols")
            if dropped:
                # A silently halved wide table reads as a complete map, so the
                # skipped names are listed: the agent can still probe them.
                line = (
                    f"{label}: profiled {len(cols)} of {len(col_names)} columns "
                    f"(name-related ones first); NOT profiled: {', '.join(dropped[:20])}"
                    + (f" and {len(dropped) - 20} more" if len(dropped) > 20 else "")
                )
                notes.append(line)
                summary.append(line)
            facts: dict[str, dict] = {}

            for name, dtype in ((str(c), str(d)) for c, d in cols):
                q = f'"{name}"'
                fact: dict[str, Any] = {"dtype": dtype}
                try:
                    nn, nd = (
                        await self._q(
                            f"SELECT COUNT({q}), COUNT(DISTINCT {q}) FROM {fq}", deadline
                        )
                    )[0]
                    fact["non_null"], fact["distinct"] = int(nn), int(nd)
                    lower = dtype.lower()
                    if any(k in lower for k in ("char", "text", "string", "uuid")):
                        await self._text_probe(fq, q, fact, deadline)
                    elif any(
                        k in lower for k in ("int", "decimal", "double", "float", "numeric", "hugeint")
                    ):
                        await self._numeric_probe(fq, q, fact, deadline)
                except _TimeoutError:
                    fact["note"] = "budget hit"
                except RuntimeError as exc:
                    fact["note"] = f"error {str(exc)[:60]}"
                facts[name] = fact
                col_index[name.lower()].append((label, fact.get("distinct", 0)))

            for name, fact in facts.items():
                bits = [
                    fact["dtype"],
                    f"distinct={fact.get('distinct', '?')}",
                    f"null={total - fact.get('non_null', total)}",
                ]
                for key in ("top", "shape", "bracketed", "placeholders", "list_valued",
                            "number_in_text", "phrasing_families", "note"):
                    if fact.get(key):
                        bits.append(f"{key}={fact[key]}")
                notes.append(f"- {name}: " + ", ".join(str(b) for b in bits))

            listy = [n for n, f in facts.items() if f.get("list_valued")]
            if listy:
                line = (
                    f"LIST-VALUED columns in {label}: {', '.join(listy)} -- values hold "
                    "several comma-separated items; deliver the stored string whole "
                    "(never one item of it) and match with LIKE/contains, not equality."
                )
                notes.append(line)
                summary.append(line)

            templated = [(n, f["number_in_text"]) for n, f in facts.items() if f.get("number_in_text")]
            for n, census in templated[:2]:
                line = (
                    f"NUMBER-IN-TEXT column \"{n}\" in {label}: {census} -- extracting the "
                    "number must cover EVERY family (OR/COALESCE over per-family patterns); "
                    "a single LIKE on the first family that matches silently drops the rest."
                )
                notes.append(line)
                summary.append(line)

            phrased = [(n, f["phrasing_families"]) for n, f in facts.items() if f.get("phrasing_families")]
            for n, census in phrased[:2]:
                line = (
                    f"PHRASING FAMILIES in free-text column \"{n}\" of {label}: {census} -- any "
                    "cohort or attribute read out of this column must match EVERY shape, not the "
                    "first one that works. Reconcile before using it: the rows your pattern "
                    "captures must equal the rows that mention the target at all "
                    f"(COUNT(*) WHERE \"{n}\" ILIKE '%<target>%'); a shortfall means a missed "
                    "shape, and the population it drops never shows up in any later result."
                )
                notes.append(line)
                summary.append(line)

            groups = self._synonym_groups(facts)
            for token, members in groups:
                detail = "; ".join(
                    f'"{m}" ({facts[m].get("distinct", "?")} distinct'
                    + (f", {facts[m]['bracketed']} bracketed" if facts[m].get("bracketed") else "")
                    + ")"
                    for m in members
                )
                line = (
                    f"SYNONYM GROUP '{token}' in {label}: {detail} -- decide which "
                    "column the question's wording actually applies to before grouping."
                )
                notes.append(line)
                summary.append(line)

            try:
                for line in await self._fd_pairs(fq, facts, deadline):
                    notes.append(line)
                    summary.append(f"{label}: {line}")
            except (_TimeoutError, RuntimeError):
                pass
            notes.append("")

        join_lines = [
            f"JOIN CANDIDATE column '{name}' appears in: "
            + ", ".join(f"{t} ({d} distinct)" for t, d in refs)
            for name, refs in sorted(col_index.items())
            if len(refs) > 1 and not name.startswith("_")
        ][:12]
        notes.extend(join_lines)
        if skipped_tables:
            notes.append(f"({skipped_tables} further tables not surveyed -- cap)")

        try:
            Path(_NOTES_PATH).parent.mkdir(parents=True, exist_ok=True)
            Path(_NOTES_PATH).write_text("\n".join(notes) + "\n", encoding="utf-8")
            wrote = f"full notes in {_NOTES_PATH}"
        except OSError:
            wrote = "notes file not writable; summary only"

        summary.extend(join_lines[:6])
        head = (
            f"surveyed {len(tables)} tables in {time.monotonic() - start:.0f}s; {wrote}\n"
        )
        boot = list(getattr(self._session, "_bootstrap_notes", []) or [])
        for note in boot:
            if note not in self._session_notes:
                self._session_notes.append(str(note))
        if self._session_notes:
            head += "".join(f"[SESSION] {n}\n" for n in self._session_notes[:8])
        if not tables:
            configured = ", ".join(f"{s.alias}({s.kind})" for s in self._session.sources) or "none"
            head += (
                f"No tables visible on the query plane (configured sources: {configured}) -- "
                "the stores may not have attached (see notes above); fall back to connecting "
                "to each store directly.\n"
            )
            try:
                dbs = await self._q(
                    "SELECT database_name FROM duckdb_databases()", hard_deadline
                )
                head += "attached catalogs: " + ", ".join(str(r[0]) for r in dbs) + "\n"
            except (RuntimeError, _TimeoutError) as exc:
                head += f"duckdb_databases() probe failed: {exc}\n"
        return head + "\n".join(summary)[: _SUMMARY_CHARS]

    async def _enumerate_tables(self, deadline: float) -> list:
        # SHOW ALL TABLES spans every attached catalog including scanner-backed
        # ones (postgres/sqlite) and carries column names/types in one pass --
        # but on some engine/scanner combinations it dies inside the scanner's
        # stats path (duckdb 1.3.1 + attached sqlite: hugeint-wrapped cast
        # errors or allocation failures) while plain catalog reads stay fine.
        # The survey is the session's only global reconnaissance, so a broken
        # primary must degrade, not zero the tool: fall back to duckdb_tables()
        # + duckdb_columns(), then to per-catalog SHOW TABLES + DESCRIBE, and
        # record which path (and which catalogs, if any) got lost.
        try:
            return await self._q(
                "SELECT database, schema, name, column_names, column_types "
                "FROM (SHOW ALL TABLES) ORDER BY database, schema, name",
                deadline,
                preview=200,
            )
        except RuntimeError as exc:
            self._session_notes.append(
                f"table enumeration degraded (SHOW ALL TABLES failed: {str(exc)[:120]})"
            )
        try:
            tables = await self._q(
                "SELECT database_name, schema_name, table_name FROM duckdb_tables() "
                "ORDER BY database_name, schema_name, table_name",
                deadline,
                preview=200,
            )
            cols = await self._q(
                "SELECT database_name, schema_name, table_name, column_name, data_type "
                "FROM duckdb_columns() ORDER BY database_name, schema_name, table_name, "
                "column_index",
                deadline,
                preview=5000,
            )
            by_table: dict[tuple, tuple[list, list]] = defaultdict(lambda: ([], []))
            for db, sc, tb, col, typ in cols:
                names, types = by_table[(str(db), str(sc), str(tb))]
                names.append(str(col))
                types.append(str(typ))
            return [
                (db, sc, tb, *by_table[(str(db), str(sc), str(tb))])
                for db, sc, tb in tables
            ]
        except RuntimeError as exc:
            self._session_notes.append(
                f"duckdb_tables() enumeration failed too ({str(exc)[:120]}); "
                "walking catalogs one by one"
            )
        rows: list = []
        catalogs = await self._q(
            "SELECT database_name FROM duckdb_databases()", deadline
        )
        for (catalog,) in catalogs:
            catalog = str(catalog)
            if catalog.lower() in _SYSTEM_SCHEMAS:
                continue
            try:
                names = await self._q(
                    f'SELECT table_name FROM "{catalog}".information_schema.tables '
                    "ORDER BY table_name",
                    deadline,
                )
            except RuntimeError as exc:
                self._session_notes.append(
                    f"catalog {catalog} not enumerable ({str(exc)[:80]}); skipped"
                )
                continue
            for (table,) in names:
                try:
                    desc = await self._q(
                        f'DESCRIBE "{catalog}"."{table}"', deadline, preview=200
                    )
                    rows.append(
                        (
                            catalog,
                            "main",
                            str(table),
                            [str(d[0]) for d in desc],
                            [str(d[1]) for d in desc],
                        )
                    )
                except RuntimeError:
                    rows.append((catalog, "main", str(table), [], []))
        return rows

    async def _text_probe(self, fq: str, q: str, fact: dict, deadline: float) -> None:
        rows = await self._q(
            f"SELECT CAST({q} AS VARCHAR), COUNT(*) FROM {fq} WHERE {q} IS NOT NULL "
            f"GROUP BY 1 ORDER BY 2 DESC LIMIT {_TOP_VALUES}",
            deadline,
        )
        fact["top"] = "/".join(str(v)[:24] for v, _ in rows)
        stats = (
            await self._q(
                f"SELECT SUM(CASE WHEN regexp_matches(CAST({q} AS VARCHAR), '^\\s*\\[.*\\]\\s*$') "
                "THEN 1 ELSE 0 END), "
                f"SUM(CASE WHEN lower(trim(CAST({q} AS VARCHAR))) IN "
                "('na','n/a','none','null','unknown','-','--','#','') THEN 1 ELSE 0 END), "
                f"SUM(CASE WHEN regexp_matches(CAST({q} AS VARCHAR), ',\\s\\S') "
                "THEN 1 ELSE 0 END), COUNT(*) "
                f"FROM {fq} WHERE {q} IS NOT NULL",
                deadline,
            )
        )[0]
        if stats and int(stats[0] or 0):
            fact["bracketed"] = int(stats[0])
        if stats and int(stats[1] or 0):
            fact["placeholders"] = int(stats[1])
        # A column where a third of the values hold comma-separated items is a
        # list-valued field: answers drawn from it must carry the stored string
        # whole, and equality filters on it silently miss list members.
        if stats and int(stats[3] or 0) and int(stats[2] or 0) >= 0.3 * int(stats[3]):
            fact["list_valued"] = f"{int(stats[2])}/{int(stats[3])} multi-item"
        numericish = (
            await self._q(
                f"SELECT COUNT(*), SUM(CASE WHEN regexp_matches(trim(CAST({q} AS VARCHAR)), "
                f"'^-?[0-9]+(\\.[0-9]+)?$') THEN 1 ELSE 0 END), "
                f"SUM(CASE WHEN regexp_matches(CAST({q} AS VARCHAR), '[0-9]') THEN 1 ELSE 0 END) "
                f"FROM {fq} WHERE {q} IS NOT NULL",
                deadline,
            )
        )[0]
        if numericish and int(numericish[0]) and int(numericish[1] or 0) >= 0.95 * int(numericish[0]):
            await self._numeric_probe(fq, f"TRY_CAST({q} AS DOUBLE)", fact, deadline)
            if fact.get("shape"):
                fact["shape"] = fact["shape"] + ", stored as text"
        else:
            await self._template_census(fq, q, fact, int(numericish[2] or 0) if numericish else 0, deadline)
        await self._phrasing_families(fq, q, fact, deadline)

    async def _phrasing_families(self, fq: str, q: str, fact: dict, deadline: float) -> None:
        """Sentence-shape families in a free-text column.

        A category that only exists inside prose ("... in the X cohort") is
        extracted with a pattern, and prose about one subject is usually written
        several ways. One pattern then captures one family and silently drops the
        rest of the population -- invisible, because what is missing never
        appears in any result. Masking the volatile parts and grouping the head
        of each value counts the families, so an extraction can be reconciled
        against them.
        """
        row = (
            await self._q(
                f"SELECT AVG(length(CAST({q} AS VARCHAR))), COUNT(*) FROM {fq} WHERE {q} IS NOT NULL",
                deadline,
            )
        )[0]
        if not row or row[0] is None:
            return
        avg_len, rows_total = float(row[0]), int(row[1])
        if avg_len < _PHRASING_MIN_AVG_LEN or rows_total < _TEMPLATE_MIN_ROWS:
            return
        # Volatile parts out (ids, uuids, numbers), then group on the opening of
        # each value. Masking content words as well was measured on real prose
        # columns and made the count worse, not better: clause counts vary
        # between rows of one phrasing, so any deeper masking fragments a shape
        # combinatorially. The head of the value is the most stable signal
        # available at zero cost.
        masked = (
            f"regexp_replace(regexp_replace(regexp_replace(CAST({q} AS VARCHAR), "
            "'[0-9A-Fa-f]{8}-[0-9A-Fa-f-]{4,}', 'U', 'g'), '[0-9]+([.,][0-9]+)*', 'N', 'g'), "
            "'[A-Z]{2,}[-N]+', 'ID', 'g')"
        )
        fam_expr = f"substr({masked}, 1, {_PHRASING_PREFIX_CHARS})"
        row2 = (
            await self._q(
                f"SELECT COUNT(DISTINCT {fam_expr}) FROM {fq} WHERE {q} IS NOT NULL", deadline
            )
        )[0]
        shapes_total = int(row2[0] or 0)
        # Few shapes over many rows means generated prose: one pattern captures
        # one shape and silently drops the others. Roughly as many shapes as
        # rows means genuinely free text (documents, comments), where a shape
        # census says nothing -- staying quiet there is what keeps the line
        # worth reading.
        if shapes_total < 2 or shapes_total > max(8, _FREE_TEXT_SHAPE_RATIO * rows_total):
            return
        fams = await self._q(
            f"SELECT {fam_expr} AS fam, COUNT(*) FROM {fq} WHERE {q} IS NOT NULL "
            f"GROUP BY 1 ORDER BY 2 DESC LIMIT {_TEMPLATE_FAMILIES}",
            deadline,
        )
        covered = sum(int(c) for _, c in fams)
        shapes = "/".join(f"'{str(f).strip()[:40]}...'({c})" for f, c in fams)
        fact["phrasing_families"] = (
            f"{shapes_total} distinct shapes over {rows_total} rows; top {len(fams)} "
            f"({covered} rows): {shapes}"
        )

    async def _template_census(
        self, fq: str, q: str, fact: dict, digit_rows: int, deadline: float
    ) -> None:
        """Numbers encoded inside natural-language text come in phrasing families.

        Masking digit runs to N and grouping exposes the families; an extractor
        built from the first LIKE that matches silently drops every other
        family, so the census exists to make full coverage checkable up front.
        Only repetitive phrasing qualifies -- free text masks to mostly unique
        templates and is skipped.
        """
        if digit_rows < _TEMPLATE_MIN_ROWS:
            return
        rows = await self._q(
            f"SELECT regexp_replace(CAST({q} AS VARCHAR), '[0-9]+([.,][0-9]+)*', 'N', 'g') AS tpl, "
            f"COUNT(*) FROM {fq} WHERE {q} IS NOT NULL AND "
            f"regexp_matches(CAST({q} AS VARCHAR), '[0-9]') "
            f"GROUP BY 1 ORDER BY 2 DESC LIMIT {_TEMPLATE_FAMILIES}",
            deadline,
        )
        covered = sum(int(c) for _, c in rows)
        if not rows or covered < _TEMPLATE_MIN_COVERAGE * digit_rows:
            return
        fams = "/".join(f"'{str(t)[:48]}'({c})" for t, c in rows)
        fact["number_in_text"] = f"{digit_rows} values carry numbers; families {fams}"

    async def _numeric_probe(self, fq: str, q: str, fact: dict, deadline: float) -> None:
        row = (
            await self._q(
                f"SELECT MIN({q}), MAX({q}), AVG(CASE WHEN {q} = FLOOR({q}) THEN 1.0 ELSE 0.0 END) "
                f"FROM {fq} WHERE {q} IS NOT NULL",
                deadline,
            )
        )[0]
        if not row or row[0] is None:
            return
        lo, hi, int_share = float(row[0]), float(row[1]), float(row[2] or 0)
        if 0.0 <= lo and hi <= 1.0:
            fact["shape"] = "ratio-like (0..1)"
        elif int_share > 0.99 and hi <= 10_000:
            fact["shape"] = f"count-like (integers {lo:.0f}..{hi:.0f})"
        elif int_share < 0.9:
            fact["shape"] = f"money/measure-like (decimals {lo:.4g}..{hi:.4g})"
        else:
            fact["shape"] = f"integers {lo:.0f}..{hi:.0f}"

    def _pick_columns(
        self, cols: list[tuple[Any, Any]]
    ) -> tuple[list[tuple[str, str]], list[str]]:
        """Choose which columns to profile on a table wider than the cap.

        Physical order is not informative order: the columns that matter for
        grain and column-choice mistakes are the ones whose names describe the
        same concept twice (a label column and its code counterpart), and on a
        wide table those sit far apart. Name-stem-related columns go first, the
        rest keep physical order, and the tail is reported, never dropped in
        silence.
        """
        named = [(str(c), str(d)) for c, d in cols]
        if len(named) <= _MAX_COLUMNS_PER_TABLE:
            return named, []
        groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for entry in named:
            for token in _tokens(entry[0]):
                groups[token].append(entry)
        # Round-robin across stem groups rather than draining one: a group with
        # only one of its members profiled cannot be compared against anything,
        # so the budget buys breadth of comparable pairs, not depth in one stem.
        multi = [g for g in groups.values() if len(g) > 1]
        ordered: list[tuple[str, str]] = []
        seen: set[str] = set()
        for rank in range(max((len(g) for g in multi), default=0)):
            for group in multi:
                if rank < len(group) and group[rank][0] not in seen:
                    seen.add(group[rank][0])
                    ordered.append(group[rank])
        ordered += [c for c in named if c[0] not in seen]
        keep = ordered[:_MAX_COLUMNS_PER_TABLE]
        kept = {n for n, _ in keep}
        return keep, [n for n, _ in named if n not in kept]

    def _synonym_groups(self, facts: dict[str, dict]) -> list[tuple[str, list[str]]]:
        by_token: dict[str, list[str]] = defaultdict(list)
        for name in facts:
            for token in _tokens(name):
                by_token[token].append(name)
        groups = []
        seen: set[frozenset] = set()
        for token, members in by_token.items():
            if len(members) < 2:
                continue
            key = frozenset(members)
            if key in seen:
                continue
            seen.add(key)
            groups.append((token, sorted(members)))
        return groups

    async def _fd_pairs(self, fq: str, facts: dict[str, dict], deadline: float) -> list[str]:
        candidates = [
            n
            for n, f in facts.items()
            if 1 < int(f.get("distinct") or 0) <= _FD_MAX_DISTINCT
            # Hierarchy is a property of categorical levels; measures (floats,
            # money) produce accidental dependencies on small data.
            and not any(k in str(f.get("dtype", "")).lower() for k in ("double", "float", "decimal", "real"))
            and "money" not in str(f.get("shape", ""))
        ]
        # Physical order would spend the whole budget on pairs involving the
        # first column; the informative pairs are the ones whose names describe
        # one concept twice, so those are probed first and the rest fill in.
        viable: list[tuple[str, str]] = []
        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                a, b = candidates[i], candidates[j]
                da, db = int(facts[a]["distinct"]), int(facts[b]["distinct"])
                if da == db or max(da, db) // max(min(da, db), 1) > 500:
                    continue
                viable.append((a, b))
        viable.sort(key=lambda p: 0 if _tokens(p[0]) & _tokens(p[1]) else 1)
        lines: list[str] = []
        pairs = 0
        for a, b in viable:
            if pairs >= _FD_MAX_PAIRS_PER_TABLE:
                return lines
            da, db = int(facts[a]["distinct"]), int(facts[b]["distinct"])
            pairs += 1
            parent, child = (a, b) if da < db else (b, a)
            qp, qc = f'"{parent}"', f'"{child}"'
            viol = int(
                (
                    await self._q(
                        f"SELECT COUNT(*) FROM (SELECT {qc} FROM {fq} GROUP BY {qc} "
                        f"HAVING COUNT(DISTINCT {qp}) > 1) v",
                        deadline,
                    )
                )[0][0]
            )
            child_distinct = max(da, db)
            purity = 1 - viol / child_distinct
            if viol == 0:
                lines.append(
                    f'HIERARCHY: "{parent}" is a parent level of "{child}" '
                    f"({min(da, db)} -> {max(da, db)}); group by the level the question names."
                )
            # A soft functional dependency marks one concept stored at two
            # grains; the mapping's violating values are real groups of the
            # finer grain, not noise -- deleting them changes the answer.
            elif purity >= _NEAR_FD_PURITY:
                lines.append(
                    f'NEAR-HIERARCHY: "{parent}" ~ parent of "{child}" (purity {purity:.2f}; '
                    f"{viol} of {child_distinct} child values map to >1 parent). The grouping "
                    f"concept exists at BOTH grains ({min(da, db)} vs {max(da, db)} groups): compute "
                    "the grain the question names as the main answer and the other as a sensitivity "
                    "check; values that break the mapping are real groups, never noise to clean away."
                )
            # Same-concept names with a mid-range mapping are neither levels nor
            # aliases: they partition the rows differently, so which one the
            # question means changes the answer and no cleaning reconciles them.
            elif _tokens(parent) & _tokens(child):
                lines.append(
                    f'CROSS-CUTTING: "{parent}" ({min(da, db)} distinct) and "{child}" '
                    f"({max(da, db)} distinct) name one concept but map {purity:.2f} pure -- NOT a "
                    "hierarchy and NOT aliases; they group the rows differently, so the answer "
                    "depends on which the question means. Compute both, deliver the one the "
                    "question's own wording names, and record the other's value in the "
                    "adjudication; every distinct stored value is a real group of its column "
                    "(code-shaped values included) and none may be dropped as dirty."
                )
        return lines


def make_survey_sources_tool(ctx: PluginContext) -> Tool:
    from raven.plugin.data_agent.session import get_session, parse_sources

    config = ctx.config or {}
    session = get_session(
        Path(ctx.services.workspace),
        parse_sources(config),
        kernel_python=config.get("kernel_python") or None,
        extension_dir=config.get("duckdb_extension_dir") or None,
    )
    return SurveySourcesTool(session)
