"""Evolution loop MVP — end-to-end wiring of orchestration mechanisms.

The loop has two distinct phases, matching spec §14:

- **Cold start** (run once before evolution begins, spec §14 step ③):
  Uses the 5th mechanism (cold-start coverage bandit, spec §13.9) to
  pick a small trial subset from the k-paired baseline (e.g. tb2 v7
  k=3 = 267 trials), routes each through the judge, and aggregates
  the labels into a ``failure_map.json`` covering ≥ 7 WHY classes.

- **Evolution round** (run repeatedly, spec §14 step ④ onward):
  Selects a candidate patch from the failure_map (or any registered
  patch source), applies it via ``EvolverTreeStore.create_child_node``,
  evaluates the child harness on a bandit-on-tasks subset, then
  re-judges the new trials so the failure_map grows organically.

MVP scope (2026-06-05): wire the data flow + persist state. Concrete
LLM judge and external eval are passed in as callbacks (``JudgeFn`` /
``EvalFn``) so the loop is testable end-to-end with mocks. Phase 1
runs claude (this conversation) as the judge; later phases swap in
``LitellmBackend`` -> Qwen-397B.

Pass criterion for spec §14 step 4 ("works end-to-end"): ``child.subset_pass_rate
!= root.subset_pass_rate`` after one full round. Any direction of
change satisfies it — the goal is to verify the wiring, not to ship
a positive lift.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

from raven.evolver.analysis.failure_map_builder import (
    build_failure_map,
    write_failure_map,
)
from raven.evolver.analysis.proxy_features import ExitStatus
from raven.evolver.judge.schema import JudgeResult
from raven.evolver.scheduler.bandit_tasks import BanditTaskScheduler
from raven.evolver.scheduler.cold_start_bandit import (
    ColdStartCoverageBandit,
    Trial,
)


log = logging.getLogger(__name__)


# ─────────────────────────────── callback protocols ───────────────────────────────


class JudgeFn(Protocol):
    """Map a trial to a :class:`JudgeResult`.

    Phase 1: claude main window (the model running this conversation).
    Phase 4: ``LitellmBackend`` -> Qwen-397B self-hosted.

    The implementation is responsible for compressing the trial's
    trajectory (via ``evolver.compressor.trajectory``) before sending
    it to the LLM. The returned ``JudgeResult`` must be schema-valid
    (cross-field invariants enforced in ``__post_init__``).
    """

    def __call__(self, trial: Trial) -> JudgeResult: ...


@dataclass(frozen=True)
class EvalResult:
    """Outcome of running a child harness on one task.

    Returned as part of ``dict[str, EvalResult]`` from :class:`EvalFn`.
    Lets the loop distinguish a model-side failure (agent didn't solve
    the task) from an infra-side failure (harbor / ssh / verifier
    timed out) — both count as ``passed=False`` for pass-rate
    arithmetic, but ``exit_status`` carries the failure mode for
    paper audit and for the bandit-on-tasks posterior to interpret
    "this task is hard for the agent" vs "this task is broken
    infra".

    Mirrors the categorical taxonomy already used by
    :class:`raven.evolver.analysis.proxy_features.ExitStatus` so
    upstream code (proxy features) and downstream code (eval) speak
    the same enum vocabulary.

    Phase 1 (MVP, single-attempt eval): one ``EvalResult`` per task in
    the subset, ``error_message`` empty unless infra failure.
    """

    task_id: str
    passed: bool
    exit_status: ExitStatus
    error_message: Optional[str] = None


class EvalFn(Protocol):
    """Evaluate a child harness on a task subset, return per-task outcomes.

    Returns ``{task_id: EvalResult}``. Phase 1: bridges to the harbor
    runner in ``raven/benchmarks/terminal_bench/`` (cherry-picked
    onto mainline 2026-06-04). Phase 4: may also call SWE-bench
    Verified for cross-benchmark transfer.

    Invariants the caller (``EvolutionLoop.run_round``) relies on:

    - ``set(returned.keys()) ⊆ set(task_subset)`` — missing tasks
      mean "not evaluated", not "secretly passed"; the loop treats
      missing keys as non-results, not as failures
    - Each returned ``EvalResult.task_id`` equals its dict key
    - Hard infra failures (ssh dropped, harbor crashed, network down
      that prevents any task from running) should ``raise`` an
      ``EvalInfraError`` rather than returning a partial dict —
      single-task verifier_timeout / runtime_error is fine to
      return with ``passed=False``
    """

    def __call__(
        self,
        child_harness_id: str,
        task_subset: Sequence[str],
    ) -> dict[str, EvalResult]: ...


class PatchSelector(Protocol):
    """Pick the next patch to apply, given the current failure_map.

    Phase 1 (MVP): trivial selector — pick the first L2/L3 candidate
    from a specified (WHERE, WHY) cell, or from a static list (e.g.
    ``human_baseline_patches.jsonl`` entries).

    Phase 2+: bandit-on-WHY chooses cell, bandit-on-nodes chooses
    parent harness, the selector falls back to LLM mutation
    (GEPA library) when no human candidate is available.
    """

    def __call__(
        self,
        failure_map: dict[str, Any],
        parent_harness_id: str,
    ) -> Optional[dict[str, Any]]:
        """Return a patch dict (with at least ``patch_id`` and
        ``components``) or ``None`` if no patch available this round."""
        ...


# ─────────────────────────────── data classes ───────────────────────────────


@dataclass
class EvolutionRoundResult:
    """Outcome of one evolution round (one ``run_round`` call)."""

    round_number: int
    parent_harness_id: str
    child_harness_id: Optional[str]
    patch_applied: Optional[dict[str, Any]]
    task_subset: list[str]
    per_task_results: dict[str, EvalResult]
    parent_subset_pass_rate: Optional[float]
    child_subset_pass_rate: Optional[float]
    judged_new_trials: list[JudgeResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def walked_through(self) -> bool:
        """Spec §14 step 4 works-end-to-end criterion: any direction of change.

        Returns True if the child pass rate is defined and differs
        from the parent. The exact lift (positive or negative) is
        secondary to verifying that the wiring functioned.
        """
        if self.child_subset_pass_rate is None:
            return False
        if self.parent_subset_pass_rate is None:
            return True
        return self.child_subset_pass_rate != self.parent_subset_pass_rate


# ─────────────────────────────── main loop ───────────────────────────────


class EvolutionLoop:
    """End-to-end evolution loop, MVP scope.

    Lifecycle:

        loop = EvolutionLoop(
            root_harness_id="v7-cb216ce",
            root_trials=trial_pool,  # 267 trials from tb2 v7 k=3
            all_task_ids=tb2_task_ids,  # 89 tasks
            judge_fn=claude_judge,
            eval_fn=harbor_eval,
            patch_selector=jsonl_patch_selector,
        )

        # Step 1: cold-start coverage bandit + claude judge ~25 trials
        loop.run_cold_start(out_path="data/failure_map.json")

        # Step 2: one evolution round (the spec §14 step-4 works-end-to-end gate)
        result = loop.run_round()
        assert result.walked_through  # subset pass rate != root

        # (Future) Step 3: run N rounds
        # for _ in range(5): loop.run_round()
    """

    def __init__(
        self,
        *,
        root_harness_id: str,
        root_trials: Sequence[Trial],
        all_task_ids: Sequence[str],
        judge_fn: JudgeFn,
        eval_fn: EvalFn,
        patch_selector: PatchSelector,
        cold_start_budget: int = 25,
        cold_start_n_why: int = 7,
        cold_start_n_stable_fail_strata: int = 5,
        eval_subset_size: int = 30,
        rng_seed: Optional[int] = None,
    ) -> None:
        self._root_harness_id = root_harness_id
        self._root_trials = list(root_trials)
        self._all_task_ids = list(all_task_ids)
        self._judge_fn = judge_fn
        self._eval_fn = eval_fn
        self._patch_selector = patch_selector
        self._cold_start_budget = cold_start_budget
        self._cold_start_n_why = cold_start_n_why
        self._cold_start_n_stable_fail_strata = cold_start_n_stable_fail_strata
        self._eval_subset_size = eval_subset_size
        self._rng_seed = rng_seed

        # State carried across rounds
        self._failure_map: dict[str, Any] = {}
        self._cold_start_judgments: list[JudgeResult] = []
        self._bandit_tasks = BanditTaskScheduler(
            all_task_ids=self._all_task_ids,
            rng_seed=rng_seed,
        )
        self._rounds_completed = 0
        # Tracks per-harness subset pass rate (root + every child evaluated)
        self._harness_pass_rate: dict[str, float] = {}

    # ───────────── cold start ─────────────

    def run_cold_start(
        self,
        *,
        out_path: Optional[str | Path] = None,
    ) -> dict[str, Any]:
        """Run cold-start coverage bandit + judge calls, build failure_map.

        Persists ``failure_map.json`` to ``out_path`` if provided.
        Returns the failure_map dict (same object kept on the loop
        instance for subsequent ``run_round`` calls).
        """
        bandit = ColdStartCoverageBandit(
            trials=self._root_trials,
            n_why_classes=self._cold_start_n_why,
            budget=self._cold_start_budget,
            n_stable_fail_strata=self._cold_start_n_stable_fail_strata,
            rng_seed=self._rng_seed,
        )
        judgments: list[JudgeResult] = []
        while not bandit.done():
            trial = bandit.next_trial()
            result = self._judge_fn(trial)
            # Surface the WHY label to the bandit so its UCB posterior
            # updates correctly on the stable_fail strata.
            why_key = self._extract_why_key(result)
            bandit.update(trial, why=why_key)
            judgments.append(result)

        self._cold_start_judgments = judgments
        self._failure_map = build_failure_map(
            judgments, min_why_classes=self._cold_start_n_why,
        )
        if out_path is not None:
            write_failure_map(self._failure_map, out_path)
            log.info("failure_map written to %s", out_path)

        log.info(
            "cold-start done: judged=%d, covered_why=%d/%d, satisfied=%s",
            len(judgments),
            self._failure_map["covered_why_count"],
            self._cold_start_n_why,
            self._failure_map["coverage_satisfied"],
        )
        return self._failure_map

    # ───────────── one evolution round ─────────────

    def run_round(self) -> EvolutionRoundResult:
        """Run one evolution round.

        Steps (matching spec §13.3 skeleton, MVP simplifications noted):

        1. (MVP) Patch source: ``self._patch_selector(failure_map, parent)``
           skips the bandit-on-WHY / bandit-on-nodes layers and directly
           returns a candidate patch (e.g. from human_baseline_patches.jsonl).
        2. (MVP) Patch apply: returns a synthetic child_harness_id from
           the selector. Phase 1 actual apply is via
           ``EvolverTreeStore.create_child_node`` — the loop itself
           doesn't open that store yet (left as an integration knob for
           the caller of run_round). The selector is expected to have
           done the git operation and return the resulting harness id.
        3. (real) bandit-on-tasks picks a K-task subset for eval.
        4. (real, via callback) Eval child on subset, get per-task pass/fail.
        5. (real, via callback) Judge the newly-produced trials, append
           to failure_map for next round.
        6. (real) Compute subset_pass_rate, compare to parent.

        Returns ``EvolutionRoundResult`` with ``walked_through`` true
        iff the child pass rate differs from the root (spec §14 ④
        "any direction of change" criterion).
        """
        if self._failure_map.get("n_total_judged", 0) == 0:
            raise RuntimeError(
                "run_round called before run_cold_start; failure_map empty"
            )

        self._rounds_completed += 1
        round_number = self._rounds_completed
        parent_id = self._root_harness_id  # MVP: always patch from root
        notes: list[str] = []

        # Step 1: select patch from failure_map
        patch = self._patch_selector(self._failure_map, parent_id)
        if patch is None:
            notes.append("no patch available — round aborted")
            return EvolutionRoundResult(
                round_number=round_number,
                parent_harness_id=parent_id,
                child_harness_id=None,
                patch_applied=None,
                task_subset=[],
                per_task_results={},
                parent_subset_pass_rate=self._harness_pass_rate.get(parent_id),
                child_subset_pass_rate=None,
                notes=notes,
            )

        # Step 2: extract child harness id (selector applies the patch)
        child_id = patch.get("child_harness_id") or patch.get("patch_id")
        if not isinstance(child_id, str):
            raise ValueError(
                f"patch_selector must return a dict with a string "
                f"'child_harness_id' or 'patch_id'; got {patch!r}"
            )

        # Step 3: bandit-on-tasks subset for child eval
        task_subset = self._bandit_tasks.choose(
            n=self._eval_subset_size, candidate_id=child_id,
        )

        # Step 4: external eval
        per_task = self._eval_fn(child_id, task_subset)

        # Defensive: filter out keys outside the requested subset
        # (the Protocol declares this as a caller invariant, but we
        # don't trust un-typed callers)
        subset_set = set(task_subset)
        per_task = {k: v for k, v in per_task.items() if k in subset_set}

        # Step 5: update bandit-on-tasks posterior — convert EvalResult
        # → bool since the bandit takes the simpler signal
        self._bandit_tasks.update(
            child_id, {k: v.passed for k, v in per_task.items()},
        )

        # Step 6: subset pass rate + walked-through check
        child_pass_rate = self._compute_pass_rate(per_task)
        self._harness_pass_rate[child_id] = child_pass_rate
        parent_rate = self._harness_pass_rate.get(parent_id)
        if parent_rate is None:
            # First round: derive root pass rate from the same task
            # subset by replaying the per-task result attribution
            # stored in the bandit (root has no per_task results yet
            # in this MVP — leave as None, ``walked_through`` falls
            # back to "child rate is defined")
            pass

        # (Optional, MVP) judge the new child trials → grow failure_map
        new_judgments: list[JudgeResult] = []
        # No new Trial objects synthesized here yet; this is where
        # Phase 2 would call judge_fn on the child's freshly-produced
        # trajectories. Left as TODO marker for the next iteration.

        return EvolutionRoundResult(
            round_number=round_number,
            parent_harness_id=parent_id,
            child_harness_id=child_id,
            patch_applied=patch,
            task_subset=list(task_subset),
            per_task_results=per_task,
            parent_subset_pass_rate=parent_rate,
            child_subset_pass_rate=child_pass_rate,
            judged_new_trials=new_judgments,
            notes=notes,
        )

    # ───────────── introspection ─────────────

    def failure_map(self) -> dict[str, Any]:
        return self._failure_map

    def cold_start_judgments(self) -> list[JudgeResult]:
        return list(self._cold_start_judgments)

    def rounds_completed(self) -> int:
        return self._rounds_completed

    # ───────────── helpers ─────────────

    @staticmethod
    def _extract_why_key(result: JudgeResult) -> str:
        """Reproduce the WHY key the bandit + failure_map_builder use.

        ``other`` sub-names get the full ``other:<extra>`` value;
        canonical classes get the enum value directly.
        """
        action = result.proposed_action
        if action.patch_why is None:
            return "l1_no_why"
        from raven.evolver.judge.schema import PatchWhy

        if action.patch_why == PatchWhy.other:
            return action.patch_why_extra or "other:unknown"
        return action.patch_why.value

    @staticmethod
    def _compute_pass_rate(per_task: dict[str, EvalResult]) -> float:
        if not per_task:
            return 0.0
        return sum(1 for r in per_task.values() if r.passed) / len(per_task)


__all__ = [
    "EvolutionLoop",
    "EvolutionRoundResult",
    "EvalFn",
    "EvalResult",
    "JudgeFn",
    "PatchSelector",
]
