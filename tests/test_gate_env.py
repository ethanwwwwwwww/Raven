"""Guardrail switch semantics: on by default for one-shot task runs.

The gates used to be opt-in, which produced a silent-no-op failure: a full
evaluation ran believing the guardrails were active while the environment never
set them, and the whole round's conclusion had to be discarded. They are now on
by default wherever the loop runs a one-shot task (``raven agent --message ...``,
which is what an evaluation harness drives), and off by default in interactive
sessions, where a completion is usually an answer rather than a code change.
An explicit environment value overrides the default in either direction, which
is how an ablation arm turns a single gate off.
"""

import pytest

from raven.agent.loop import completion_gates as cg

SWITCH = "RAVEN_TEST_SWITCH"


def test_unset_follows_the_call_site_default(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)
    assert cg.gate_enabled(SWITCH, default=True) is True
    assert cg.gate_enabled(SWITCH, default=False) is False


def test_default_is_on_when_the_call_site_says_nothing(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)
    assert cg.gate_enabled(SWITCH) is True


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "anything"])
def test_explicit_truthy_forces_on_even_in_interactive_mode(monkeypatch, raw):
    monkeypatch.setenv(SWITCH, raw)
    assert cg.gate_enabled(SWITCH, default=False) is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", " off ", "disabled"])
def test_explicit_opt_out_forces_off_even_in_task_mode(monkeypatch, raw):
    monkeypatch.setenv(SWITCH, raw)
    assert cg.gate_enabled(SWITCH, default=True) is False
