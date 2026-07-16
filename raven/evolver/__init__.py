"""Harness self-evolution subsystem.

Implements the budget-bounded harness evolver described in
``docs/specs/2026-05-27-evolver-pipeline.md``. Four orthogonal
mechanisms compose to make harness self-evolution feasible on a
single 8×A800 box:

- ``scheduler.bandit_tasks``  — best-arm task selection (~5-10× saving)
- ``scheduler.bandit_nodes``  — Thompson tree search over candidates
- ``scheduler.bandit_why``    — quality-diversity over pathology classes
- ``tree``                    — Git-backed harness version tree
- ``judge``                   — LLM judge with L1/L2/L3 + (WHERE,WHY) output
- ``analysis``                — offline trajectory mining helpers

Status: P1 prerequisite (Volc empty-content bug) still pending — see
``docs/specs/2026-05-27-p1-parallel-work.md`` for the parallel work
that does not depend on a clean baseline.
"""
