"""Completion gates end-to-end through the loop: injection, counting, silence.

The pure decision helpers are unit-tested in ``test_completion_gates``; this
covers the wiring that a real run depends on -- the nudge actually reaching the
message list, the counter reaching TurnOutcome, and the gate staying silent
once the turn has edited a file. Waiting for a live task to finish empty is not
a reliable way to exercise the firing path.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.providers.base import LLMProvider, LLMResponse


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


class _TextOnlyProvider(LLMProvider):
    """Answers with text and never calls a tool -- a turn that edits nothing."""

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.calls = 0
        self.seen: list[list[dict]] = []

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.calls += 1
        self.seen.append(list(messages))
        return LLMResponse(content="The repository already satisfies this.", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


def _agent(workspace: Path, provider: LLMProvider) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=10,
        restrict_to_workspace=True,
        interactive=False,
    )


def _only_empty_diff_gate(monkeypatch) -> None:
    for switch in ("RAVEN_REQUIRE_REAL_TEST_EVIDENCE", "RAVEN_VERIFY_BEFORE_COMPLETE"):
        monkeypatch.setenv(switch, "off")
    monkeypatch.delenv("RAVEN_GATE_EMPTY_DIFF", raising=False)


@pytest.mark.asyncio
async def test_finishing_without_edits_injects_the_nudge_once_and_counts_it(workspace, monkeypatch):
    _only_empty_diff_gate(monkeypatch)
    provider = _TextOnlyProvider()
    agent = _agent(workspace, provider)

    _final, _tools, messages, outcome = await agent._run_agent_loop(
        [{"role": "user", "content": "Make sure the frobnicator handles empty input."}],
    )

    injected = [
        m for m in messages
        if m.get("role") == "user" and "without any observed repository modification" in str(m.get("content"))
    ]
    assert len(injected) == 1
    assert outcome.gate_triggers == {"empty_diff": 1}
    assert provider.calls == 2  # the answer, then one more after the nudge


@pytest.mark.asyncio
async def test_gate_is_silent_when_switched_off(workspace, monkeypatch):
    for switch in (
        "RAVEN_REQUIRE_REAL_TEST_EVIDENCE",
        "RAVEN_VERIFY_BEFORE_COMPLETE",
        "RAVEN_GATE_EMPTY_DIFF",
    ):
        monkeypatch.setenv(switch, "off")
    provider = _TextOnlyProvider()
    agent = _agent(workspace, provider)

    _f, _t, messages, outcome = await agent._run_agent_loop([{"role": "user", "content": "hi"}])

    assert not any("without any observed repository modification" in str(m.get("content")) for m in messages)
    assert outcome.gate_triggers == {}
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_interactive_sessions_do_not_arm_the_gate_by_default(workspace, monkeypatch):
    for switch in (
        "RAVEN_REQUIRE_REAL_TEST_EVIDENCE",
        "RAVEN_VERIFY_BEFORE_COMPLETE",
        "RAVEN_GATE_EMPTY_DIFF",
    ):
        monkeypatch.delenv(switch, raising=False)
    provider = _TextOnlyProvider()
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=10,
        restrict_to_workspace=True,
        interactive=True,
    )

    _f, _t, messages, outcome = await agent._run_agent_loop([{"role": "user", "content": "what does this do?"}])

    assert outcome.gate_triggers == {}
    assert provider.calls == 1
