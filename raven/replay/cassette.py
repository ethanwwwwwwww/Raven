"""The recorded material and the cursors that hand it out.

One cassette holds one past task: the ordered LLM responses, the ordered tool
results, and the CLI prompts that drove them. Replay walks both sequences by
position — position, not content hashing, is the real key, because the recorded
ids flow back into the request the loop builds next, so an id match proves
nothing that the order does not already prove.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ENV_CASSETTE = "RAVEN_REPLAY_CASSETTE"
ENV_TRANSCRIPT_DIR = "RAVEN_REPLAY_TRANSCRIPT_DIR"


class ReplayError(RuntimeError):
    """Replay hit something it must not paper over."""


class ReplayExhaustedError(ReplayError):
    """The agent asked for more recorded work than the cassette holds."""


class ReplayToolMismatchError(ReplayError):
    """The agent called a different tool than the recording did at this point."""


@dataclass
class LlmEntry:
    idx: int
    kind: str
    response: dict[str, Any]


@dataclass
class ToolEntry:
    idx: int
    tool_call_id: str | None
    name: str | None
    content: str


@dataclass
class ReplayCassette:
    root: Path
    meta: dict[str, Any]
    llm_calls: list[LlmEntry]
    tool_results: list[ToolEntry]
    invocations: list[dict[str, Any]]
    transcript_dir: Path | None = None
    _llm_cursor: int = field(default=0, init=False)
    _tool_cursor: int = field(default=0, init=False)

    @property
    def instance_id(self) -> str:
        return self.meta.get("instance_id", self.root.name)

    @property
    def model(self) -> str | None:
        return self.meta.get("model")

    def next_llm(self) -> LlmEntry:
        if self._llm_cursor >= len(self.llm_calls):
            raise self._fatal(
                ReplayExhaustedError(
                    f"[{self.instance_id}] the loop asked for LLM call #{self._llm_cursor + 1} "
                    f"but the cassette holds only {len(self.llm_calls)}; the change made the agent "
                    "run longer than the recording did"
                )
            )
        entry = self.llm_calls[self._llm_cursor]
        self._llm_cursor += 1
        self._write_progress()
        return entry

    def next_tool(self) -> ToolEntry:
        if self._tool_cursor >= len(self.tool_results):
            raise self._fatal(
                ReplayExhaustedError(
                    f"[{self.instance_id}] the loop asked for tool result #{self._tool_cursor + 1} "
                    f"but the cassette holds only {len(self.tool_results)}"
                )
            )
        entry = self.tool_results[self._tool_cursor]
        self._tool_cursor += 1
        self._write_progress()
        return entry

    def fatal(self, error: ReplayError) -> ReplayError:
        """Public entry for other replay modules to record a fatal mismatch."""
        return self._fatal(error)

    def _fatal(self, error: ReplayError) -> ReplayError:
        """Record the error on disk, then hand it back to be raised.

        Raven's turn scheduler catches everything a turn raises, reports it on an
        internal event and returns -- so the CLI exits 0 with no output and a
        replay failure would look like a quiet success. The marker file survives
        that, which is what the batch driver actually checks.
        """
        if self.transcript_dir is not None:
            payload = {"instance_id": self.instance_id, "error": type(error).__name__, "message": str(error), **self.progress()}
            (self.transcript_dir / "fatal.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        return error

    def progress(self) -> dict[str, list[int]]:
        return {
            "llm": [self._llm_cursor, len(self.llm_calls)],
            "tool": [self._tool_cursor, len(self.tool_results)],
        }

    def fully_consumed(self) -> bool:
        return self._llm_cursor == len(self.llm_calls) and self._tool_cursor == len(self.tool_results)

    def _write_progress(self) -> None:
        if self.transcript_dir is None:
            return
        payload = {"instance_id": self.instance_id, **self.progress(), "fully_consumed": self.fully_consumed()}
        target = self.transcript_dir / "consumed.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(target)

    def _restore_progress(self) -> None:
        """Pick the cursors back up where the previous process left them.

        One recorded task can span several CLI invocations (the harness re-runs
        ``raven agent`` to continue an unfinished task), and each invocation is
        its own process while the recorded calls are one continuous sequence. So
        the cursors have to outlive the process, or invocation two would be fed
        the answers to invocation one all over again.
        """
        if self.transcript_dir is None:
            return
        saved = self.transcript_dir / "consumed.json"
        if not saved.exists():
            return
        payload = json.loads(saved.read_text(encoding="utf-8"))
        llm_done, llm_total = payload["llm"]
        tool_done, tool_total = payload["tool"]
        if (llm_total, tool_total) != (len(self.llm_calls), len(self.tool_results)):
            raise ReplayError(
                f"[{self.instance_id}] the progress file in {self.transcript_dir} was left by a run over a "
                f"different cassette ({llm_total} calls / {tool_total} tool results, this one has "
                f"{len(self.llm_calls)} / {len(self.tool_results)}); use a fresh transcript directory"
            )
        self._llm_cursor = llm_done
        self._tool_cursor = tool_done


_LOADED: dict[str, ReplayCassette] = {}


def _read_jsonl(path: Path, instance_id: str) -> list[dict[str, Any]]:
    if not path.exists():
        raise ReplayError(f"[{instance_id}] cassette is missing {path.name}")
    rows = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ReplayError(f"[{instance_id}] {path.name} line {lineno} is not valid JSON: {exc}") from exc
    return rows


def load_cassette(path: str | Path, transcript_dir: str | Path | None = None) -> ReplayCassette:
    """Load a cassette, memoized per absolute path.

    Memoized on purpose: the provider and the tool registry must share one set
    of cursors, otherwise each would drain its own copy and the consumed check
    would pass on a half-driven replay.
    """
    root = Path(path).expanduser().resolve()
    key = str(root)
    if key in _LOADED:
        return _LOADED[key]

    if not (root / "meta.json").exists():
        raise ReplayError(f"{root} is not a cassette directory (no meta.json)")
    meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    instance_id = meta.get("instance_id", root.name)

    calls = [LlmEntry(idx=r.get("idx", i), kind=r.get("kind", "main"), response=r["response"]) for i, r in enumerate(_read_jsonl(root / "llm_calls.jsonl", instance_id))]
    tools = [
        ToolEntry(idx=r.get("idx", i), tool_call_id=r.get("tool_call_id"), name=r.get("name"), content=r.get("content", ""))
        for i, r in enumerate(_read_jsonl(root / "tool_results.jsonl", instance_id))
    ]
    invocations_path = root / "invocations.json"
    if not invocations_path.exists():
        raise ReplayError(f"[{instance_id}] cassette is missing invocations.json")
    invocations = json.loads(invocations_path.read_text(encoding="utf-8"))

    if transcript_dir is None:
        env_dir = os.environ.get(ENV_TRANSCRIPT_DIR)
        transcript_dir = Path(env_dir) if env_dir else None
    if transcript_dir is not None:
        transcript_dir = Path(transcript_dir)
        transcript_dir.mkdir(parents=True, exist_ok=True)

    cassette = ReplayCassette(
        root=root,
        meta=meta,
        llm_calls=calls,
        tool_results=tools,
        invocations=invocations,
        transcript_dir=transcript_dir,
    )
    cassette._restore_progress()
    _LOADED[key] = cassette
    return cassette


def replay_enabled() -> bool:
    return bool(os.environ.get(ENV_CASSETTE))


def active_cassette() -> ReplayCassette | None:
    """The cassette named by the environment, or None when replay is off."""
    path = os.environ.get(ENV_CASSETTE)
    return load_cassette(path) if path else None


def _reset_for_tests() -> None:
    _LOADED.clear()
