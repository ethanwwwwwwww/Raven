"""Evolution loop — wires the orchestration mechanisms together.

See ``raven.evolver.loop.main.EvolutionLoop`` for the end-to-end
entrypoint. Module structure mirrors the spec §13.3 algorithmic
skeleton plus the §13.9 cold-start mechanism added 2026-06-05.
"""

from raven.evolver.loop.main import (
    EvolutionLoop,
    EvolutionRoundResult,
    EvalFn,
    EvalResult,
    JudgeFn,
    PatchSelector,
)

__all__ = [
    "EvolutionLoop",
    "EvolutionRoundResult",
    "EvalFn",
    "EvalResult",
    "JudgeFn",
    "PatchSelector",
]
