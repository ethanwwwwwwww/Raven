"""The run/status/finalize state machine over durable artifacts.

Resume model: work is proven by artifacts, not by process state.

- phase 1 (cold start): proof = vanilla trial result files. Resume fills
  only the missing trials.
- phase 2 (rounds): proof = journal/rounds.jsonl. Resume replays completed
  rounds (loop.run's built-in journal replay) and continues.
- phase 3 (unseal): proof = the ``unsealed_at`` stamp in run_meta.json.
  Unsealing is one-way — a stamped run refuses to resume (--force overrides,
  marking any further rounds as retention-invalid is on the caller).

``status`` never reads the sealed directory: while a run is resumable, test
numbers must stay invisible (SOP §0) — the only path to them is natural
termination or an explicit ``finalize``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

from raven.evolver.launch.config import RunSpec, RunSpecError, load_run_spec
from raven.evolver.launch.contract import BenchBundle, LaunchContext
from raven.evolver.launch.models import build_role_call_fns, describe_models
from raven.evolver.launch.registry import load_bench
from raven.evolver.launch.state import RunMeta, atomic_write_json, load_json_or


def _say(msg: str) -> None:
    print(f"[evolve] {msg}", flush=True)


def _load_spec(config_path: str, smoke: bool) -> RunSpec:
    try:
        return load_run_spec(config_path, smoke=smoke)
    except RunSpecError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _build_bundle(spec: RunSpec, *, with_models: bool) -> BenchBundle:
    try:
        models = build_role_call_fns(spec.models) if with_models else {
            "driver": None, "design": None, "verdict": None,
        }
    except ValueError as exc:
        print(f"models error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    build = load_bench(spec.bench)
    try:
        return build(LaunchContext(spec=spec, models=models, smoke=spec.smoke))
    except ValueError as exc:
        print(f"bench setup error ({spec.bench}): {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _note_defaulted_base(spec: RunSpec) -> None:
    if not spec.base_sha_defaulted:
        return
    _say(f"base_sha not set — using repo HEAD {spec.base_sha[:12]} "
         "(pin base_sha in the yaml to freeze the root explicitly)")
    proc = subprocess.run(
        ["git", "-C", str(spec.repo_root), "status", "--porcelain",
         "--untracked-files=no"],
        capture_output=True, text=True,
    )
    if proc.stdout.strip():
        _say("warning: repo_root has uncommitted changes — they are NOT part "
             "of the root node (evaluations check out commits, not the "
             "working tree)")


def _meta_guard(spec: RunSpec, *, force: bool) -> RunMeta:
    """Config-drift + one-way-unseal guards; returns the (possibly new) meta."""
    snapshot = spec.snapshot()
    meta = RunMeta.load(spec.work_dir)
    if meta is None:
        return RunMeta.create(spec.work_dir, snapshot)
    if meta.unsealed_at and not force:
        print(
            f"this run was unsealed at {meta.unsealed_at} "
            f"(reason: {meta.finalize_reason}); resuming would leak test "
            "numbers into decisions. Start a fresh work_dir, or pass --force "
            "to continue with retention marked invalid.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if not meta.check_config(snapshot) and not force:
        print(
            "config drift: the effective configuration differs from the one "
            "this work_dir was started with (same-regime rule, SOP §0). "
            "Candidate and control arms would no longer be comparable. "
            "Use a fresh work_dir for the new config, or --force to override.",
            file=sys.stderr,
        )
        if spec.base_sha_defaulted:
            print(
                "note: base_sha is omitted in the yaml, so it re-resolved to "
                f"the current HEAD ({spec.base_sha[:12]}); if the repo gained "
                "commits since the run started, pin base_sha to the original "
                f"root ({meta.config_snapshot.get('base_sha', '?')[:12]}) to "
                "resume.",
                file=sys.stderr,
            )
        raise SystemExit(2)
    return meta


def _run_rounds(spec: RunSpec, bundle: BenchBundle):
    from raven.evolver.orchestrator.state.journal import RoundJournal

    orch = bundle.build_orchestrator()
    journal = RoundJournal(bundle.journal_path)
    result = orch.run(bundle.root_node_id, journal=journal, root_node=bundle.root_node)
    return orch, journal, result


def _unseal_and_report(spec: RunSpec, bundle: BenchBundle, orch, records: list[dict],
                       meta: RunMeta, reason: str) -> None:
    if bundle.unseal is None:
        _say("no sealed test set configured; skipping unseal")
        meta.stamp_unsealed(reason=f"{reason} (no sealed test)")
        return
    _say("unsealing: blind-scoring deliverables on the sealed test set …")
    report = bundle.unseal(records, orch)
    atomic_write_json(Path(spec.work_dir) / "retention.json", report)
    meta.stamp_unsealed(reason=reason)
    _say(f"retention report -> {Path(spec.work_dir) / 'retention.json'}")
    for key in ("best_round", "best_node_id", "best_train", "best_test",
                "vanilla_train", "vanilla_test", "retention",
                "sealed_credited_2sigma"):
        if isinstance(report, dict) and key in report:
            _say(f"  {key}: {report[key]}")


def cmd_run(config_path: str, *, smoke: bool = False, force: bool = False) -> int:
    spec = _load_spec(config_path, smoke)
    _note_defaulted_base(spec)
    spec.work_dir.mkdir(parents=True, exist_ok=True)
    meta = _meta_guard(spec, force=force)
    meta.config_snapshot.setdefault("resolved_models", describe_models(spec.models))
    meta.save()

    bundle = _build_bundle(spec, with_models=True)

    done, total = bundle.cold_start_done(), bundle.cold_start_total
    if done < total:
        _say(f"phase 1/3 cold start: {done}/{total} trials present, running the rest …")
        try:
            bundle.run_cold_start()
        except KeyboardInterrupt:
            _say(f"interrupted during cold start "
                 f"({bundle.cold_start_done()}/{total} trials done and kept); "
                 "re-run the same command to continue")
            return 130
        done = bundle.cold_start_done()
        if done < total:
            _say(f"cold start still incomplete ({done}/{total}); re-run to retry "
                 "the missing trials")
            return 1
    _say(f"phase 1/3 cold start complete ({total} trials)")

    _say("phase 2/3 evolution rounds (interrupt any time; same command resumes)")
    _say(f"  live progress: {spec.work_dir}/findings.md (per-round log), "
         f"{spec.work_dir}/journal/rounds.jsonl (checkpoints)")
    try:
        orch, journal, result = _run_rounds(spec, bundle)
    except KeyboardInterrupt:
        _say("interrupted — completed rounds are journaled; "
             "re-run the same command to resume, `status` to inspect, "
             "`finalize` to stop here and unseal")
        return 130
    except RuntimeError as exc:
        # Environment-shaped failures (Gate0 precheck, dead endpoint) are
        # actionable messages, not tracebacks; completed work is durable.
        print(f"run stopped: {exc}", file=sys.stderr)
        print("fix the environment and re-run the same command to resume",
              file=sys.stderr)
        return 1

    for rr in result.rounds:
        _say(f"round {rr.round_index}: parent={rr.parent_id} "
             f"promoted={rr.promoted} -> {rr.next_parent_id}")
    _say(f"stopped: {result.stop_reason}; final parent: {result.final_parent_id}")

    _say("phase 3/3 unseal")
    _unseal_and_report(spec, bundle, orch, journal.load(), meta,
                       reason=result.stop_reason or "terminated")
    return 0


def cmd_check(config_path: str, *, smoke: bool = False) -> int:
    """Validate everything cheap before spending: config, models, bench setup.

    Builds the model call_fns (catches a missing claude binary / bad spec) and
    the bench bundle (catches dead whitelist prefixes, missing task files or
    subject config, absent AppWorld install) without running anything.
    """
    spec = _load_spec(config_path, smoke)
    _note_defaulted_base(spec)
    bundle = _build_bundle(spec, with_models=True)
    _say(f"bench:    {spec.bench} (root {spec.base_sha[:12]} @ {spec.repo_root})")
    _say(f"work_dir: {spec.work_dir}")
    _say(f"models:   {describe_models(spec.models)}")
    _say(f"funnel:   k_screen={spec.funnel.k_screen} k_confirm={spec.funnel.k_confirm} "
         f"budget={spec.funnel.budget.max_why_per_round}x"
         f"{spec.funnel.budget.candidates_per_why} "
         f"rounds<={spec.funnel.termination.max_rounds}")
    _say(f"cold start: {bundle.cold_start_done()}/{bundle.cold_start_total} trials present")
    _say(f"sealed test: {'configured' if bundle.unseal else 'not configured'}")
    _say("check OK — ready to run")
    return 0


def _node_status_counts(work_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in sorted(Path(work_dir).glob("nodes/*.json")):
        rec = load_json_or(path, {})
        status = rec.get("status", "?")
        counts[status] = counts.get(status, 0) + 1
    return counts


def cmd_status(config_path: str, *, smoke: bool = False) -> int:
    spec = _load_spec(config_path, smoke)
    meta = RunMeta.load(spec.work_dir)
    if meta is None:
        _say(f"no run state under {spec.work_dir} (phase 0: not started)")
        return 0
    _say(f"work_dir: {spec.work_dir}")
    _say(f"started: {meta.created_at}  config: {meta.config_hash}")
    if meta.unsealed_at:
        _say(f"UNSEALED at {meta.unsealed_at} ({meta.finalize_reason}) — run is final")
        report = load_json_or(Path(spec.work_dir) / "retention.json", None)
        if report:
            _say(f"retention report: {Path(spec.work_dir) / 'retention.json'}")
        return 0

    bundle = _build_bundle(spec, with_models=False)
    done, total = bundle.cold_start_done(), bundle.cold_start_total
    if done < total:
        _say(f"phase 1: cold start {done}/{total} trials — no results yet")
        return 0

    from raven.evolver.orchestrator.state.journal import RoundJournal

    records = RoundJournal(bundle.journal_path).load()
    if not records:
        _say("phase 2: cold start done, no completed rounds yet")
        return 0
    _say(f"phase 2: {len(records)} completed round(s); test stays sealed until "
         "termination or `finalize`")
    counts = _node_status_counts(spec.work_dir)
    if counts:
        _say("candidates by status: "
             + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    promoted = []
    for rec in records:
        _say(f"  round {rec.get('round_index')}: promoted={rec.get('promoted')} "
             f"beat_vanilla={rec.get('beat_vanilla')} "
             f"parent -> {rec.get('next_parent_id')}")
        if rec.get("promoted") and rec.get("next_parent_sha"):
            promoted.append((rec.get("next_parent_id"), rec.get("next_parent_sha"),
                             rec.get("next_parent_train")))
    if promoted:
        _say("promoted commits (train-side numbers only):")
        for node_id, sha, train in promoted:
            _say(f"  {node_id} @ {sha}  train={train}")
    return 0


def cmd_finalize(config_path: str, *, smoke: bool = False, yes: bool = False) -> int:
    spec = _load_spec(config_path, smoke)
    meta = RunMeta.load(spec.work_dir)
    if meta is None:
        print("nothing to finalize: run was never started", file=sys.stderr)
        return 2
    if meta.unsealed_at:
        _say(f"already unsealed at {meta.unsealed_at}; see retention.json")
        return 0

    bundle = _build_bundle(spec, with_models=False)
    from raven.evolver.orchestrator.state.journal import RoundJournal

    records = RoundJournal(bundle.journal_path).load()
    if not records:
        print("nothing to finalize: no completed rounds in the journal",
              file=sys.stderr)
        return 2
    if not yes:
        print(
            f"finalize will END this run after {len(records)} round(s) and unseal "
            "the test set — it cannot be resumed afterwards. Re-run with --yes.",
            file=sys.stderr,
        )
        return 2

    # Unsealing scores nodes on test: the orchestrator (hence models for its
    # construction path) is not needed, but the bench's eval is — the bundle
    # closures carry it. vanilla_train comes from the built orchestrator, so
    # build it without LLM roles (they are only called during rounds).
    orch = bundle.build_orchestrator()
    _unseal_and_report(spec, bundle, orch, records, meta, reason="user_finalized")
    return 0


__all__ = ["cmd_run", "cmd_status", "cmd_check", "cmd_finalize"]
