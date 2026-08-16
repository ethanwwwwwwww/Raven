"""The real agent loop, driven end to end off a cassette, with no network.

This is the test that proves the mechanism: a genuine ``AgentLoop`` runs its
genuine turn -- context assembly, tool dispatch, message bookkeeping -- and the
only thing replaced is where model answers and tool results come from.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.loop.main import AgentLoop
from raven.replay import cassette as cas
from raven.replay.normalize import mask_untrusted_nonces
from raven.replay.provider import ReplayProvider
from raven.replay.tool_registry import ReplayToolRegistry
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


GATE_ENV_VARS = (
    "RAVEN_REQUIRE_REAL_TEST_EVIDENCE",
    "RAVEN_VERIFY_BEFORE_COMPLETE",
    "RAVEN_GATE_STALE",
    "RAVEN_GATE_RED",
    "RAVEN_GATE_EMPTY_DIFF",
)


@pytest.fixture(autouse=True)
def _replay_env(tmp_path, monkeypatch):
    cas._reset_for_tests()
    monkeypatch.setenv(cas.ENV_TRANSCRIPT_DIR, str(tmp_path / "transcript"))
    # Pinned off here because these tests are about the replay plumbing, and the
    # gates default to on for non-interactive runs -- a short synthetic
    # trajectory that never runs tests would be held back by them. Real replays
    # pin these to whatever the recorded run exported.
    for var in GATE_ENV_VARS:
        monkeypatch.setenv(var, "0")
    yield
    cas._reset_for_tests()


def _cassette(tmp_path, calls, tools):
    root = tmp_path / "cassette"
    root.mkdir(parents=True, exist_ok=True)
    (root / "meta.json").write_text(json.dumps({"instance_id": "demo__demo-1", "model": "stub"}))
    (root / "llm_calls.jsonl").write_text("".join(json.dumps(c) + "\n" for c in calls))
    (root / "tool_results.jsonl").write_text("".join(json.dumps(t) + "\n" for t in tools))
    (root / "invocations.json").write_text(json.dumps([{"idx": 0, "text": "fix the bug"}]))
    return root


def _call(idx, *, content=None, tool_calls=(), prompt_tokens=100):
    return {
        "idx": idx,
        "kind": "main",
        "response": {
            "content": content,
            "reasoning_content": None,
            "tool_calls": [{"id": cid, "name": name, "arguments": args} for cid, name, args in tool_calls],
            "finish_reason": "tool_calls" if tool_calls else "stop",
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 10, "total_tokens": prompt_tokens + 10},
        },
    }


THREE_CALL_TRAJECTORY = [
    _call(0, tool_calls=[("c1", "read_file", {"path": "a.py"})]),
    _call(1, tool_calls=[("c2", "write_file", {"path": "a.py", "content": "fixed"})], prompt_tokens=200),
    _call(2, content="Fixed the bug. TASK_COMPLETE", prompt_tokens=300),
]
THREE_CALL_TOOLS = [
    {"idx": 0, "tool_call_id": "c1", "name": "read_file", "content": "def broken(): pass"},
    {"idx": 1, "tool_call_id": "c2", "name": "write_file", "content": "Wrote 1 line to a.py"},
]


async def _run(tmp_path, monkeypatch, calls=None, tools=None) -> tuple[AgentLoop, ReplayCassetteRun]:
    root = _cassette(tmp_path, calls or THREE_CALL_TRAJECTORY, tools if tools is not None else THREE_CALL_TOOLS)
    monkeypatch.setenv(cas.ENV_CASSETTE, str(root))

    workspace = tmp_path / "ws"
    workspace.mkdir()
    cassette = cas.load_cassette(root)
    agent = AgentLoop(
        provider=ReplayProvider(cassette),
        workspace=workspace,
        model="stub",
        max_iterations=10,
        restrict_to_workspace=True,
        interactive=False,
    )
    out = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text=cassette.invocations[0]["text"],
        ),
        session_key="s1",
    )
    return agent, ReplayCassetteRun(agent=agent, cassette=cassette, output=out)


class ReplayCassetteRun:
    def __init__(self, agent, cassette, output):
        self.agent = agent
        self.cassette = cassette
        self.output = output


class TestEndToEnd:
    async def test_loop_reaches_the_recorded_ending(self, tmp_path, monkeypatch):
        _, run = await _run(tmp_path, monkeypatch)
        assert run.output is not None
        assert "TASK_COMPLETE" in run.output[0]

    async def test_the_cassette_is_fully_consumed(self, tmp_path, monkeypatch):
        """Leftover entries would mean the loop did less work than the recording:
        the replay's own way of catching a change that shortens a trajectory."""
        _, run = await _run(tmp_path, monkeypatch)
        assert run.cassette.progress() == {"llm": [3, 3], "tool": [2, 2]}
        assert run.cassette.fully_consumed()

    async def test_the_registry_is_the_replay_one_and_nothing_was_written(self, tmp_path, monkeypatch):
        agent, _ = await _run(tmp_path, monkeypatch)
        assert isinstance(agent.tools, ReplayToolRegistry)
        assert list((agent.workspace).iterdir()) == []

    async def test_real_tools_are_still_registered_so_schemas_stay_real(self, tmp_path, monkeypatch):
        agent, _ = await _run(tmp_path, monkeypatch)
        names = {d["function"]["name"] for d in agent.tools.get_definitions()}
        assert {"read_file", "write_file", "exec"} <= names

    async def test_session_history_pairs_every_recorded_tool_call(self, tmp_path, monkeypatch):
        agent, _ = await _run(tmp_path, monkeypatch)
        messages = agent.sessions.get_or_create("s1").messages
        tool_ids = [m.get("tool_call_id") for m in messages if m.get("role") == "tool"]
        assert tool_ids == ["c1", "c2"]

    async def test_transcript_holds_one_line_per_recorded_call(self, tmp_path, monkeypatch):
        await _run(tmp_path, monkeypatch)
        lines = (tmp_path / "transcript" / "calls.jsonl").read_text().splitlines()
        assert len(lines) == 3
        first = json.loads(lines[0])
        assert first["request"]["messages"][0]["role"] == "system"
        assert "read_file" in {t["function"]["name"] for t in first["request"]["tools"]}

    async def test_consumed_file_is_left_for_the_driver(self, tmp_path, monkeypatch):
        await _run(tmp_path, monkeypatch)
        payload = json.loads((tmp_path / "transcript" / "consumed.json").read_text())
        assert payload["fully_consumed"] is True
        assert payload["instance_id"] == "demo__demo-1"


class TestDivergenceIsLoud:
    async def test_a_shorter_cassette_raises_instead_of_passing(self, tmp_path, monkeypatch):
        """If the recording runs out mid-trajectory the run must fail, not end
        early with a transcript that looks complete."""
        with pytest.raises(cas.ReplayExhaustedError):
            await _run(tmp_path, monkeypatch, calls=THREE_CALL_TRAJECTORY[:2])

    async def test_a_different_recorded_tool_raises(self, tmp_path, monkeypatch):
        swapped = [
            {"idx": 0, "tool_call_id": "c1", "name": "exec", "content": "whatever"},
            THREE_CALL_TOOLS[1],
        ]
        with pytest.raises(cas.ReplayToolMismatchError, match="diverged"):
            await _run(tmp_path, monkeypatch, tools=swapped)


class TestGuardrailsStayLive:
    async def test_an_enabled_gate_lengthens_the_trajectory_and_is_reported(self, tmp_path, monkeypatch):
        """Guardrails run for real under replay -- which is the point, since
        tuning them is what this harness exists for. Here the evidence gate
        refuses an ending that never ran tests and asks for one more turn; with
        no recorded answer left, that shows up as exhaustion rather than as a
        quietly different result."""
        monkeypatch.setenv("RAVEN_REQUIRE_REAL_TEST_EVIDENCE", "1")
        with pytest.raises(cas.ReplayExhaustedError, match="LLM call #4"):
            await _run(tmp_path, monkeypatch)


class TestDeterminism:
    async def test_two_runs_produce_the_same_transcript(self, tmp_path, monkeypatch):
        """Byte-for-byte equality across runs is the property the whole diff
        workflow rests on. The clock, the workspace path and the session state
        are the three sources of drift, and all three are pinned here."""
        transcripts = []
        for i in range(2):
            cas._reset_for_tests()
            run_dir = tmp_path / f"run{i}"
            monkeypatch.setenv(cas.ENV_TRANSCRIPT_DIR, str(run_dir / "transcript"))
            monkeypatch.setenv(cas.ENV_CASSETTE, str(_cassette(run_dir, THREE_CALL_TRAJECTORY, THREE_CALL_TOOLS)))
            await _run_in(tmp_path / "shared-ws", run_dir, monkeypatch)
            transcripts.append(mask_untrusted_nonces((run_dir / "transcript" / "calls.jsonl").read_text()))

        assert transcripts[0] == transcripts[1]

    async def test_the_fence_nonce_is_the_only_thing_that_moves(self, tmp_path, monkeypatch):
        """Guards the mask above: it must be needed for a real reason and cover
        the whole gap, so a future drift cannot hide behind it."""
        raw = []
        for i in range(2):
            cas._reset_for_tests()
            run_dir = tmp_path / f"raw{i}"
            monkeypatch.setenv(cas.ENV_TRANSCRIPT_DIR, str(run_dir / "transcript"))
            monkeypatch.setenv(cas.ENV_CASSETTE, str(_cassette(run_dir, THREE_CALL_TRAJECTORY, THREE_CALL_TOOLS)))
            await _run_in(tmp_path / "ws-shared", run_dir, monkeypatch)
            raw.append((run_dir / "transcript" / "calls.jsonl").read_text())

        assert raw[0] != raw[1]
        assert mask_untrusted_nonces(raw[0]) == mask_untrusted_nonces(raw[1])


def _clear_session_state(workspace: Path) -> None:
    """Drop the persisted session for this workspace.

    Sessions live outside the workspace and outlive a run, so a second replay
    over the same workspace would resume the first one's history and build a
    different request. The batch driver gets this for free by giving each run its
    own home directory; a same-process rerun has to do it by hand.
    """
    import shutil

    from raven.config.paths import get_workspace_state_dir

    shutil.rmtree(get_workspace_state_dir(workspace, "sessions"), ignore_errors=True)


async def _run_in(workspace: Path, run_dir: Path, monkeypatch) -> None:
    from datetime import datetime

    workspace.mkdir(exist_ok=True)
    _clear_session_state(workspace)
    cassette = cas.load_cassette(run_dir / "cassette")
    agent = AgentLoop(
        provider=ReplayProvider(cassette),
        workspace=workspace,
        model="stub",
        max_iterations=10,
        restrict_to_workspace=True,
        interactive=False,
        now_fn=lambda: datetime(2026, 1, 2, 3, 4, 5),
    )
    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="fix the bug",
        ),
        session_key="s1",
    )
