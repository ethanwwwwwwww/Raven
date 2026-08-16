"""The replay provider: recorded answers, no network, no credentials."""

from __future__ import annotations

import json

import pytest

from raven.providers.base import GenerationSettings
from raven.replay import cassette as cas
from raven.replay.provider import ReplayProvider, build_replay_provider

from tests.test_replay_cassette import _write


@pytest.fixture(autouse=True)
def _fresh_cassette_cache():
    cas._reset_for_tests()
    yield
    cas._reset_for_tests()


def _provider(tmp_path, **kw):
    return ReplayProvider(cas.load_cassette(_write(tmp_path), transcript_dir=tmp_path / "t"), **kw)


class TestChat:
    async def test_returns_recorded_tool_calls_with_recorded_ids(self, tmp_path):
        """The ids have to survive: the loop pairs tool results by them, and
        minting new ones would pull a random source into a run that must be
        byte-for-byte reproducible."""
        response = await _provider(tmp_path).chat([{"role": "user", "content": "go"}])
        assert response.finish_reason == "tool_calls"
        assert [(c.id, c.name, c.arguments) for c in response.tool_calls] == [("c1", "list_dir", {"path": "/w"})]
        assert response.reasoning_content == "look"

    async def test_replays_usage_verbatim(self, tmp_path):
        """Usage drives the loop's compaction trigger. Invent it and compaction
        fires on a different turn, after which the trajectory is a different
        trajectory."""
        response = await _provider(tmp_path).chat([{"role": "user", "content": "go"}])
        assert response.usage == {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105}

    async def test_advances_in_recorded_order(self, tmp_path):
        provider = _provider(tmp_path)
        first = await provider.chat([{"role": "user", "content": "go"}])
        second = await provider.chat([{"role": "user", "content": "go"}])
        assert first.has_tool_calls and second.content == "TASK_COMPLETE"

    async def test_running_past_the_recording_raises(self, tmp_path):
        provider = _provider(tmp_path)
        await provider.chat([])
        await provider.chat([])
        with pytest.raises(cas.ReplayExhaustedError):
            await provider.chat([])

    async def test_needs_no_credentials(self, tmp_path):
        provider = _provider(tmp_path)
        assert provider.api_key is None and provider.api_base is None
        assert provider.get_default_model() == "deepseek/x"


class TestRetryLadderIsSkipped:
    async def test_chat_with_retry_consumes_exactly_one_entry(self, tmp_path):
        provider = _provider(tmp_path)
        await provider.chat_with_retry([{"role": "user", "content": "go"}])
        assert provider.cassette.progress()["llm"] == [1, 2]

    async def test_generation_defaults_are_resolved_like_the_real_ladder(self, tmp_path):
        """The effective sampling parameters land in the transcript, so they are
        themselves a diff target and must not read 4096/0.7 by accident."""
        provider = _provider(tmp_path, generation=GenerationSettings(temperature=1.0, max_tokens=65536))
        await provider.chat_with_retry([{"role": "user", "content": "go"}])
        recorded = json.loads((tmp_path / "t" / "calls.jsonl").read_text().splitlines()[0])
        assert recorded["request"]["max_tokens"] == 65536
        assert recorded["request"]["temperature"] == 1.0


class TestStreamingPathAgrees:
    async def test_single_delta_carries_the_same_response(self, tmp_path):
        """``chat_stream`` is inherited from the base provider, which wraps
        ``chat()``. This pins that the two paths cannot drift: the deltas must
        reconstruct exactly what ``chat()`` returns."""
        streamed = [d async for d in _provider(tmp_path).chat_stream([{"role": "user", "content": "go"}])]
        assert len(streamed) == 1
        delta = streamed[0]
        assert delta.reasoning_content == "look"
        assert delta.usage == {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105}
        calls = delta.tool_call_delta["tool_calls"]
        assert [(c["id"], c["function"]["name"], json.loads(c["function"]["arguments"])) for c in calls] == [
            ("c1", "list_dir", {"path": "/w"})
        ]


class TestTranscript:
    async def test_records_the_full_request_and_response(self, tmp_path):
        provider = _provider(tmp_path)
        tools = [{"type": "function", "function": {"name": "list_dir", "description": "d"}}]
        await provider.chat([{"role": "system", "content": "S"}, {"role": "user", "content": "go"}], tools=tools)

        line = json.loads((tmp_path / "t" / "calls.jsonl").read_text().splitlines()[0])
        assert line["idx"] == 0
        assert [m["role"] for m in line["request"]["messages"]] == ["system", "user"]
        assert line["request"]["tools"] == tools
        assert line["response"]["content"] is None

    async def test_appends_one_line_per_call(self, tmp_path):
        provider = _provider(tmp_path)
        await provider.chat([])
        await provider.chat([])
        assert len((tmp_path / "t" / "calls.jsonl").read_text().splitlines()) == 2


class TestBuild:
    def test_takes_generation_settings_from_the_recorded_config(self, tmp_path):
        class _Defaults:
            model = "deepseek/x"
            temperature = 1.0
            max_tokens = 65536
            reasoning_effort = "high"

        class _Agents:
            defaults = _Defaults()

        class _Config:
            agents = _Agents()

        provider = build_replay_provider(_write(tmp_path), _Config())
        assert provider.generation.temperature == 1.0
        assert provider.generation.max_tokens == 65536
        assert provider.generation.reasoning_effort == "high"
        assert provider.get_default_model() == "deepseek/x"
