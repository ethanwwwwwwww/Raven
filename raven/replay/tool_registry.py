"""Tool registry that answers from the cassette instead of touching the machine.

A subclass rather than a wrapper: registration, schema rendering and name repair
all stay exactly as they are, so tool definitions remain real -- editing a tool
description has to show up in the replayed request, that is half the point.
Only execution is swapped out.
"""

from __future__ import annotations

import os
from typing import Any

from raven.agent.tools.base import ToolOutput
from raven.agent.tools.registry import ToolRegistry
from raven.replay.cassette import (
    ENV_CASSETTE,
    ReplayCassette,
    ReplayToolMismatchError,
    load_cassette,
)

# Tools whose recorded output cannot be replayed positionally and whose absence
# would silently change the trajectory instead of failing it.
_WRITE_TOOL_HINTS = ("write_file", "edit_file", "apply_patch", "multi_edit")


class ReplayToolRegistry(ToolRegistry):
    """Returns the recorded result for each call, in recorded order.

    A name mismatch is a finding, not an error to smooth over: it means the
    change under test made the agent call something different at this point,
    which is exactly what the replay exists to surface.
    """

    def __init__(self, cassette: ReplayCassette) -> None:
        super().__init__()
        self.cassette = cassette
        self.workspace_dirty = False

    async def execute(self, name: str, params: dict[str, Any]) -> str:
        entry = self.cassette.next_tool()
        canonical = self.canonical_name(name)
        # Canonicalize the recorded name too: cassettes recorded before a tool
        # rename carry the legacy alias (e.g. "find" for today's "glob"), and
        # that is the same tool, not a trajectory divergence.
        if entry.name and canonical != self.canonical_name(entry.name):
            raise self.cassette.fatal(
                ReplayToolMismatchError(
                    f"[{self.cassette.instance_id}] tool call #{entry.idx + 1} diverged: "
                    f"the recording ran {entry.name!r}, this run asked for {canonical!r} "
                    f"(raw name {name!r}, arguments {sorted(params)}); "
                    "the change altered which tool the agent reaches for"
                )
            )
        if any(hint in (entry.name or "") for hint in _WRITE_TOOL_HINTS) and not (entry.content or "").startswith("Error"):
            # The completion gates ask git whether the workspace changed. Nothing
            # is really written here, so that probe needs this flag to stay
            # truthful about the recorded run instead of always answering "clean".
            self.workspace_dirty = True
        return ToolOutput(entry.content)


def tool_registry_for_env() -> ToolRegistry:
    """A replay registry when a cassette is configured, otherwise the real one."""
    path = os.environ.get(ENV_CASSETTE)
    if not path:
        return ToolRegistry()
    return ReplayToolRegistry(load_cassette(path))
