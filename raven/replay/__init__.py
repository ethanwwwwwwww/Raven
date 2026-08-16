"""Keyless transcript replay: drive the real agent loop off a recorded cassette.

Every LLM call and tool result comes from a cassette extracted from a past
evaluation run, so a prompt or loop change can be diffed against a baseline in
seconds instead of a full evaluation. Nothing here runs unless
``RAVEN_REPLAY_CASSETTE`` is set.

The one rule that makes the result trustworthy: never improvise. A missing,
extra, or out-of-order recorded entry raises, because a replay that patches
over a gap yields a transcript that looks fine and proves nothing.
"""

from raven.replay.cassette import (
    ENV_CASSETTE,
    ENV_TRANSCRIPT_DIR,
    ReplayError,
    ReplayExhaustedError,
    ReplayToolMismatchError,
    active_cassette,
    load_cassette,
    replay_enabled,
)

__all__ = [
    "ENV_CASSETTE",
    "ENV_TRANSCRIPT_DIR",
    "ReplayError",
    "ReplayExhaustedError",
    "ReplayToolMismatchError",
    "active_cassette",
    "load_cassette",
    "replay_enabled",
]
