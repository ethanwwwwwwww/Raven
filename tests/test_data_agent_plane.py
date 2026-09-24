"""DuckDB evidence plane and the four L1 tools, against a real fixture.

Builds a two-store fixture (a DuckDB file + a SQLite file with a prefix-mangled
join key) so the tools run over the same shape DataAgentBench uses, then checks
the read-only gate, profiling (sentinel + type-mix), distinct values, and the
join probe (match rate + fan-out).
"""

from __future__ import annotations

import duckdb
import pytest

from raven.plugin.context import PluginContext, ServiceLocator
from raven.plugin.data_agent import tools as dat
from raven.plugin.data_agent.calibration import GENERAL, STRICT, apply_overrides, resolve_profile
from raven.plugin.data_agent.session import DataAgentSession, SourceSpec
from raven.plugin.data_agent.sqlgate import SqlRejectedError, ensure_read_only, wrap_with_cap

# Survey census tests use small fixtures; the shipped row floor targets real tables.
_LOW_ROWS = apply_overrides(GENERAL, {"thresholds": {"template_min_rows": 6}})


@pytest.fixture
def stores(tmp_path):
    sales = tmp_path / "sales.duckdb"
    con = duckdb.connect(str(sales))
    con.execute("CREATE TABLE orders (order_id INTEGER, book_ref VARCHAR, amount DOUBLE)")
    con.execute(
        "INSERT INTO orders VALUES (1,'bref_1',10.0),(2,'bref_1',5.0),(3,'bref_2',7.0),(4,'bref_9',1.0)"
    )
    con.close()
    meta = tmp_path / "meta.db"
    con = duckdb.connect()
    con.execute("INSTALL sqlite_scanner; LOAD sqlite_scanner;")
    con.execute(f"ATTACH '{meta}' AS m (TYPE sqlite)")
    con.execute("CREATE TABLE m.books (book_id VARCHAR, grade VARCHAR)")
    # grade mixes numbers and a sentinel; book_id is prefixed differently from book_ref
    con.execute("INSERT INTO m.books VALUES ('bid_1','5'),('bid_2','NA'),('bid_3','4')")
    con.close()
    return sales, meta


@pytest.fixture
def session(stores, tmp_path):
    sales, meta = stores
    srcs = (
        SourceSpec(alias="sales", kind="duckdb", ref=str(sales)),
        SourceSpec(alias="meta", kind="sqlite", ref=str(meta)),
    )
    return DataAgentSession(workspace=tmp_path, sources=srcs)


def _ctx(session, tmp_path):
    # A PluginContext whose factory would resolve to this exact session.
    return PluginContext(
        config={},
        services=ServiceLocator(workspace=tmp_path),
    )


# ---- read-only gate ----

@pytest.mark.parametrize(
    "bad",
    [
        "INSERT INTO orders VALUES (9,'x',1)",
        "UPDATE orders SET amount=0",
        "DROP TABLE orders",
        "ATTACH 'x.db' AS y",
        "SELECT 1; DROP TABLE orders",
        "COPY orders TO 'out.csv'",
    ],
)
def test_gate_rejects_writes_and_stacking(bad):
    with pytest.raises(SqlRejectedError):
        ensure_read_only(bad)


@pytest.mark.parametrize(
    "ok",
    [
        "SELECT * FROM orders",
        "WITH t AS (SELECT 1 x) SELECT * FROM t",
        "SELECT count(*) FROM orders WHERE amount > 1  -- trailing comment",
    ],
)
def test_gate_allows_reads(ok):
    ensure_read_only(ok)  # must not raise


def test_cap_is_cte_aware():
    wrapped = wrap_with_cap("WITH t AS (SELECT 1 x) SELECT * FROM t", 10)
    assert duckdb.connect().sql(wrapped).fetchall() == [(1,)]


# ---- tools ----

async def test_sql_tool_preview_and_reject(session, tmp_path):
    tool = dat.SqlTool(session)
    out = await tool.execute("SELECT * FROM sales.orders ORDER BY order_id")
    assert "rows=4" in out and "bref_1" in out
    rejected = await tool.execute("DELETE FROM sales.orders")
    assert rejected.startswith("[SQL REJECTED]")


async def test_sql_empty_result_note(session):
    tool = dat.SqlTool(session)
    out = await tool.execute("SELECT * FROM sales.orders WHERE amount > 999")
    assert "rows=0" in out and "Empty is a valid answer" in out


async def test_profile_flags_sentinel_and_type_mix(session):
    tool = dat.ProfileTool(session)
    out = await tool.execute("meta.books", column="grade")
    assert "SENTINEL?" in out  # 'NA' surfaces
    assert "MIXED-TYPE?" in out  # '5','4' numeric but 'NA' not


async def test_distinct_values(session):
    tool = dat.DistinctValuesTool(session)
    out = await tool.execute("sales.orders", "book_ref")
    assert "bref_1" in out and "n_distinct=3" in out


async def test_python_tool_keeps_state_and_shares_con(session):
    tool = dat.PythonTool(session)
    first = await tool.execute("x = 41")
    assert "PYTHON ERROR" not in first
    second = await tool.execute("x + 1")
    assert "42" in second
    via_con = await tool.execute("con.sql('SELECT COUNT(*) FROM sales.orders').fetchone()[0]")
    assert "4" in via_con


async def test_python_tool_auto_inspects_dataframe(session):
    tool = dat.PythonTool(session)
    out = await tool.execute("df = con.sql('SELECT * FROM sales.orders').df()\ndf")
    assert "[AUTO-INSPECT]" in out and "shape=(4," in out


async def test_python_tool_verify_flags_empty_filter(session):
    tool = dat.PythonTool(session)
    await tool.execute("import pandas as pd\nbase = pd.DataFrame({'a': [1, 2, 3]})")
    out = await tool.execute("filtered = base[base['a'] > 99]")
    assert "[VERIFY: WARN]" in out and "filtered" in out


async def test_python_tool_verify_checks_merge_fanout(session):
    tool = dat.PythonTool(session)
    await tool.execute(
        "import pandas as pd\n"
        "left = pd.DataFrame({'k': [1, 1, 1]})\n"
        "right = pd.DataFrame({'k': [1, 1, 1]})"
    )
    out = await tool.execute("merged = left.merge(right, on='k')")
    assert "[VERIFY: FAIL] merge" in out


async def test_python_tool_reports_restart_after_kernel_death(session):
    tool = dat.PythonTool(session)
    await tool.execute("y = 7")
    crashed = await tool.execute("import os; os._exit(1)")
    assert "variable" in crashed.lower() or "kernel" in crashed.lower()
    after = await tool.execute("'y' in dir()")
    assert "KERNEL RESTARTED" in after
    assert "False" in after


async def test_sql_save_as_writes_parquet_handle(session):
    tool = dat.SqlTool(session)
    out = await tool.execute("SELECT * FROM sales.orders", save_as="orders_all")
    assert "orders_all.parquet" in out
    again = await tool.execute(
        f"SELECT COUNT(*) AS n FROM read_parquet('{session.artifact_dir / 'orders_all.parquet'}')"
    )
    assert "4" in again


def test_parse_sources_reads_sources_file(tmp_path):
    import json

    from raven.plugin.data_agent.session import parse_sources

    manifest = tmp_path / "sources.json"
    manifest.write_text(json.dumps([{"alias": "meta", "kind": "sqlite", "path": "/dab/data/m.db"}]))
    specs = parse_sources({"sources": [{"alias": "a", "kind": "duckdb", "path": "/x.duckdb"}], "sources_file": str(manifest)})
    assert [s.alias for s in specs] == ["a", "meta"]
    assert specs[1].ref == "/dab/data/m.db"


async def test_semantic_map_materializes_joinable_table(session):
    from raven.plugin.data_agent.semantic_map import SemanticMapTool

    class FakeProvider:
        async def chat(self, messages, model=None, max_tokens=0, temperature=0.0):
            import json as _json
            from types import SimpleNamespace

            items = _json.loads(messages[0]["content"].split("ITEMS:\n", 1)[1])
            payload = [{"id": it["id"], "value": "big" if "9" in it["text"] else "small"} for it in items]
            return SimpleNamespace(content=_json.dumps(payload))

    tool = SemanticMapTool(session, FakeProvider())
    out = await tool.execute(
        table="sales.orders",
        id_column="order_id",
        text_column="book_ref",
        task="size of the book id",
        output_table="ref_size",
    )
    assert "materialized ref_size" in out and "4 rows, 4 with a value" in out
    joined = await dat.SqlTool(session).execute(
        "SELECT value, COUNT(*) c FROM ref_size GROUP BY 1 ORDER BY 1"
    )
    assert "big" in joined and "small" in joined


async def test_semantic_map_labels_vote_and_tie_break(session):
    from raven.plugin.data_agent.semantic_map import SemanticMapTool

    class SplitProvider:
        def __init__(self):
            self.calls = 0

        async def chat(self, messages, model=None, max_tokens=0, temperature=0.0):
            import json as _json
            from types import SimpleNamespace

            self.calls += 1
            prompt = messages[0]["content"]
            items = _json.loads(prompt.split("ITEMS:\n", 1)[1])
            second_pass = "second, independent" in prompt
            payload = []
            for it in items:
                # The second annotator flips exactly one row; the tie-break
                # pass sides with the first, so majority restores "keep".
                if second_pass and it["id"] == "1":
                    payload.append({"id": it["id"], "value": "flip"})
                else:
                    payload.append({"id": it["id"], "value": "keep"})
            return SimpleNamespace(content=_json.dumps(payload))

    provider = SplitProvider()
    tool = SemanticMapTool(session, provider)
    out = await tool.execute(
        table="sales.orders",
        id_column="order_id",
        text_column="book_ref",
        task="keep or flip",
        labels=["keep", "flip"],
        output_table="voted",
    )
    assert "4 rows, 4 with a value" in out
    assert "1 row(s) disagreed" in out and "1 resolved by majority" in out
    # One batch: first pass + sampled second pass + tie-break for the flipped row.
    assert provider.calls == 3
    values = await dat.SqlTool(session).execute("SELECT DISTINCT value FROM voted")
    assert "keep" in values and "flip" not in values


async def test_semantic_map_consistent_labels_skip_full_second_pass(session):
    from raven.plugin.data_agent.semantic_map import SemanticMapTool

    class SteadyProvider:
        def __init__(self):
            self.calls = 0

        async def chat(self, messages, model=None, max_tokens=0, temperature=0.0):
            import json as _json
            from types import SimpleNamespace

            self.calls += 1
            items = _json.loads(messages[0]["content"].split("ITEMS:\n", 1)[1])
            return SimpleNamespace(content=_json.dumps([{"id": it["id"], "value": "same"} for it in items]))

    ids = [[str(i), f"text {i}"] for i in range(450)]
    session.put_rows("many_rows", ["rid", "body"], ids)
    provider = SteadyProvider()
    tool = SemanticMapTool(session, provider)
    out = await tool.execute(
        table="many_rows",
        id_column="rid",
        text_column="body",
        task="same or not",
        labels=["same", "other"],
        output_table="steady",
    )
    assert "450 rows, 450 with a value" in out
    assert "sampled check only" in out and "0 row(s) disagreed" in out
    # 5 first-pass batches (100 rows each) + 3 sampled second passes,
    # no escalation, no tie-breaks.
    assert provider.calls == 8


async def test_semantic_map_open_extraction_stays_single_pass(session):
    from raven.plugin.data_agent.semantic_map import SemanticMapTool

    class CountingProvider:
        def __init__(self):
            self.calls = 0

        async def chat(self, messages, model=None, max_tokens=0, temperature=0.0):
            import json as _json
            from types import SimpleNamespace

            self.calls += 1
            items = _json.loads(messages[0]["content"].split("ITEMS:\n", 1)[1])
            return SimpleNamespace(content=_json.dumps([{"id": it["id"], "value": "v"} for it in items]))

    provider = CountingProvider()
    tool = SemanticMapTool(session, provider)
    out = await tool.execute(
        table="sales.orders",
        id_column="order_id",
        text_column="book_ref",
        task="anything",
        output_table="single_pass",
    )
    assert "4 rows, 4 with a value" in out
    assert provider.calls == 1


async def test_semantic_map_cross_model_verify_routes_and_arbitrates(session):
    from raven.plugin.data_agent.semantic_map import SemanticMapTool

    class RoutingProvider:
        def __init__(self):
            self.models = []

        async def chat(self, messages, model=None, max_tokens=0, temperature=0.0):
            import json as _json
            from types import SimpleNamespace

            self.models.append(model)
            prompt = messages[0]["content"]
            items = _json.loads(prompt.split("ITEMS:\n", 1)[1])
            verify = "second, independent" in prompt
            arbiter = "deciding annotator" in prompt
            payload = []
            for it in items:
                # The verify and arbiter models both read row 7 differently
                # from the base labeler, so majority overturns the base label.
                if (verify or arbiter) and it["id"] == "7":
                    payload.append({"id": it["id"], "value": "flip"})
                else:
                    payload.append({"id": it["id"], "value": "keep"})
            return SimpleNamespace(content=_json.dumps(payload))

    ids = [[str(i), f"text {i}"] for i in range(450)]
    session.put_rows("cross_rows", ["rid", "body"], ids)
    provider = RoutingProvider()
    tool = SemanticMapTool(
        session, provider, model="fast", verify_model="strong", arbiter_model="strongest"
    )
    assert tool._cross_model
    assert not SemanticMapTool(session, provider, model="fast", verify_model="fast")._cross_model
    out = await tool.execute(
        table="cross_rows",
        id_column="rid",
        text_column="body",
        task="keep or flip",
        labels=["keep", "flip"],
        output_table="crossed",
    )
    assert "450 rows, 450 with a value" in out
    assert "cross-model: second pass by strong" in out and "decided by strongest" in out
    # 5 first-pass batches + 5 full (unsampled) verify batches + 1 arbitration.
    assert provider.models == ["fast"] * 5 + ["strong"] * 5 + ["strongest"]
    flipped = await dat.SqlTool(session).execute("SELECT value FROM crossed WHERE id = '7'")
    assert "flip" in flipped


async def test_resolve_entities_clusters_variants(session):
    from raven.plugin.data_agent.entity_resolution import ResolveEntitiesTool

    rows = []
    for v, n in [
        ("Silver Harbor Nights", 3),
        ("silver harbor níghts", 2),
        ("Silver Harbor Nigts", 1),
        ("Completely Different Song", 4),
    ]:
        rows.extend([[f"{v}-{i}", v] for i in range(n)])
    session.put_rows("plays", ["play_id", "song"], rows)

    tool = ResolveEntitiesTool(session)
    out = await tool.execute(table="plays", column="song", output_table="song_map")
    assert "4 distinct values -> 2 entities" in out
    assert "1 clusters merged 2+ variants" in out
    joined = await dat.SqlTool(session).execute(
        "SELECT m.canonical, COUNT(*) c FROM plays p JOIN song_map m ON p.song = m.value "
        "GROUP BY 1 ORDER BY c DESC"
    )
    # The most frequent original spelling wins the canonical slot, and the
    # merged entity aggregates all 6 plays vs 4 for the unrelated song.
    assert "Silver Harbor Nights" in joined and "6" in joined


async def test_resolve_entities_threshold_and_rejection(session):
    from raven.plugin.data_agent.entity_resolution import ResolveEntitiesTool

    tool = ResolveEntitiesTool(session)
    out = await tool.execute(
        table="sales.orders",
        column="book_ref",
        output_table="x",
        where="1=1; DROP TABLE sales.orders",
    )
    assert out.startswith("[RESOLVE_ENTITIES REJECTED]")
    strict = await tool.execute(
        table="sales.orders", column="book_ref", output_table="refs_strict", threshold=1.0
    )
    # bref_1 / bref_2 / bref_9 stay distinct at threshold 1.0.
    assert "3 distinct values -> 3 entities" in strict


async def test_resolve_entities_keeps_unnormalizable_values_distinct(session):
    from raven.plugin.data_agent.entity_resolution import ResolveEntitiesTool

    rows = []
    for i, v in enumerate(["***", "...", "- . -", "#9", "9"]):
        rows.extend([[f"{v}-{j}-{i}", v] for j in range(2)])
    session.put_rows("titles", ["row_id", "title"], rows)

    tool = ResolveEntitiesTool(session)
    out = await tool.execute(table="titles", column="title", output_table="title_map")
    # Punctuation-only titles normalize to nothing and digits-only labels are
    # placeholders: nothing here may merge, five spellings stay five entities.
    assert "5 distinct values -> 5 entities" in out


async def test_resolve_entities_merges_decorated_variants(session):
    from raven.plugin.data_agent.entity_resolution import ResolveEntitiesTool

    rows = []
    for i, v in enumerate(
        [
            "The River Runs Through Winter Fields",
            "The River Runs Through Winter Fields (Wide mix | Alt take)",
            "006-The River Runs Through Winter Fields",
            "Arn Voss - The River Runs Through Winter Fields",
            "Completely Different Song",
            "Love",
            "Love Story",
        ]
    ):
        rows.append([f"r{i}", v])
    session.put_rows("catalog", ["row_id", "title"], rows)

    tool = ResolveEntitiesTool(session)
    out = await tool.execute(table="catalog", column="title", output_table="catalog_map")
    # The four decorated spellings collapse into one entity; short names must not
    # merge by containment ('Love' stays apart from 'Love Story').
    assert "7 distinct values -> 4 entities" in out


async def test_resolve_entities_placeholders_stay_apart(session):
    from raven.plugin.data_agent.entity_resolution import ResolveEntitiesTool

    rows = []
    for i, v in enumerate(["unknown", "Unknown", "n/a", "[untitled]", "007", "Real Song Name Here"]):
        rows.extend([[f"p{i}-{j}", v] for j in range(2)])
    session.put_rows("labels", ["row_id", "name"], rows)

    tool = ResolveEntitiesTool(session)
    out = await tool.execute(table="labels", column="name", output_table="label_map")
    # 'unknown' and 'Unknown' share a normalized key but are placeholders, so no
    # merging happens anywhere: six distinct spellings stay six entities.
    assert "6 distinct values -> 6 entities" in out
    assert "look like null placeholders" in out


async def test_bootstrap_classifier_scales_sample_to_corpus(session):
    from raven.plugin.data_agent.bootstrap_classifier import BootstrapClassifierTool

    sports = ["match win goal team score league cup final", "player coach season tournament victory"]
    finance = ["stock market shares profit revenue earnings", "bank interest rate bond investor fund"]
    corpus, labeled = [], []
    for i in range(40):
        # First 20 rows sports-flavored, last 20 finance-flavored.
        text = f"{sports[i % 2]} filler{i}" if i < 20 else f"{finance[i % 2]} filler{i}"
        corpus.append([f"r{i}", text])
        if i % 2 == 0:
            labeled.append([f"r{i}", "Sports" if i < 20 else "Finance"])
    # Pad the labeled sample past the minimum by repeating with distinct ids.
    for j in range(20):
        base = corpus[j if j < 10 else 20 + j - 10]
        corpus.append([f"x{j}", base[1]])
        labeled.append([f"x{j}", "Sports" if j < 10 else "Finance"])
    session.put_rows("articles", ["article_id", "body"], corpus)
    session.put_rows("sample_labels", ["id", "value"], labeled)

    tool = BootstrapClassifierTool(session)
    out = await tool.execute(
        table="articles",
        id_column="article_id",
        text_column="body",
        labeled_table="sample_labels",
        output_table="body_labels",
    )
    assert "rows classified" in out
    joined = await dat.SqlTool(session).execute(
        "SELECT label, COUNT(*) FROM body_labels GROUP BY 1 ORDER BY 1"
    )
    assert "Finance" in joined and "Sports" in joined


async def test_bootstrap_classifier_refuses_thin_or_single_label(session):
    from raven.plugin.data_agent.bootstrap_classifier import BootstrapClassifierTool

    session.put_rows("tiny", ["rid", "txt"], [[f"t{i}", f"word{i}"] for i in range(40)])
    session.put_rows("tiny_labels", ["id", "value"], [["t0", "A"], ["t1", "B"]])
    tool = BootstrapClassifierTool(session)
    out = await tool.execute(
        table="tiny",
        id_column="rid",
        text_column="txt",
        labeled_table="tiny_labels",
        output_table="tiny_out",
    )
    assert out.startswith("[BOOTSTRAP_CLASSIFIER REFUSED]")
    session.put_rows(
        "tiny_labels2", ["id", "value"], [[f"t{i}", "OnlyLabel"] for i in range(35)]
    )
    out = await tool.execute(
        table="tiny",
        id_column="rid",
        text_column="txt",
        labeled_table="tiny_labels2",
        output_table="tiny_out2",
    )
    assert "single label" in out


async def test_semantic_map_rejects_write_predicate(session):
    from raven.plugin.data_agent.semantic_map import SemanticMapTool

    tool = SemanticMapTool(session, provider=object())
    out = await tool.execute(
        table="sales.orders",
        id_column="order_id",
        text_column="book_ref",
        task="x",
        output_table="t",
        where="1=1; DROP TABLE sales.orders",
    )
    assert out.startswith("[SEMANTIC_MAP REJECTED]")


def test_semantic_map_parse_tolerates_fences_and_garbage():
    from raven.plugin.data_agent.semantic_map import _parse_batch_response

    assert _parse_batch_response('```json\n[{"id": "1", "value": "a"}]\n```') == {"1": "a"}
    assert _parse_batch_response('noise [{"id": "1", "value": null}] noise') == {"1": None}
    assert _parse_batch_response("no json here") is None
    assert _parse_batch_response("[]") is None


def test_semantic_map_factory_declines_without_provider(tmp_path):
    from raven.plugin.data_agent.semantic_map import make_semantic_map_tool

    ctx = PluginContext(
        config={},
        services=ServiceLocator(workspace=tmp_path),
    )
    assert make_semantic_map_tool(ctx) is None


async def test_verify_join_detects_prefix_mismatch(session):
    tool = dat.VerifyJoinTool(session)
    # raw keys never match: bref_* vs bid_*
    raw = await tool.execute("sales.orders", "book_ref", "meta.books", "book_id")
    assert "NO KEYS MATCH" in raw
    # transform both to the bare id -> they match, and fan-out is reported
    fixed = await tool.execute(
        "sales.orders",
        "book_ref",
        "meta.books",
        "book_id",
        left_transform="replace(book_ref,'bref_','')",
        right_transform="replace(book_id,'bid_','')",
    )
    assert "match_rate=0" not in fixed.split("VERDICT")[0].split("match_rate=")[1][:5]


async def test_verify_join_normalization_probe_and_unmatched_sample(session):
    session.put_rows(
        "jleft", ["k", "v"], [["#A1 ", "x"], ["B2", "y"], ["C3", "z"]]
    )
    session.put_rows("jright", ["k", "w"], [["a1", "1"], ["b2", "2"]])
    tool = dat.VerifyJoinTool(session)
    out = await tool.execute("jleft", "k", "jright", "k")
    assert "key normalization" in out and "0 -> 2" in out
    assert "unmatched left keys" in out

    # partial raw match: the missing-key count and sample name the gap
    session.put_rows(
        "jleft2", ["k", "v"], [["a1", "x"], ["b2", "y"], ["x9", "z"]]
    )
    out = await tool.execute("jleft2", "k", "jright", "k")
    assert "no right match" in out and "x9" in out


async def test_grain_check_detects_hierarchy(session):
    from raven.plugin.data_agent.grain_check import GrainCheckTool

    rows = [
        ["BRCA", "BRCA-LumA", "s1"],
        ["BRCA", "BRCA-LumB", "s2"],
        ["BRCA", "BRCA-LumA", "s3"],
        ["LUAD", "LUAD-prox", "s4"],
        ["LUAD", "LUAD-prox", "s5"],
        ["GBM", "GBM-mes", "s6"],
    ]
    session.put_rows("tumors", ["cancer_type", "cancer_subtype", "sample_id"], rows)
    tool = GrainCheckTool(session)
    out = await tool.execute(table="tumors", columns=["cancer_type", "cancer_subtype"])
    assert '"cancer_type" is a PARENT level of "cancer_subtype"' in out
    assert "(3 -> 4)" in out
    assert "DIFFERENT grains" in out


async def test_grain_check_aliases_and_independent(session):
    from raven.plugin.data_agent.grain_check import GrainCheckTool

    session.put_rows(
        "shops",
        ["shop_code", "shop_name", "city"],
        [["s1", "Alpha", "NY"], ["s2", "Beta", "NY"], ["s3", "Gamma", "LA"], ["s4", "Delta", "SF"]],
    )
    tool = GrainCheckTool(session)
    aliased = await tool.execute(table="shops", columns=["shop_code", "shop_name"])
    assert "1:1 (aliases of the same grain)" in aliased
    # city does not functionally determine shop_code and vice versa is a parent:
    # every shop maps to one city -> city is the coarser level.
    nested = await tool.execute(table="shops", columns=["shop_code", "city"])
    assert '"city" is a PARENT level of "shop_code"' in nested


async def test_grain_check_key_uniqueness(session):
    from raven.plugin.data_agent.grain_check import GrainCheckTool

    session.put_rows(
        "export_dup", ["grp", "val"], [["a", "1"], ["a", "2"], ["b", "3"]]
    )
    tool = GrainCheckTool(session)
    dup = await tool.execute(table="export_dup", columns=["grp"], key_column="grp")
    assert "NOT unique" in dup and "1 surplus" in dup
    session.put_rows("export_ok", ["grp", "val"], [["a", "1"], ["b", "3"]])
    ok = await tool.execute(table="export_ok", columns=["grp"], key_column="grp")
    assert "unique across the export" in ok


async def test_grain_check_error_paths(session):
    from raven.plugin.data_agent.grain_check import GrainCheckTool

    tool = GrainCheckTool(session)
    assert "REJECTED" in await tool.execute(table="sales.orders", columns=[])
    assert "REJECTED" in await tool.execute(
        table="sales.orders", columns=["a", "b", "c", "d", "e"]
    )
    missing = await tool.execute(table="sales.orders", columns=["no_such_column"])
    assert "GRAIN_CHECK ERROR" in missing and "profile the table" in missing


async def test_submit_answer_writes_rows_and_adjudication(tmp_path):
    import json as _json

    from raven.plugin.data_agent.deliver import SubmitAnswerTool

    tool = SubmitAnswerTool(answer_path=str(tmp_path / "answer.txt"))
    out = await tool.execute(
        rows=["Alpha 10", "Beta 8", "Gamma 5"],
        selected_reading="rank stores by units sold, absolute count",
        rejected_readings=["rank by revenue: computed, gives Beta 900 / Alpha 700 / Gamma 120"],
        reason="the question says 'most units'; units_sold has 812 rows vs 0 rows for any revenue column",
    )
    assert "wrote 3 rows" in out
    assert (tmp_path / "answer.txt").read_text() == "Alpha 10\nBeta 8\nGamma 5\n"
    record = _json.loads((tmp_path / "adjudication.json").read_text())
    assert record["rows_delivered"] == 3 and len(record["rejected_readings"]) == 1


async def test_submit_answer_refuses_hollow_adjudication(tmp_path):
    from raven.plugin.data_agent.deliver import SubmitAnswerTool

    tool = SubmitAnswerTool(answer_path=str(tmp_path / "answer.txt"))
    assert "REFUSED" in await tool.execute(rows=[], selected_reading="x", reason="y" * 50)
    assert "REFUSED" in await tool.execute(rows=["a"], selected_reading="x", reason="thin")
    assert not (tmp_path / "answer.txt").exists()


async def test_submit_answer_requires_numbers_when_adjudicating(tmp_path):
    from raven.plugin.data_agent.deliver import SubmitAnswerTool

    tool = SubmitAnswerTool(answer_path=str(tmp_path / "answer.txt"), profile=STRICT)
    out = await tool.execute(
        rows=["ka0X"],
        selected_reading="quantity-tier reading of the discount policy",
        rejected_readings=["mandatory bundle reading"],
        reason="the bundle reading seemed less plausible given the quote contents overall here",
    )
    assert "REFUSED" in out and "measured quantities" in out
    out = await tool.execute(
        rows=["ka0X"],
        selected_reading="quantity-tier reading of the discount policy",
        rejected_readings=["mandatory bundle reading: fires on 209/209 quotes (background)"],
        reason="tier reading: 2659/2966 rows exactly on tier, 0 below; bundle reading fires on 209/209 quotes (background)",
    )
    assert "wrote 1 rows" in out
    out = await tool.execute(
        rows=["42"],
        selected_reading="single literal reading",
        reason="the question admits one reading; direct aggregate over the full table computed",
    )
    assert "REFUSED" in out and "reading families" in out
    out = await tool.execute(
        rows=["42"],
        selected_reading="single literal reading",
        reason="the question admits one reading; direct aggregate over the full table computed",
    )
    assert "wrote 1 rows" in out


async def test_submit_answer_requires_computed_rejected_readings(tmp_path):
    from raven.plugin.data_agent.deliver import SubmitAnswerTool

    tool = SubmitAnswerTool(answer_path=str(tmp_path / "answer.txt"), profile=STRICT)
    base = dict(
        rows=["a", "b", "c"],
        selected_reading="entity-weighted count over resolved names",
        reason="entity-weighted: 41 entities; record-weighted: 977 rows dominated by 3 duplicates",
    )
    out = await tool.execute(
        **base, rejected_readings=["record-weighted count (looked wrong)"]
    )
    assert "REFUSED" in out and "carry no computed value" in out
    out = await tool.execute(
        **base, rejected_readings=["record-weighted count: 977 rows"]
    )
    assert "wrote 3 rows" in out
    out = await tool.execute(
        **base, rejected_readings=["revenue-based reading: no such column in any table"]
    )
    assert "wrote 3 rows" in out


async def test_submit_answer_nudges_single_value_no_alternatives_once(tmp_path):
    from raven.plugin.data_agent.deliver import SubmitAnswerTool

    tool = SubmitAnswerTool(answer_path=str(tmp_path / "answer.txt"), profile=STRICT)
    kwargs = dict(
        rows=["7"],
        selected_reading="calendar-year window, inclusive bounds",
        reason="COUNT over the full filtered population for 2019, one aggregate over 8140 rows",
    )
    out = await tool.execute(**kwargs)
    assert "REFUSED" in out and "reading families" in out
    out = await tool.execute(**kwargs)
    assert "wrote 1 rows" in out

    # Providing computed alternatives up front skips the nudge entirely.
    fresh = SubmitAnswerTool(answer_path=str(tmp_path / "a2.txt"), profile=STRICT)
    out = await fresh.execute(
        rows=["7"],
        selected_reading="calendar-year window, inclusive bounds",
        rejected_readings=["fiscal-year window: 9, differs from calendar 7"],
        reason="calendar 7 vs fiscal 9 over 8140 rows; the question names January explicitly",
    )
    assert "wrote 1 rows" in out


def test_submit_answer_contract_carries_delivery_rules():
    from raven.plugin.data_agent.deliver import SubmitAnswerTool

    tool = SubmitAnswerTool()
    desc = tool.description
    assert "unverifiable" in desc and "outside knowledge" in desc
    assert "removed zero rows" in desc
    assert "record-weighted vs entity-weighted" in desc
    assert "every defensible variant" in desc
    reason_desc = tool.parameters["properties"]["reason"]["description"]
    assert "margin over the runner-up" in reason_desc
    rejected_desc = tool.parameters["properties"]["rejected_readings"]["description"]
    assert "not rejected, it is ignored" in rejected_desc


async def test_submit_answer_refuses_for_each_shortfall_once(tmp_path):
    from raven.plugin.data_agent.deliver import SubmitAnswerTool

    q = tmp_path / "question.txt"
    q.write_text("What is the best year for each CPC group at level 4?")
    tool = SubmitAnswerTool(
        answer_path=str(tmp_path / "answer.txt"), question_path=str(q), profile=STRICT
    )
    kwargs = dict(
        rows=["A61 1963", "H04 1964"],
        selected_reading="best filing year per level-4 CPC class",
        reason="EMA per class over 23 level-4 classes computed from 46 primary code pairs",
    )
    out = await tool.execute(**kwargs)
    assert "REFUSED" in out and "for each" in out
    assert not (tmp_path / "answer.txt").exists()
    # The single-reading nudge is an independent one-shot gate; a short
    # no-alternatives delivery consumes it next, then the answer lands.
    out = await tool.execute(**kwargs)
    assert "REFUSED" in out and "reading families" in out
    out = await tool.execute(**kwargs)
    assert "wrote 2 rows" in out

    scalar = SubmitAnswerTool(
        answer_path=str(tmp_path / "a2.txt"), question_path=str(q), profile=STRICT
    )
    out = await scalar.execute(
        rows=["r1", "r2", "r3"],
        selected_reading="per-group best year, all groups",
        reason="three groups exist in the filtered data; one row each delivered",
    )
    assert "wrote 3 rows" in out

    noq = SubmitAnswerTool(
        answer_path=str(tmp_path / "a3.txt"),
        question_path=str(tmp_path / "missing.txt"),
        profile=STRICT,
    )
    nkw = dict(
        rows=["42"],
        selected_reading="single literal reading",
        reason="direct aggregate over the full table computed, single reading",
    )
    out = await noq.execute(**nkw)
    assert "REFUSED" in out and "reading families" in out
    out = await noq.execute(**nkw)
    assert "wrote 1 rows" in out


async def test_survey_sources_surfaces_synonym_columns_and_shapes(session, tmp_path, monkeypatch):
    from raven.plugin.data_agent import survey as survey_mod
    from raven.plugin.data_agent.survey import SurveySourcesTool

    monkeypatch.setattr(survey_mod, "_NOTES_PATH", str(tmp_path / "survey.md"))
    session.put_rows(
        "clinical",
        ["lesion_type", "lesion_type_code", "sample_id", "purity"],
        [
            ["Alpha type", "100/1", "s1", "0.71"],
            ["Alpha type", "200/2", "s2", "0.55"],
            ["Beta type", "300/3", "s3", "0.62"],
            ["Beta type", "[Not Available]", "s4", "0.44"],
        ],
    )
    session.put_rows(
        "quotes",
        ["quantity", "line_amount"],
        [["5", "1111.11"], ["8", "2222.22"], ["10", "3333.33"], ["7", "4444.44"]],
    )
    out = await SurveySourcesTool(session).execute()
    notes = (tmp_path / "survey.md").read_text()
    assert "SYNONYM GROUP" in out and "lesion" in out.lower()
    assert "1 bracketed" in notes
    assert "count-like" in notes and "money/measure-like" in notes
    assert "ratio-like" in notes
    assert "survey.md" in out


async def test_survey_sources_flags_list_valued_columns(session, tmp_path, monkeypatch):
    from raven.plugin.data_agent import survey as survey_mod
    from raven.plugin.data_agent.survey import SurveySourcesTool

    monkeypatch.setattr(survey_mod, "_NOTES_PATH", str(tmp_path / "survey.md"))
    session.put_rows(
        "business",
        ["name", "categories"],
        [
            ["Shop One", "Hardware, Tools & Supplies, Fasteners"],
            ["Shop Two", "Hardware, Paint & Coatings"],
            ["Shop Three", "Garden, Outdoor Living"],
            ["Solo Kiosk", "Lumber"],
        ],
    )
    out = await SurveySourcesTool(session).execute()
    notes = (tmp_path / "survey.md").read_text()
    assert "LIST-VALUED" in out and "categories" in out
    assert "deliver the stored string whole" in notes
    flagged = out.split("LIST-VALUED columns in ")[1].split("--")[0].split(":")[1]
    assert "categories" in flagged and "name" not in flagged


async def test_survey_sources_survives_broken_show_all_tables(session, tmp_path, monkeypatch):
    from raven.plugin.data_agent import survey as survey_mod
    from raven.plugin.data_agent.survey import SurveySourcesTool

    monkeypatch.setattr(survey_mod, "_NOTES_PATH", str(tmp_path / "survey.md"))
    session.put_rows(
        "tracks",
        ["track_id", "title"],
        [["1", "Song A"], ["2", "Song B"], ["3", "Song C"]],
    )
    tool = SurveySourcesTool(session)
    real_q = tool._q

    async def poisoned_q(sql, deadline, preview=32):
        if "SHOW ALL TABLES" in sql:
            raise RuntimeError(
                "INTERNAL Error: Information loss on integer cast: value "
                "-1456564680077579643 outside of target range"
            )
        return await real_q(sql, deadline, preview=preview)

    monkeypatch.setattr(tool, "_q", poisoned_q)
    out = await tool.execute()
    assert "[SURVEY ERROR]" not in out
    assert "tracks" in out and "title" in (tmp_path / "survey.md").read_text()
    assert "degraded" in out


async def test_survey_sources_detects_hierarchy_and_join_candidates(session, tmp_path, monkeypatch):
    from raven.plugin.data_agent import survey as survey_mod
    from raven.plugin.data_agent.survey import SurveySourcesTool

    monkeypatch.setattr(survey_mod, "_NOTES_PATH", str(tmp_path / "survey.md"))
    session.put_rows(
        "projects_catalog",
        ["org_name", "project_name"],
        [["acme", "acme/a"], ["acme", "acme/b"], ["zeta", "zeta/x"], ["zeta", "zeta/y"], ["zeta", "zeta/z"]],
    )
    session.put_rows("stars", ["project_name", "star_total"], [["acme/a", "10"], ["zeta/x", "7"]])
    out = await SurveySourcesTool(session).execute()
    notes = (tmp_path / "survey.md").read_text()
    assert "HIERARCHY" in notes and "org_name" in notes
    assert "JOIN CANDIDATE" in notes and "project_name" in notes
    assert "surveyed" in out


async def test_survey_template_census_names_phrasing_families(session, tmp_path, monkeypatch):
    from raven.plugin.data_agent import survey as survey_mod
    from raven.plugin.data_agent.survey import SurveySourcesTool

    monkeypatch.setattr(survey_mod, "_NOTES_PATH", str(tmp_path / "survey.md"))
    rows = (
        [[f"downloaded {i * 7} times"] for i in range(1, 5)]
        + [[f"{i * 3} downloads this week"] for i in range(1, 4)]
        + [["no digits in this one"]]
    )
    session.put_rows("pkg_stats", ["stats_text"], rows)
    # Free text masks to mostly unique templates: no family line for it.
    session.put_rows(
        "notes_free",
        ["txt"],
        [[f"met client {i} at venue {chr(65 + i)} discussing item {i * i}"] for i in range(12)],
    )
    out = await SurveySourcesTool(session, profile=_LOW_ROWS).execute()
    assert 'NUMBER-IN-TEXT column "stats_text"' in out
    assert "downloaded N times" in out and "N downloads this week" in out
    assert "cover EVERY family" in out
    assert 'NUMBER-IN-TEXT column "txt"' not in out


async def test_survey_near_hierarchy_reports_dual_grain_purity(session, tmp_path, monkeypatch):
    from raven.plugin.data_agent import survey as survey_mod
    from raven.plugin.data_agent.survey import SurveySourcesTool

    monkeypatch.setattr(survey_mod, "_NOTES_PATH", str(tmp_path / "survey.md"))
    rows = [["p1", f"s{i:02d}"] for i in range(10)]
    rows += [["p2", f"s{i:02d}"] for i in range(10, 20)]
    rows += [["p1", "sX"], ["p2", "sX"]]
    session.put_rows("cohort", ["ctype", "csubtype"], rows)
    out = await SurveySourcesTool(session).execute()
    assert 'NEAR-HIERARCHY: "ctype" ~ parent of "csubtype"' in out
    assert "purity 0.95" in out and "BOTH grains" in out
    assert "never noise to clean away" in out


async def test_survey_wide_table_prioritizes_related_columns(session, tmp_path, monkeypatch):
    from raven.plugin.data_agent import survey as survey_mod
    from raven.plugin.data_agent.survey import SurveySourcesTool

    monkeypatch.setattr(survey_mod, "_NOTES_PATH", str(tmp_path / "survey.md"))
    monkeypatch.setattr(survey_mod, "_MAX_COLUMNS_PER_TABLE", 4)
    # Two stem-related columns sit far apart in physical order; the filler
    # columns in between would consume the whole profiling budget.
    cols = ["filler_alpha", "batch_code", "filler_beta", "filler_gamma", "batch_label"]
    session.put_rows("wide", cols, [[f"v{i}{j}" for j in range(5)] for i in range(3)])
    out = await SurveySourcesTool(session).execute()
    notes = (tmp_path / "survey.md").read_text()
    assert "profiled 4 of 5 columns" in out
    assert "NOT profiled" in out
    assert "- batch_code:" in notes and "- batch_label:" in notes
    assert "SYNONYM GROUP" in notes


async def test_survey_fd_probe_reaches_name_related_pair_on_wide_table(session, tmp_path, monkeypatch):
    from raven.plugin.data_agent import survey as survey_mod
    from raven.plugin.data_agent.survey import SurveySourcesTool

    monkeypatch.setattr(survey_mod, "_NOTES_PATH", str(tmp_path / "survey.md"))
    monkeypatch.setattr(survey_mod, "_FD_MAX_PAIRS_PER_TABLE", 1)
    # Physical order pairs col0 with everything; only name-relatedness ordering
    # spends the single probe on the (type, subtype) pair that matters.
    rows = []
    for i in range(9):
        rows.append([f"noise{i}", f"pad{i % 3}", f"t{i % 3}", f"s{i}"])
    session.put_rows("clin", ["noise_col", "pad_col", "lesion_type", "lesion_subtype"], rows)
    await SurveySourcesTool(session).execute()
    notes = (tmp_path / "survey.md").read_text()
    assert "HIERARCHY" in notes
    assert '"lesion_type"' in notes and '"lesion_subtype"' in notes


async def test_survey_flags_cross_cutting_same_concept_columns(session, tmp_path, monkeypatch):
    from raven.plugin.data_agent import survey as survey_mod
    from raven.plugin.data_agent.survey import SurveySourcesTool

    monkeypatch.setattr(survey_mod, "_NOTES_PATH", str(tmp_path / "survey.md"))
    # Two same-concept encodings that partition rows differently: neither a
    # hierarchy (violations both ways) nor aliases (distinct counts differ).
    rows = [
        ["Alpha type", "100/1"],
        ["Alpha type", "200/2"],
        ["Beta type", "100/1"],
        ["Beta type", "300/3"],
        ["Gamma type", "400/4"],
        ["Gamma type", "300/3"],
    ]
    session.put_rows("lesion", ["lesion_name", "lesion_code"], rows)
    out = await SurveySourcesTool(session).execute()
    assert "CROSS-CUTTING" in out
    assert "NOT a hierarchy and NOT aliases" in out
    assert "real group of its column and none may be dropped" in out
    assert "NEAR-HIERARCHY" not in out

    # A deployment can splice a note into the line through the fragment slot.
    noted = apply_overrides(
        GENERAL, {"fragments": {"survey.cross_cutting_note": " (odd-shaped values too)"}}
    )
    out = await SurveySourcesTool(session, profile=noted).execute()
    assert "real group of its column (odd-shaped values too) and none" in out


async def test_survey_reports_phrasing_families_in_free_text(session, tmp_path, monkeypatch):
    from raven.plugin.data_agent import survey as survey_mod
    from raven.plugin.data_agent.survey import SurveySourcesTool

    monkeypatch.setattr(survey_mod, "_NOTES_PATH", str(tmp_path / "survey.md"))
    # One subject written three ways: a pattern matching one shape drops the rest.
    rows = []
    for i in range(4):
        rows.append([f"In the Alpha study dataset, subject SUBJ-{i:03d} (UUID 1a2b3c4d-99-{i}) is enrolled."])
    for i in range(4):
        rows.append([f"Subject SUBJ-{i:03d} (UUID 5e6f7a8b-11-{i}) is enrolled in the Alpha study."])
    for i in range(4):
        rows.append([f"Subject SUBJ-{i:03d}, registered under UUID 9c8d7e6f-22-{i}, belongs to the Alpha study cohort."])
    session.put_rows("subjects", ["subject_note"], rows)
    out = await SurveySourcesTool(session, profile=_LOW_ROWS).execute()
    assert 'PHRASING FAMILIES in free-text column "subject_note"' in out
    assert "must match EVERY shape" in out
    assert "3 distinct shapes over 12 rows" in out


async def test_survey_skips_phrasing_families_for_short_values(session, tmp_path, monkeypatch):
    from raven.plugin.data_agent import survey as survey_mod
    from raven.plugin.data_agent.survey import SurveySourcesTool

    monkeypatch.setattr(survey_mod, "_NOTES_PATH", str(tmp_path / "survey.md"))
    session.put_rows("codes", ["state"], [[f"state_{i % 4}"] for i in range(12)])
    out = await SurveySourcesTool(session, profile=_LOW_ROWS).execute()
    assert "PHRASING FAMILIES" not in out


async def test_survey_skips_phrasing_families_for_free_text(session, tmp_path, monkeypatch):
    from raven.plugin.data_agent import survey as survey_mod
    from raven.plugin.data_agent.survey import SurveySourcesTool

    monkeypatch.setattr(survey_mod, "_NOTES_PATH", str(tmp_path / "survey.md"))
    # Genuinely free text: about as many shapes as rows, so a shape census would
    # be noise. Measured on real document columns, which sit near a 1:1 ratio.
    rows = [
        [f"unique opening line {i} followed by prose that shares no template with the others at all"]
        for i in range(40)
    ]
    session.put_rows("docs", ["body"], rows)
    out = await SurveySourcesTool(session, profile=_LOW_ROWS).execute()
    assert "PHRASING FAMILIES" not in out


async def test_submit_answer_requires_measured_self_imposed_exclusions(tmp_path):
    from raven.plugin.data_agent.deliver import SubmitAnswerTool
    import json as _json

    question = tmp_path / "q.txt"
    question.write_text("Name the five highest-scoring entries.", encoding="utf-8")
    tool = SubmitAnswerTool(
        answer_path=str(tmp_path / "answer.txt"),
        question_path=str(question),
        profile=STRICT,
    )
    base = dict(
        rows=["alpha 9", "beta 8", "gamma 7", "delta 6", "epsilon 5"],
        selected_reading="rank entries by the stored score, all entries eligible",
        reason="score column populated on 2451 of 2451 entries; runner-up margin 21 points",
    )
    out = await tool.execute(
        **base, excluded_populations=["dropped entries whose name looked synthetic"]
    )
    assert "REFUSED" in out and "declared without measurement" in out
    assert not (tmp_path / "answer.txt").exists()

    measured = dict(
        **base,
        excluded_populations=[
            "dropped 10941 entries whose name looked synthetic; keeping them changes "
            "the top 5 to a different set of 5"
        ],
    )
    out = await tool.execute(**measured)
    assert "REFUSED" in out and "two bases" in out
    assert not (tmp_path / "answer.txt").exists()

    # Same rows again: the basis check gets one final pass, then stands down.
    out = await tool.execute(**measured)
    assert "REFUSED" in out and "same rows the last refusal returned" in out

    out = await tool.execute(**measured)
    assert "wrote 5 rows" in out
    record = _json.loads((tmp_path / "adjudication.json").read_text())
    assert record["excluded_populations"] and "10941" in record["excluded_populations"][0]


async def test_submit_answer_exclusion_basis_gate_fires_once_and_warns_in_general(tmp_path):
    from raven.plugin.data_agent.deliver import SubmitAnswerTool

    args = dict(
        rows=["alpha 9", "beta 8"],
        selected_reading="rank by stored score, synthetic-looking names dropped",
        rejected_readings=["keep all rows: top becomes zeta 11"],
        reason="dropped 120 of 890 rows; with them the leader flips, margin 2 vs 21",
        excluded_populations=["dropped 120 rows with concatenated identifiers; answer flips"],
    )
    question = tmp_path / "q.txt"
    question.write_text("Name the two highest-scoring entries.", encoding="utf-8")
    strict = SubmitAnswerTool(
        answer_path=str(tmp_path / "a.txt"), question_path=str(question), profile=STRICT
    )
    out = await strict.execute(**args)
    assert "REFUSED" in out and "store itself classifies them as" in out
    out = await strict.execute(**args)
    assert "REFUSED" in out and "same rows the last refusal returned" in out
    out = await strict.execute(**args)
    assert "wrote 2 rows" in out

    general = SubmitAnswerTool(answer_path=str(tmp_path / "b.txt"), question_path=str(question))
    out = await general.execute(**args)
    assert "wrote 2 rows" in out and "ADVISORY" in out and "two bases" in out


async def test_submit_answer_refuses_half_of_a_two_part_question(tmp_path):
    from raven.plugin.data_agent.deliver import SubmitAnswerTool

    question = tmp_path / "q.txt"
    question.write_text(
        "Identify the repository with the most contributors and the file in it "
        "that changed most often.",
        encoding="utf-8",
    )
    args = dict(
        selected_reading="rank repositories by distinct contributor count",
        rejected_readings=["commit-weighted contributors: same winner, 3211 commits"],
        reason="contributor column populated on 8140 of 8140 rows; margin 214 over runner-up",
        excluded_populations=[],
    )
    tool = SubmitAnswerTool(
        answer_path=str(tmp_path / "a.txt"), question_path=str(question), profile=STRICT
    )
    out = await tool.execute(rows=["octo/hello-world"], **args)
    assert "REFUSED" in out and "more than one thing to report" in out
    assert not (tmp_path / "a.txt").exists()

    # Fires once: a resubmission the agent stands behind goes through.
    out = await tool.execute(rows=["octo/hello-world"], **args)
    assert "wrote 1 rows" in out

    # A row that carries both fields never trips it.
    fresh = SubmitAnswerTool(
        answer_path=str(tmp_path / "b.txt"), question_path=str(question), profile=STRICT
    )
    out = await fresh.execute(rows=["octo/hello-world, src/main.py (41 changes)"], **args)
    assert "wrote 1 rows" in out

    # Neither does a single-value answer to a single-request question.
    single = tmp_path / "q2.txt"
    single.write_text("Which warehouse shipped the most pallets?", encoding="utf-8")
    other = SubmitAnswerTool(
        answer_path=str(tmp_path / "c.txt"), question_path=str(single), profile=STRICT
    )
    out = await other.execute(rows=["Northgate Depot"], **args)
    assert "wrote 1 rows" in out


async def test_submit_answer_refuses_a_reworded_resubmission_of_the_same_exclusion(tmp_path):
    from raven.plugin.data_agent.deliver import SubmitAnswerTool

    question = tmp_path / "q.txt"
    question.write_text("Name the three highest-scoring entries.", encoding="utf-8")
    tool = SubmitAnswerTool(
        answer_path=str(tmp_path / "a.txt"), question_path=str(question), profile=STRICT
    )
    rows = ["alpha 9", "beta 8", "gamma 7"]
    base = dict(
        rows=rows,
        selected_reading="rank by stored score, concatenated identifiers dropped",
        rejected_readings=["keeping them: top becomes zeta 11"],
        reason="dropped 120 of 890 rows; with them the leader flips, margin 2 vs 21",
    )
    out = await tool.execute(**base, excluded_populations=["dropped 120 rows with odd ids; answer flips"])
    assert "REFUSED" in out and "two bases" in out

    # Same rows, reworded justification: refused once more, and told why.
    out = await tool.execute(
        **base, excluded_populations=["removed 120 malformed-looking identifiers; answer flips"]
    )
    assert "REFUSED" in out and "same rows the last refusal returned" in out
    assert not (tmp_path / "a.txt").exists()

    # Third pass stands, whatever the agent decided -- no deadlock.
    out = await tool.execute(**base, excluded_populations=["dropped 120 rows with odd ids; answer flips"])
    assert "wrote 3 rows" in out


async def test_submit_answer_second_exclusion_refusal_defers_to_a_quoted_condition(tmp_path):
    from raven.plugin.data_agent.deliver import SubmitAnswerTool

    question = tmp_path / "q.txt"
    question.write_text(
        "Identify the busiest depot and the pallet it shipped most often, counting "
        "only pallets that carry a scan record in the scans table.",
        encoding="utf-8",
    )
    rows = ["Northgate Depot", "PAL-4471", "38 shipments"]
    base = dict(
        rows=rows,
        selected_reading="rank depots by scanned shipments",
        rejected_readings=["unscanned pallets included: 41 shipments, different pallet"],
        reason="scan join keeps 902 of 1204 rows; runner-up margin 11 shipments",
    )
    tool = SubmitAnswerTool(
        answer_path=str(tmp_path / "a.txt"), question_path=str(question), profile=STRICT
    )
    out = await tool.execute(**base, excluded_populations=["dropped 302 unscanned rows; answer changes"])
    assert "REFUSED" in out and "two bases" in out

    # Same rows, but now the entry quotes the question's own condition: the
    # second pass stands down instead of pushing a correct answer off itself.
    out = await tool.execute(
        **base,
        excluded_populations=[
            "302 rows dropped: the question says 'only pallets that carry a scan "
            "record in the scans table', and these have none"
        ],
    )
    assert "wrote 3 rows" in out


async def test_submit_answer_second_exclusion_refusal_fires_on_unquoted_rewording(tmp_path):
    from raven.plugin.data_agent.deliver import SubmitAnswerTool

    question = tmp_path / "q.txt"
    question.write_text(
        "Which packages are the top 5 most popular by star count, and their versions?",
        encoding="utf-8",
    )
    rows = ["alpha 9", "beta 8", "gamma 7"]
    base = dict(
        rows=rows,
        selected_reading="rank by stored score, concatenated identifiers dropped",
        rejected_readings=["keeping them: top becomes zeta 11"],
        reason="dropped 120 of 890 rows; with them the leader flips, margin 2 vs 21",
    )
    tool = SubmitAnswerTool(
        answer_path=str(tmp_path / "a.txt"), question_path=str(question), profile=STRICT
    )
    out = await tool.execute(**base, excluded_populations=["dropped 120 rows with odd ids; answer flips"])
    assert "REFUSED" in out and "two bases" in out

    # Reworded in the agent's own words -- including a paraphrase of the
    # question -- still gets the second pass.
    out = await tool.execute(
        **base,
        excluded_populations=[
            "120 composite identifiers removed, as required by the question's "
            "popularity metric; they carry NULL timestamps and empty hashes"
        ],
    )
    assert "REFUSED" in out and "no entry quotes the question" in out
    out = await tool.execute(**base, excluded_populations=["dropped 120 rows with odd ids; answer flips"])
    assert "wrote 3 rows" in out


async def test_submit_answer_second_exclusion_refusal_skipped_when_rows_change(tmp_path):
    from raven.plugin.data_agent.deliver import SubmitAnswerTool

    tool = SubmitAnswerTool(answer_path=str(tmp_path / "a.txt"), profile=STRICT)
    base = dict(
        selected_reading="rank by stored score",
        rejected_readings=["keeping them: top becomes zeta 11"],
        reason="dropped 120 of 890 rows; with them the leader flips, margin 2 vs 21",
        excluded_populations=["dropped 120 rows with odd ids; answer flips"],
    )
    out = await tool.execute(rows=["alpha 9", "beta 8"], **base)
    assert "REFUSED" in out and "two bases" in out
    # Recomputed answer: the refusal did its job, so it does not fire again.
    out = await tool.execute(rows=["zeta 11", "alpha 9"], **base)
    assert "wrote 2 rows" in out


def test_submit_answer_contract_names_self_imposed_exclusions():
    from raven.plugin.data_agent.deliver import SubmitAnswerTool

    tool = SubmitAnswerTool()
    desc = tool.description
    assert "subpopulation you decided to leave out" in desc
    assert "Dropping them is a reading, not cleanup" in desc
    assert "BEFORE any predicate you added" in desc
    params = tool.parameters
    assert "excluded_populations" in params["required"]
    excl = params["properties"]["excluded_populations"]["description"]
    assert "what the answer becomes without it" in excl


def test_submit_answer_contract_separates_membership_from_completeness():
    from raven.plugin.data_agent.deliver import SubmitAnswerTool

    desc = SubmitAnswerTool().description
    # The unverifiable-predicate rule must stay scoped to the column the
    # question reads, or it becomes a licence to evict sparse rows.
    assert "does not travel to any other column" in desc
    assert "Membership comes from the table the question points at" in desc
    assert "completeness opinion" in desc


# ---- calibration profiles ----

async def test_submit_answer_general_profile_warns_and_writes(tmp_path):
    from raven.plugin.data_agent.deliver import SubmitAnswerTool

    tool = SubmitAnswerTool(answer_path=str(tmp_path / "answer.txt"))
    kwargs = dict(
        rows=["7"],
        selected_reading="calendar-year window, inclusive bounds",
        reason="COUNT over the full filtered population for 2019, one aggregate over 8140 rows",
    )
    out = await tool.execute(**kwargs)
    assert "wrote 1 rows" in out and "[ADVISORY]" in out
    assert "reading families" in out and "REFUSED" not in out
    assert (tmp_path / "answer.txt").read_text() == "7\n"
    out = await tool.execute(**kwargs)
    assert "[ADVISORY]" not in out


async def test_submit_answer_gate_off_is_silent(tmp_path):
    from raven.plugin.data_agent.deliver import SubmitAnswerTool

    off = apply_overrides(GENERAL, {"gates": {"single_reading": "off"}})
    tool = SubmitAnswerTool(answer_path=str(tmp_path / "answer.txt"), profile=off)
    out = await tool.execute(
        rows=["7"],
        selected_reading="calendar-year window, inclusive bounds",
        reason="COUNT over the full filtered population for 2019, one aggregate over 8140 rows",
    )
    assert "wrote 1 rows" in out and "ADVISORY" not in out


def test_calibration_resolve_profile_layers(tmp_path):
    p = resolve_profile({})
    assert p.name == "general" and p.gate_mode("single_reading") == "warn"
    p = resolve_profile({"profile": "strict"})
    assert p.gate_mode("single_reading") == "refuse" and p.gate_mode("for_each") == "refuse"

    pack = tmp_path / "pack.yaml"
    pack.write_text(
        "profile_overrides:\n"
        "  name: pack\n"
        "  gates: {single_reading: refuse}\n"
        "  thresholds: {template_min_rows: 6}\n"
        "  lexicons:\n"
        "    extra_sentinels: ['1.23E45']\n"
        "    extra_placeholders: ['voidmark']\n"
        "  fragments:\n"
        "    profile.sentinel_examples: 'AA, BB'\n",
        encoding="utf-8",
    )
    p = resolve_profile(
        {"profile_file": str(pack), "profile_overrides": {"gates": {"for_each": "off"}}}
    )
    assert p.name == "pack"
    assert p.gate_mode("single_reading") == "refuse"
    assert p.gate_mode("for_each") == "off"
    assert "1.23e45" in p.lexicons.sentinel_values()
    assert "voidmark" in p.lexicons.placeholder_values()
    assert p.thresholds.template_min_rows == 6
    assert p.fragment("profile.sentinel_examples") == "AA, BB"

    with pytest.raises(ValueError):
        apply_overrides(GENERAL, {"bogus": {}})
    with pytest.raises(ValueError):
        apply_overrides(GENERAL, {"gates": {"no_such_gate": "warn"}})
    with pytest.raises(ValueError):
        apply_overrides(GENERAL, {"gates": {"for_each": "loud"}})
    with pytest.raises(ValueError):
        apply_overrides(GENERAL, {"thresholds": {"no_such_knob": 1}})
    with pytest.raises(ValueError):
        apply_overrides(GENERAL, {"lexicons": {"sentinels": ["x"]}})
    with pytest.raises(ValueError):
        resolve_profile({"profile": "no_such_profile"})


async def test_profile_tool_lexicon_extras_and_fragment(session):
    session.put_rows(
        "marks", ["grade"], [["zz-void"], ["zz-void"], ["good"], ["fine"]]
    )
    plain = await dat.ProfileTool(session).execute("marks", column="grade")
    assert "SENTINEL?" not in plain
    prof = apply_overrides(
        GENERAL,
        {
            "lexicons": {"extra_sentinels": ["ZZ-VOID"]},
            "fragments": {"profile.sentinel_examples": "AA, BB"},
        },
    )
    tool = dat.ProfileTool(session, profile=prof)
    assert "sentinel values (AA, BB)" in tool.description
    out = await tool.execute("marks", column="grade")
    assert "SENTINEL?" in out and "zz-void" in out


async def test_profile_tool_flags_repeated_extreme_constant(session):
    rows = [["-999"]] * 5 + [[str(i)] for i in range(1, 25)]
    session.put_rows("sensor", ["reading"], rows)
    out = await dat.ProfileTool(session).execute("sensor", column="reading")
    assert "REPEATED-EXTREME?" in out and "x5" in out
    session.put_rows("sensor_ok", ["reading"], [[str(i)] for i in range(1, 31)])
    ok = await dat.ProfileTool(session).execute("sensor_ok", column="reading")
    assert "REPEATED-EXTREME?" not in ok


async def test_resolve_entities_bracketed_values_never_merge(session):
    from raven.plugin.data_agent.entity_resolution import ResolveEntitiesTool

    session.put_rows(
        "takes",
        ["title"],
        [["[Void Take]"], ["[Void Take]"], ["Void Take"], ["void take"]],
    )
    tool = ResolveEntitiesTool(session)
    out = await tool.execute(table="takes", column="title", output_table="take_map")
    assert "3 distinct values -> 2 entities" in out
    assert "look like null placeholders" in out


async def test_resolve_entities_bracket_feature_pins_old_semantics(session):
    from raven.plugin.data_agent.entity_resolution import ResolveEntitiesTool

    session.put_rows(
        "takes_pinned",
        ["title"],
        [["[Void Take]"], ["[Void Take]"], ["Void Take"], ["void take"]],
    )
    pinned = apply_overrides(GENERAL, {"features": {"bracketed_placeholders": False}})
    tool = ResolveEntitiesTool(session, profile=pinned)
    out = await tool.execute(table="takes_pinned", column="title", output_table="pin_map")
    # With the structural rule off, the bracketed spelling shares its
    # normalized key with the plain ones and merges as it did pre-R28.
    assert "3 distinct values -> 1 entities" in out


async def test_profile_repeated_extreme_feature_switch(session):
    rows = [["-999"]] * 5 + [[str(i)] for i in range(1, 25)]
    session.put_rows("sensor_pinned", ["reading"], rows)
    pinned = apply_overrides(GENERAL, {"features": {"repeated_extreme": False}})
    out = await dat.ProfileTool(session, profile=pinned).execute(
        "sensor_pinned", column="reading"
    )
    assert "REPEATED-EXTREME?" not in out


# ---- files source kind ----

async def test_files_source_registers_views_and_joins(stores, tmp_path):
    sales, _ = stores
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "orders extra.csv").write_text(
        "order_id,region\n1,east\n2,west\n3,east\n", encoding="utf-8"
    )
    (drop / "notes.jsonl").write_text('{"k": 1, "v": "a"}\n{"k": 2, "v": "b"}\n', encoding="utf-8")
    (drop / "readme.md").write_text("not data", encoding="utf-8")
    session = DataAgentSession(
        workspace=tmp_path,
        sources=(
            SourceSpec(alias="sales", kind="duckdb", ref=str(sales)),
            SourceSpec(alias="inbox", kind="files", ref=str(drop)),
        ),
    )
    out = await dat.SqlTool(session).execute("SELECT COUNT(*) AS n FROM inbox.orders_extra")
    assert "attached inbox.orders_extra" in out and "3 rows" in out
    assert "attached inbox.notes" in out
    assert "readme" not in out
    joined = await dat.SqlTool(session).execute(
        "SELECT COUNT(*) AS n FROM sales.orders o JOIN inbox.orders_extra e "
        "ON o.order_id = e.order_id"
    )
    assert "rows=1" in joined and "3" in joined


async def test_files_source_is_visible_to_survey(tmp_path, monkeypatch):
    from raven.plugin.data_agent import survey as survey_mod
    from raven.plugin.data_agent.survey import SurveySourcesTool

    monkeypatch.setattr(survey_mod, "_NOTES_PATH", str(tmp_path / "survey.md"))
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "stock.csv").write_text(
        "sku,qty\n" + "".join(f"s{i},{i * 3}\n" for i in range(8)), encoding="utf-8"
    )
    session = DataAgentSession(
        workspace=tmp_path, sources=(SourceSpec(alias="inbox", kind="files", ref=str(drop)),)
    )
    out = await SurveySourcesTool(session).execute()
    assert "stock" in out and "8 rows" in out


async def test_files_source_degrades_on_malformed_csv(tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    # Invalid UTF-8 (a latin-1 byte): the strict attempt fails and the ladder
    # falls through to the latin-1 encoding, named in the attach note.
    (drop / "legacy.csv").write_bytes(b"name,val\ncaf\xe9,1\nbar,2\n")
    (drop / "ragged.csv").write_text("a,b\n1,2\n3,4,5\n6,7\n", encoding="utf-8")
    session = DataAgentSession(
        workspace=tmp_path, sources=(SourceSpec(alias="inbox", kind="files", ref=str(drop)),)
    )
    out = await dat.SqlTool(session).execute("SELECT COUNT(*) FROM inbox.legacy")
    assert "attached inbox.legacy" in out and "latin-1 fallback" in out
    assert "attached inbox.ragged" in out


async def test_files_source_reports_unsupported_dir(tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "readme.md").write_text("nothing tabular", encoding="utf-8")
    session = DataAgentSession(
        workspace=tmp_path, sources=(SourceSpec(alias="inbox", kind="files", ref=str(drop)),)
    )
    out = await dat.SqlTool(session).execute("SELECT 1")
    assert "no supported data files" in out


def test_parse_sources_accepts_files_kind(tmp_path):
    from raven.plugin.data_agent.session import parse_sources

    specs = parse_sources({"sources": [{"alias": "inbox", "kind": "files", "path": "/data/drop"}]})
    assert specs[0].kind == "files" and specs[0].ref == "/data/drop"
