"""Cassette loading and the twin cursors that drive a keyless replay.

The cursors are the whole safety story: a replay that quietly returns a wrong
entry produces a plausible transcript that proves nothing, so every mismatch
between recorded and requested work has to raise instead.
"""

from __future__ import annotations

import json

import pytest

from raven.replay import cassette as cas


def _write(tmp_path, calls=None, tools=None, meta=None, invocations=None):
    root = tmp_path / "demo__demo-1"
    root.mkdir(parents=True, exist_ok=True)
    calls = (
        calls
        if calls is not None
        else [
            {
                "idx": 0,
                "kind": "main",
                "response": {
                    "content": None,
                    "reasoning_content": "look",
                    "tool_calls": [{"id": "c1", "name": "list_dir", "arguments": {"path": "/w"}}],
                    "finish_reason": "tool_calls",
                    "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105},
                },
            },
            {
                "idx": 1,
                "kind": "main",
                "response": {
                    "content": "TASK_COMPLETE",
                    "reasoning_content": None,
                    "tool_calls": [],
                    "finish_reason": "stop",
                    "usage": {"prompt_tokens": 200, "completion_tokens": 3, "total_tokens": 203},
                },
            },
        ]
    )
    tools = tools if tools is not None else [{"idx": 0, "tool_call_id": "c1", "name": "list_dir", "content": "setup.py"}]
    meta = meta if meta is not None else {"instance_id": "demo__demo-1", "model": "deepseek/x", "schema_version": 1}
    invocations = invocations if invocations is not None else [{"idx": 0, "text": "do the thing"}]

    (root / "meta.json").write_text(json.dumps(meta))
    (root / "llm_calls.jsonl").write_text("".join(json.dumps(c) + "\n" for c in calls))
    (root / "tool_results.jsonl").write_text("".join(json.dumps(t) + "\n" for t in tools))
    (root / "invocations.json").write_text(json.dumps(invocations))
    (root / "raven_config.replay.json").write_text(json.dumps({"agents": {"defaults": {"model": "deepseek/x"}}}))
    return root


class TestLoading:
    def test_loads_calls_tools_and_meta(self, tmp_path):
        c = cas.load_cassette(_write(tmp_path))
        assert c.instance_id == "demo__demo-1"
        assert c.model == "deepseek/x"
        assert len(c.llm_calls) == 2 and len(c.tool_results) == 1
        assert c.invocations == [{"idx": 0, "text": "do the thing"}]

    def test_same_path_yields_one_shared_object(self, tmp_path):
        """The provider and the tool registry must share one set of cursors —
        two copies would each drain their own half and the consumed check
        would pass on a half-driven replay."""
        root = _write(tmp_path)
        assert cas.load_cassette(root) is cas.load_cassette(str(root))

    def test_missing_directory_is_loud(self, tmp_path):
        with pytest.raises(cas.ReplayError, match="not a cassette"):
            cas.load_cassette(tmp_path / "nope")


class TestCursors:
    def test_llm_entries_come_back_in_order(self, tmp_path):
        c = cas.load_cassette(_write(tmp_path))
        assert c.next_llm().response["finish_reason"] == "tool_calls"
        assert c.next_llm().response["content"] == "TASK_COMPLETE"

    def test_exhausted_llm_script_names_the_instance_and_counts(self, tmp_path):
        c = cas.load_cassette(_write(tmp_path))
        c.next_llm()
        c.next_llm()
        with pytest.raises(cas.ReplayExhaustedError) as exc:
            c.next_llm()
        msg = str(exc.value)
        assert "demo__demo-1" in msg and "3" in msg and "2" in msg

    def test_exhausted_tool_script_is_loud(self, tmp_path):
        c = cas.load_cassette(_write(tmp_path))
        c.next_tool()
        with pytest.raises(cas.ReplayExhaustedError):
            c.next_tool()

    def test_progress_reports_both_cursors(self, tmp_path):
        c = cas.load_cassette(_write(tmp_path))
        c.next_llm()
        assert c.progress() == {"llm": [1, 2], "tool": [0, 1]}

    def test_fully_consumed_only_when_both_cursors_drain(self, tmp_path):
        c = cas.load_cassette(_write(tmp_path))
        c.next_llm()
        c.next_tool()
        assert not c.fully_consumed()
        c.next_llm()
        assert c.fully_consumed()


class TestProgressFile:
    def test_progress_is_written_for_the_driver_to_check(self, tmp_path):
        """The driver reads this after the process exits: leftover entries mean
        the agent did less work than it did on the recorded run, which is a
        divergence the exit code alone would not reveal."""
        out = tmp_path / "t"
        out.mkdir()
        c = cas.load_cassette(_write(tmp_path), transcript_dir=out)
        c.next_llm()
        assert json.loads((out / "consumed.json").read_text()) == {
            "instance_id": "demo__demo-1",
            "llm": [1, 2],
            "tool": [0, 1],
            "fully_consumed": False,
        }


class TestCursorsSurviveTheProcess:
    """One recorded task can span several CLI invocations, and each invocation is
    its own process while the recorded calls are one continuous sequence. Without
    resume, invocation two would be served invocation one's answers again -- which
    is exactly what happened the first time a real two-invocation task was
    replayed: 32 of 38 calls consumed and the material silently misaligned.
    """

    def test_a_later_process_picks_up_where_the_first_stopped(self, tmp_path):
        root = _write(tmp_path)
        out = tmp_path / "t"
        first = cas.load_cassette(root, transcript_dir=out)
        first.next_llm()
        first.next_tool()

        cas._reset_for_tests()
        resumed = cas.load_cassette(root, transcript_dir=out)
        assert resumed.progress() == {"llm": [1, 2], "tool": [1, 1]}
        assert resumed.next_llm().response["content"] == "TASK_COMPLETE"
        assert resumed.fully_consumed()

    def test_a_progress_file_from_another_cassette_is_refused(self, tmp_path):
        """A stale transcript directory would silently start the replay midway,
        so the mismatch has to be an error rather than a best guess."""
        out = tmp_path / "t"
        out.mkdir()
        (out / "consumed.json").write_text(json.dumps({"instance_id": "other", "llm": [5, 99], "tool": [1, 7]}))
        with pytest.raises(cas.ReplayError, match="different cassette"):
            cas.load_cassette(_write(tmp_path), transcript_dir=out)

    def test_a_fresh_transcript_directory_starts_at_zero(self, tmp_path):
        c = cas.load_cassette(_write(tmp_path), transcript_dir=tmp_path / "fresh")
        assert c.progress() == {"llm": [0, 2], "tool": [0, 1]}
