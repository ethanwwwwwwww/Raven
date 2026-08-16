"""The provider that answers from the cassette instead of the network.

Wrapping at the provider boundary covers every model call the agent makes --
the main loop, the compaction summary, the exhaustion synthesis, subagents --
because they all hold the same provider object. It also needs no credentials,
which is the point: the replay suite has to run for anyone, any time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from raven.providers.base import GenerationSettings, LLMProvider, LLMResponse, ToolCallRequest
from raven.replay.cassette import ReplayCassette, load_cassette


class ReplayProvider(LLMProvider):
    """Hands out recorded responses in recorded order.

    ``chat_stream`` is inherited from :class:`LLMProvider`, which wraps
    ``chat()`` into a single terminal delta -- so the streaming and
    non-streaming paths cannot drift apart here by construction.
    """

    def __init__(
        self,
        cassette: ReplayCassette,
        generation: GenerationSettings | None = None,
        default_model: str | None = None,
        transcript_dir: Path | None = None,
    ) -> None:
        super().__init__(api_key=None, api_base=None)
        self.cassette = cassette
        self.generation = generation or GenerationSettings()
        self._default_model = default_model or cassette.model or "replay-model"
        self._transcript_dir = transcript_dir or cassette.transcript_dir

    def get_default_model(self) -> str:
        return self._default_model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        entry = self.cassette.next_llm()
        self._record(
            idx=entry.idx,
            request={
                "model": model or self._default_model,
                "messages": messages,
                "tools": tools,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "reasoning_effort": reasoning_effort,
                "tool_choice": tool_choice,
            },
            response=entry.response,
        )
        return _to_response(entry.response)

    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: object = LLMProvider._SENTINEL,
        temperature: object = LLMProvider._SENTINEL,
        reasoning_effort: object = LLMProvider._SENTINEL,
        tool_choice: str | dict[str, Any] | None = None,
        fallback_models: list[str] | None = None,
    ) -> LLMResponse:
        """No retry ladder, no fallback chain, no health probe.

        Recorded responses never fail transiently, and the sleeps between
        retries would only slow the suite down. The generation defaults are
        resolved the way the real ladder resolves them, because the effective
        values land in the transcript and are themselves worth diffing.
        """
        if max_tokens is self._SENTINEL:
            max_tokens = self.generation.max_tokens
        if temperature is self._SENTINEL:
            temperature = self.generation.temperature
        if reasoning_effort is self._SENTINEL:
            reasoning_effort = self.generation.reasoning_effort
        return await self.chat(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,  # type: ignore[arg-type]
            temperature=temperature,  # type: ignore[arg-type]
            reasoning_effort=reasoning_effort,  # type: ignore[arg-type]
            tool_choice=tool_choice,
        )

    def _record(self, idx: int, request: dict[str, Any], response: dict[str, Any]) -> None:
        if self._transcript_dir is None:
            return
        self._transcript_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"idx": idx, "request": request, "response": response}, ensure_ascii=False, sort_keys=True)
        with (self._transcript_dir / "calls.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _to_response(recorded: dict[str, Any]) -> LLMResponse:
    """Rebuild the loop-facing response object from recorded fields.

    The recorded tool-call ids are reused verbatim so the loop pairs results the
    way it did on the real run; minting fresh ones would also pull in a random
    source and break byte-for-byte reproducibility.
    """
    calls = [
        ToolCallRequest(id=c["id"], name=c["name"], arguments=c.get("arguments") or {})
        for c in recorded.get("tool_calls") or []
    ]
    return LLMResponse(
        content=recorded.get("content"),
        tool_calls=calls,
        finish_reason=recorded.get("finish_reason", "stop"),
        usage=recorded.get("usage") or {},
        reasoning_content=recorded.get("reasoning_content"),
    )


def build_replay_provider(cassette_path: str | Path, config: Any = None) -> ReplayProvider:
    """Provider for the cassette at ``cassette_path``.

    Generation settings come from the recorded config when one is passed, so the
    request fields in the transcript match what the recorded run actually sent.
    """
    cassette = load_cassette(cassette_path)
    generation = GenerationSettings()
    default_model = cassette.model

    defaults = getattr(getattr(config, "agents", None), "defaults", None)
    if defaults is not None:
        generation = GenerationSettings(
            temperature=getattr(defaults, "temperature", generation.temperature),
            max_tokens=getattr(defaults, "max_tokens", generation.max_tokens),
            reasoning_effort=getattr(defaults, "reasoning_effort", generation.reasoning_effort),
        )
        default_model = getattr(defaults, "model", default_model) or default_model

    return ReplayProvider(cassette, generation=generation, default_model=default_model)
