"""AppWorld bench plugin for the unified launcher (built-in scorer line).

``bench_config`` schema (YAML, under the run spec):

    bench_config:
      config_path: /path/subject_runtime.json   # required: agent runtime config
      appworld_data_root: /path/appworld        # AppWorld install (holds data/);
                                                # exported as APPWORLD_ROOT
      train_task_file: /path/train.txt          # or train_task_ids: [...]
      test_task_file: /path/test.txt            # optional (enables sealed test)
      n: 90                # optional cap on train tasks
      conc: 8
      base_port: 8600
      python_exe: <venv python>                 # default: current interpreter
      vanilla_experiment: vanilla               # cold-start ledger name
      extra_args: ["--task-timeout", "400"]     # passed through to batch.py
      whitelist: ["raven/agent/", ...]          # default: sandbox whitelist
      min_confirm_lift: 0.0
      taxonomy_mode: hardcoded                  # or induce
      why_selection: driver
      analysis_mode: mapreduce                  # or agentic
      agentic_model: claude-opus-4-8
      require_beacon: true
      zero_hit_preflight: false
"""

from __future__ import annotations

import sys
from pathlib import Path

from raven.benchmarks.appworld.evolve import adapter as aw_adapter
from raven.evolver.launch.contract import BenchBundle, LaunchContext, validate_whitelist

_KNOWN_KEYS = {
    "config_path", "train_task_file", "train_task_ids", "test_task_file",
    "test_task_ids", "n", "conc", "base_port", "python_exe",
    "vanilla_experiment", "extra_args", "whitelist", "min_confirm_lift",
    "taxonomy_mode", "taxonomy_path", "why_selection", "analysis_mode",
    "agentic_model", "require_beacon", "zero_hit_preflight",
    "appworld_data_root", "precheck", "precheck_min_tok_s",
}


def _wait_ports_free(base: int, count: int, timeout: float = 20.0) -> None:
    """Wait for this run's own env-server ports to free after a batch.

    ``batch.py`` terminates its servers with SIGTERM; a server can outlive the
    batch by a few seconds. The Gate0 precheck (correctly) treats a bound port
    as an orphan, so give our just-finished phase a grace window instead of
    failing the round on our own shutdown race.
    """
    import socket
    import time

    def bound(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) == 0

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(bound(p) for p in range(base, base + count)):
            return
        time.sleep(1.0)


def _task_ids(bc: dict, prefix: str) -> list[str]:
    ids = bc.get(f"{prefix}_task_ids")
    if ids:
        return [str(t) for t in ids]
    file_key = f"{prefix}_task_file"
    if bc.get(file_key):
        path = Path(bc[file_key]).expanduser()
        if not path.is_file():
            raise ValueError(f"bench_config.{file_key}: not found: {path}")
        return [l.strip() for l in path.read_text().splitlines() if l.strip()]
    return []


def build(ctx: LaunchContext) -> BenchBundle:
    spec = ctx.spec
    bc = dict(spec.bench_config)
    unknown = set(bc) - _KNOWN_KEYS
    if unknown:
        raise ValueError(f"bench_config: unknown keys {sorted(unknown)}")
    if not bc.get("config_path"):
        raise ValueError("bench_config.config_path is required (subject runtime config)")
    config_path = Path(bc["config_path"]).expanduser()
    if not config_path.is_file():
        raise ValueError(
            f"bench_config.config_path not found: {config_path} — this is the "
            "subject agent's runtime config JSON (model endpoint etc.)"
        )

    # The batch scorer locates the AppWorld install/data via APPWORLD_ROOT;
    # surface a missing install at build time, not as a mid-run stack trace.
    data_root = bc.get("appworld_data_root")
    if data_root:
        data_root = Path(data_root).expanduser()
        if not (data_root / "data").is_dir():
            raise ValueError(
                f"bench_config.appworld_data_root has no data/ under it: {data_root} — "
                "install AppWorld there first (see raven/evolver/README.md, Bootstrap)"
            )
        import os

        os.environ["APPWORLD_ROOT"] = str(data_root)

    train_ids = _task_ids(bc, "train")
    if not train_ids:
        raise ValueError("bench_config: train_task_ids or train_task_file is required")
    if bc.get("n"):
        train_ids = train_ids[: int(bc["n"])]
    test_ids = _task_ids(bc, "test")
    overlap = set(train_ids) & set(test_ids)
    if overlap:
        raise ValueError(f"train/test task sets overlap: {sorted(overlap)[:5]} …")

    from raven.benchmarks.appworld.evolve.sandbox import WHITELIST_PREFIXES

    whitelist = tuple(bc.get("whitelist") or WHITELIST_PREFIXES)
    validate_whitelist(spec.repo_root, spec.base_sha, whitelist)

    work = Path(spec.work_dir)
    runs_root = work / "runs"
    ws_root = work / "ws"
    worktree_root = work / "wt"
    van_exp = bc.get("vanilla_experiment", "vanilla")
    vanilla_out_dir = runs_root / van_exp
    k_confirm = spec.funnel.k_confirm

    cfg = aw_adapter.AppWorldConfig(
        appworld_root=spec.repo_root,
        python_exe=bc.get("python_exe") or sys.executable,
        config_path=config_path,
        out_dir_root=runs_root,
        split="train",
        n=len(train_ids),
        conc=int(bc.get("conc", 8)),
        base_port=bc.get("base_port"),
        workspace=ws_root,
        extra_args=tuple(str(a) for a in bc.get("extra_args", ())),
    )

    def cold_start_done() -> int:
        if not vanilla_out_dir.is_dir():
            return 0
        return sum(
            1
            for tid in train_ids
            for k in range(k_confirm)
            if (vanilla_out_dir / f"{tid}_k{k}.json").is_file()
        )

    def run_cold_start() -> None:
        runs_root.mkdir(parents=True, exist_ok=True)
        aw_adapter.run_eval(cfg, K=k_confirm, experiment=van_exp, task_ids=train_ids)

    def build_orchestrator():
        from raven.benchmarks.appworld.evolve.run import build_appworld_orchestrator

        if cfg.base_port is not None:
            _wait_ports_free(cfg.base_port, cfg.conc)
        precheck = None
        if not bc.get("precheck", True):
            precheck = lambda: None  # noqa: E731 — explicit opt-out
        elif bc.get("precheck_min_tok_s") is not None:
            from raven.benchmarks.appworld.evolve.precheck import make_appworld_precheck

            precheck = make_appworld_precheck(
                cfg, min_tok_per_s=float(bc["precheck_min_tok_s"])
            )
        return build_appworld_orchestrator(
            config=spec.funnel,
            aw_cfg=cfg,
            repo_root=spec.repo_root,
            base_sha=spec.base_sha,
            driver_call_fn=ctx.models.get("driver"),
            design_call_fn=ctx.models.get("design") or ctx.models.get("driver"),
            verdict_call_fn=ctx.models.get("verdict"),
            vanilla_out_dir=vanilla_out_dir,
            train_task_ids=train_ids,
            test_task_ids=test_ids,
            runs_root=runs_root,
            ws_root=ws_root,
            worktree_root=worktree_root,
            min_confirm_lift=float(bc.get("min_confirm_lift", 0.0)),
            taxonomy_mode=bc.get("taxonomy_mode", "hardcoded"),
            taxonomy_path=bc.get("taxonomy_path"),
            require_beacon=bool(bc.get("require_beacon", True)),
            zero_hit_preflight=bool(bc.get("zero_hit_preflight", False)),
            why_selection=bc.get("why_selection", "driver"),
            analysis_mode=bc.get("analysis_mode", "mapreduce"),
            agentic_model=bc.get("agentic_model", "claude-opus-4-8"),
            whitelist_prefixes=whitelist,
            precheck=precheck,
        )

    from raven.evolver.tree.node import HarnessNode

    root_node = HarnessNode(
        node_id="C0", parent_id=None, git_commit_sha=spec.base_sha,
        git_branch="", created_at=HarnessNode.utc_now(), created_at_iter=0,
    )

    unseal = None
    if test_ids:
        def unseal(records: list[dict], orch) -> dict:
            import dataclasses

            from raven.benchmarks.appworld.evolve.run import build_appworld_sealed_runner
            from raven.evolver.orchestrator.sealed.runner import unseal_retention

            runner = build_appworld_sealed_runner(
                aw_cfg=cfg, repo_root=spec.repo_root, test_task_ids=test_ids,
                sealed_dir=spec.funnel.sealed_output_dir or work / "sealed",
                k=k_confirm,
            )
            report = unseal_retention(
                runner, records,
                vanilla_node=root_node,
                vanilla_train=orch._vanilla_train_mean,
            )
            return dataclasses.asdict(report) if dataclasses.is_dataclass(report) \
                else dict(report)

    return BenchBundle(
        root_node_id="C0",
        root_node=root_node,
        journal_path=work / "journal" / "rounds.jsonl",
        cold_start_total=len(train_ids) * k_confirm,
        cold_start_done=cold_start_done,
        run_cold_start=run_cold_start,
        build_orchestrator=build_orchestrator,
        unseal=unseal,
    )


__all__ = ["build"]
