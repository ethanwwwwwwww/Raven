"""Unit tests for the wall-clock budget reminder."""

from __future__ import annotations

import pytest

from raven.agent.loop.time_budget import CHECKPOINT_ENV, DEADLINE_ENV, TimeBudgetReminder


def test_from_env_absent_or_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DEADLINE_ENV, raising=False)
    assert TimeBudgetReminder.from_env(100.0) is None
    monkeypatch.setenv(DEADLINE_ENV, "not-a-number")
    assert TimeBudgetReminder.from_env(100.0) is None
    monkeypatch.setenv(DEADLINE_ENV, "50")
    assert TimeBudgetReminder.from_env(100.0) is None


def test_reminders_fire_once_per_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEADLINE_ENV, "1000")
    tb = TimeBudgetReminder.from_env(0.0)
    assert tb is not None
    assert tb.poll(100.0) is None
    note = tb.poll(500.0)
    assert note is not None and "8 minutes" in note
    assert tb.poll(510.0) is None
    assert tb.poll(750.0) is not None
    assert tb.poll(900.0) is not None
    assert tb.poll(990.0) is None


def test_jump_past_multiple_thresholds_fires_single_note() -> None:
    tb = TimeBudgetReminder(1000.0, 0.0)
    note = tb.poll(950.0)
    assert note is not None
    assert tb.poll(999.0) is None


def test_remaining_clamps_at_zero() -> None:
    tb = TimeBudgetReminder(1000.0, 0.0)
    note = tb.poll(1200.0)
    assert note is not None and "About 0 minutes" in note


def test_checkpoint_ask_only_from_second_threshold() -> None:
    tb = TimeBudgetReminder(1000.0, 0.0, checkpoint=True)
    first = tb.poll(500.0)
    assert first is not None and "Checkpoint your work" not in first
    second = tb.poll(750.0)
    assert second is not None and "Checkpoint your work" in second
    third = tb.poll(900.0)
    assert third is not None and "Checkpoint your work" in third


def test_jump_past_thresholds_reports_the_highest_crossed() -> None:
    tb = TimeBudgetReminder(1000.0, 0.0, checkpoint=True)
    note = tb.poll(950.0)
    assert note is not None and "Checkpoint your work" in note


def test_checkpoint_ask_is_off_by_default() -> None:
    tb = TimeBudgetReminder(1000.0, 0.0)
    for now in (500.0, 750.0, 900.0):
        note = tb.poll(now)
        assert note is not None and "Checkpoint your work" not in note


def test_from_env_reads_the_checkpoint_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEADLINE_ENV, "1000")
    monkeypatch.delenv(CHECKPOINT_ENV, raising=False)
    tb = TimeBudgetReminder.from_env(0.0)
    assert tb is not None and tb.checkpoint is False
    monkeypatch.setenv(CHECKPOINT_ENV, "1")
    tb = TimeBudgetReminder.from_env(0.0)
    assert tb is not None and tb.checkpoint is True
