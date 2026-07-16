"""Unit tests for the unified evolution launcher (raven.evolver.launch)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from raven.evolver.launch.config import (
    SMOKE_BUILTIN,
    RunSpecError,
    deep_merge,
    load_run_spec,
)
from raven.evolver.launch.contract import validate_whitelist
from raven.evolver.launch.registry import load_bench
from raven.evolver.launch.state import RunMeta, atomic_write_json, config_fingerprint


@pytest.fixture()
def subject_repo(tmp_path: Path) -> tuple[Path, str]:
    """A tiny git repo standing in for the evolved subject."""
    repo = tmp_path / "subject"
    (repo / "raven/agent").mkdir(parents=True)
    (repo / "raven/agent/loop.py").write_text("x = 1\n")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=repo, check=True, env={**env, "PATH": "/usr/bin:/bin"},
                       capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                         capture_output=True, text=True).stdout.strip()
    return repo, sha


def _write_spec(tmp_path: Path, repo: Path, sha: str, **extra) -> Path:
    data = {
        "bench": "appworld",
        "repo_root": str(repo),
        "base_sha": sha,
        "work_dir": str(tmp_path / "work"),
        **extra,
    }
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


class TestRunSpec:
    def test_minimal_spec_loads_with_defaults(self, tmp_path, subject_repo):
        repo, sha = subject_repo
        spec = load_run_spec(_write_spec(tmp_path, repo, sha))
        assert spec.bench == "appworld"
        assert spec.funnel.k_confirm == 3
        assert spec.funnel.termination.patience == 10
        assert spec.funnel.sealed_output_dir == spec.work_dir / "sealed"

    def test_omitted_base_sha_resolves_to_head(self, tmp_path, subject_repo):
        repo, sha = subject_repo
        path = tmp_path / "spec.yaml"
        path.write_text(yaml.safe_dump({
            "bench": "appworld",
            "repo_root": str(repo),
            "work_dir": str(tmp_path / "work"),
        }))
        spec = load_run_spec(path)
        assert spec.base_sha == sha
        assert spec.base_sha_defaulted is True
        assert spec.snapshot()["base_sha"] == sha

    def test_explicit_base_sha_kept_verbatim(self, tmp_path, subject_repo):
        repo, sha = subject_repo
        spec = load_run_spec(_write_spec(tmp_path, repo, sha[:7]))
        assert spec.base_sha == sha[:7]
        assert spec.base_sha_defaulted is False

    def test_missing_required_keys(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text(yaml.safe_dump({"bench": "appworld"}))
        with pytest.raises(RunSpecError, match="missing required"):
            load_run_spec(path)

    def test_unknown_funnel_key_rejected(self, tmp_path, subject_repo):
        repo, sha = subject_repo
        path = _write_spec(tmp_path, repo, sha, funnel={"k_confrim": 3})
        with pytest.raises(RunSpecError, match="unknown keys"):
            load_run_spec(path)

    def test_unknown_model_role_rejected(self, tmp_path, subject_repo):
        repo, sha = subject_repo
        path = _write_spec(tmp_path, repo, sha, models={"designer": {}})
        with pytest.raises(RunSpecError, match="unknown roles"):
            load_run_spec(path)

    def test_smoke_applies_builtin_shrink_then_user_overlay(
        self, tmp_path, subject_repo
    ):
        repo, sha = subject_repo
        path = _write_spec(
            tmp_path, repo, sha,
            funnel={"k_confirm": 3},
            smoke={"funnel": {"termination": {"max_rounds": 2}}},
        )
        spec = load_run_spec(path, smoke=True)
        assert spec.funnel.k_confirm == SMOKE_BUILTIN["funnel"]["k_confirm"]
        assert spec.funnel.budget.max_why_per_round == 1
        assert spec.funnel.termination.max_rounds == 2  # user overlay wins
        assert spec.work_dir.name.endswith("_smoke")

    def test_non_smoke_ignores_smoke_section(self, tmp_path, subject_repo):
        repo, sha = subject_repo
        path = _write_spec(tmp_path, repo, sha, smoke={"funnel": {"k_confirm": 1}})
        spec = load_run_spec(path)
        assert spec.funnel.k_confirm == 3
        assert "smoke" not in spec.raw

    def test_deep_merge_nested(self):
        merged = deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 9}, "d": 3})
        assert merged == {"a": {"b": 9, "c": 2}, "d": 3}


class TestRunMeta:
    def test_create_load_roundtrip(self, tmp_path):
        meta = RunMeta.create(tmp_path, {"bench": "x"})
        again = RunMeta.load(tmp_path)
        assert again is not None
        assert again.config_hash == meta.config_hash
        assert again.unsealed_at is None

    def test_config_drift_detected(self, tmp_path):
        meta = RunMeta.create(tmp_path, {"bench": "x", "funnel": {"k": 3}})
        assert meta.check_config({"bench": "x", "funnel": {"k": 3}})
        assert not meta.check_config({"bench": "x", "funnel": {"k": 1}})

    def test_unseal_stamp_is_persisted(self, tmp_path):
        meta = RunMeta.create(tmp_path, {})
        meta.stamp_unsealed(reason="user_finalized")
        again = RunMeta.load(tmp_path)
        assert again.unsealed_at
        assert again.finalize_reason == "user_finalized"

    def test_fingerprint_is_order_insensitive(self):
        assert config_fingerprint({"a": 1, "b": 2}) == config_fingerprint(
            {"b": 2, "a": 1}
        )

    def test_atomic_write_leaves_no_tmp_on_success(self, tmp_path):
        target = tmp_path / "x.json"
        atomic_write_json(target, {"ok": True})
        assert json.loads(target.read_text()) == {"ok": True}
        assert list(tmp_path.glob("*.tmp")) == []


class TestWhitelistValidation:
    def test_live_prefix_passes(self, subject_repo):
        repo, sha = subject_repo
        validate_whitelist(repo, sha, ("raven/agent/",))

    def test_dead_prefix_refuses(self, subject_repo):
        repo, sha = subject_repo
        with pytest.raises(ValueError, match="match no files"):
            validate_whitelist(repo, sha, ("nonexistent/agent/",))

    def test_empty_whitelist_refuses(self, subject_repo):
        repo, sha = subject_repo
        with pytest.raises(ValueError, match="empty"):
            validate_whitelist(repo, sha, ())


class TestAppWorldEntry:
    def _ctx(self, tmp_path, subject_repo, bench_config, smoke=False):
        from raven.evolver.launch.contract import LaunchContext

        repo, sha = subject_repo
        path = _write_spec(tmp_path, repo, sha, bench_config=bench_config)
        spec = load_run_spec(path, smoke=smoke)
        return LaunchContext(
            spec=spec, models={"driver": None, "design": None, "verdict": None},
            smoke=smoke,
        )

    def _bench_config(self, tmp_path):
        cfg = tmp_path / "subject_cfg.json"
        cfg.write_text("{}")
        return {
            "config_path": str(cfg),
            "train_task_ids": ["t1", "t2"],
            "whitelist": ["raven/agent/"],
        }

    def test_build_bundle_shape(self, tmp_path, subject_repo):
        build = load_bench("appworld")
        bundle = build(self._ctx(tmp_path, subject_repo, self._bench_config(tmp_path)))
        assert bundle.root_node_id == "C0"
        assert bundle.cold_start_total == 2 * 3  # 2 tasks x k_confirm=3
        assert bundle.cold_start_done() == 0
        assert bundle.unseal is None  # no test ids -> no sealed test

    def test_cold_start_counts_existing_trials(self, tmp_path, subject_repo):
        build = load_bench("appworld")
        ctx = self._ctx(tmp_path, subject_repo, self._bench_config(tmp_path))
        bundle = build(ctx)
        van = ctx.spec.work_dir / "runs" / "vanilla"
        van.mkdir(parents=True)
        (van / "t1_k0.json").write_text("{}")
        (van / "t1_k1.json").write_text("{}")
        assert bundle.cold_start_done() == 2

    def test_unknown_bench_config_key_rejected(self, tmp_path, subject_repo):
        build = load_bench("appworld")
        bc = self._bench_config(tmp_path)
        bc["train_tasks"] = ["x"]
        with pytest.raises(ValueError, match="unknown keys"):
            build(self._ctx(tmp_path, subject_repo, bc))

    def test_train_test_overlap_rejected(self, tmp_path, subject_repo):
        build = load_bench("appworld")
        bc = self._bench_config(tmp_path)
        bc["test_task_ids"] = ["t2", "t3"]
        with pytest.raises(ValueError, match="overlap"):
            build(self._ctx(tmp_path, subject_repo, bc))

    def test_dead_whitelist_refuses_at_build(self, tmp_path, subject_repo):
        build = load_bench("appworld")
        bc = self._bench_config(tmp_path)
        bc["whitelist"] = ["nonexistent/dir/"]
        with pytest.raises(ValueError, match="match no files"):
            build(self._ctx(tmp_path, subject_repo, bc))


class TestBatchTrialResume:
    def test_existing_result_is_returned_without_rerun(self, tmp_path, monkeypatch):
        from raven.benchmarks.appworld import batch

        out_dir = tmp_path
        rec = {"task_id": "t1", "success": True}
        (out_dir / "t1_k0.json").write_text(json.dumps(rec))

        def boom(*a, **k):  # noqa: ANN002, ANN003
            raise AssertionError("subprocess must not run for a completed trial")

        monkeypatch.setattr(batch.subprocess, "run", boom)

        class Args:
            config = "unused"
            workspace = str(tmp_path / "ws")
            experiment = "e"
            model = None
            env = ""
            task_timeout = 1

        got = batch._run_one("t1", 0, 9999, Args(), str(out_dir))
        assert got == rec

    def test_corrupt_result_is_rerun(self, tmp_path, monkeypatch):
        from raven.benchmarks.appworld import batch

        (tmp_path / "t1_k0.json").write_text("{half written")
        calls = []

        def fake_run(*a, **k):  # noqa: ANN002, ANN003
            calls.append(1)

        monkeypatch.setattr(batch.subprocess, "run", fake_run)

        class Args:
            config = "unused"
            workspace = str(tmp_path / "ws")
            experiment = "e"
            model = None
            env = ""
            task_timeout = 1

        got = batch._run_one("t1", 0, 9999, Args(), str(tmp_path))
        assert calls, "corrupt file must trigger a re-run"
        assert got.get("infra_error")  # agent_cli never wrote a fresh result


class TestCli:
    def test_parser_subcommands(self):
        from raven.evolver.cli import build_parser

        p = build_parser()
        args = p.parse_args(["run", "--config", "x.yaml", "--smoke", "--force"])
        assert args.command == "run" and args.smoke and args.force
        args = p.parse_args(["finalize", "--config", "x.yaml", "--yes"])
        assert args.command == "finalize" and args.yes

    def test_status_on_fresh_dir_reports_not_started(self, tmp_path, subject_repo, capsys):
        from raven.evolver.launch.runner import cmd_status

        repo, sha = subject_repo
        path = _write_spec(tmp_path, repo, sha)
        assert cmd_status(str(path)) == 0
        assert "not started" in capsys.readouterr().out

    def test_run_refuses_after_unseal(self, tmp_path, subject_repo, capsys):
        from raven.evolver.launch.runner import cmd_run

        repo, sha = subject_repo
        path = _write_spec(tmp_path, repo, sha)
        work = tmp_path / "work"
        work.mkdir()
        meta = RunMeta.create(work, {})
        meta.stamp_unsealed(reason="user_finalized")
        with pytest.raises(SystemExit) as exc:
            cmd_run(str(path))
        assert exc.value.code == 2
        assert "unsealed" in capsys.readouterr().err

    def test_run_refuses_on_config_drift(self, tmp_path, subject_repo, capsys):
        from raven.evolver.launch.runner import cmd_run

        repo, sha = subject_repo
        path = _write_spec(tmp_path, repo, sha)
        work = tmp_path / "work"
        work.mkdir()
        RunMeta.create(work, {"bench": "appworld", "different": True})
        with pytest.raises(SystemExit) as exc:
            cmd_run(str(path))
        assert exc.value.code == 2
        assert "config drift" in capsys.readouterr().err
