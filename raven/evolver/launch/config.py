"""Run-spec loading: YAML -> validated RunSpec (+ --smoke overlay).

The YAML shape:

    bench: appworld
    repo_root: /path/to/subject          # the repo being evolved
    base_sha: <commit>                   # optional; omitted -> repo_root HEAD at launch
    work_dir: ./evo_work

    models:                              # optional; omitted -> raven's own model
      driver:  {provider: claude_cli, model: claude-haiku-4-5}
      design:  {provider: claude_cli, model: claude-opus-4-8}
      verdict: {provider: openai_compat, base_url: ..., model: ...}

    funnel:                              # optional; SOP-aligned defaults
      k_screen: 1
      k_confirm: 3
      budget:      {max_why_per_round: 2, candidates_per_why: 3}
      termination: {patience: 10, max_rounds: 20}
      anchor:      {n_sentinel: 12, cull_sigma_mult: 1.5}

    bench_config: {...}                  # schema owned by the bench entry

    smoke: {...}                         # optional deep-merge overlay for --smoke

``--smoke`` applies built-in shrink defaults (1 WHY x 1 candidate x 1 round,
K=1) first, then the user's ``smoke:`` section on top, and suffixes work_dir
with ``_smoke`` so a smoke run never touches the real run's state.
"""

from __future__ import annotations

import copy
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from raven.evolver.orchestrator.config import (
    AnchorParams,
    Budget,
    OrchestratorConfig,
    Termination,
)

SMOKE_BUILTIN: dict = {
    "funnel": {
        "k_confirm": 1,
        "budget": {"max_why_per_round": 1, "candidates_per_why": 1,
                   "recombinations_per_round": 0},
        "termination": {"patience": 1, "max_rounds": 1},
    },
}


class RunSpecError(ValueError):
    """A config file problem the user must fix; message says exactly what."""


def deep_merge(base: dict, overlay: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@dataclass
class RunSpec:
    bench: str
    repo_root: Path
    base_sha: str
    work_dir: Path
    funnel: OrchestratorConfig
    models: dict = field(default_factory=dict)
    bench_config: dict = field(default_factory=dict)
    smoke: bool = False
    base_sha_defaulted: bool = False
    raw: dict = field(default_factory=dict)

    def snapshot(self) -> dict:
        """The effective configuration recorded in run_meta (drift guard)."""
        return {
            "bench": self.bench,
            "repo_root": str(self.repo_root),
            "base_sha": self.base_sha,
            "models": self.raw.get("models", {}),
            "funnel": self.raw.get("funnel", {}),
            "bench_config": self.raw.get("bench_config", {}),
            "smoke": self.smoke,
        }


def _build_funnel(repo_root: Path, work_dir: Path, funnel: dict) -> OrchestratorConfig:
    known = {"k_screen", "k_confirm", "anchor", "budget", "termination",
             "sealed_test_split"}
    unknown = set(funnel) - known
    if unknown:
        raise RunSpecError(f"funnel: unknown keys {sorted(unknown)}")
    try:
        return OrchestratorConfig(
            repo_root=repo_root,
            work_dir=work_dir,
            driver_llm_spec={},
            k_screen=int(funnel.get("k_screen", 1)),
            k_confirm=int(funnel.get("k_confirm", 3)),
            anchor=AnchorParams(**funnel.get("anchor", {})),
            budget=Budget(**funnel.get("budget", {})),
            termination=Termination(**funnel.get("termination", {})),
            sealed_test_split=funnel.get("sealed_test_split", "test"),
            sealed_output_dir=work_dir / "sealed",
        )
    except TypeError as exc:
        raise RunSpecError(f"funnel: {exc}") from exc


def load_run_spec(config_path: str | Path, *, smoke: bool = False) -> RunSpec:
    path = Path(config_path)
    if not path.is_file():
        raise RunSpecError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise RunSpecError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise RunSpecError(f"{path}: top level must be a mapping")

    if smoke:
        overlay = data.pop("smoke", {}) or {}
        data = deep_merge(deep_merge(data, SMOKE_BUILTIN), overlay)
    else:
        data.pop("smoke", None)

    missing = [k for k in ("bench", "repo_root", "work_dir") if not data.get(k)]
    if missing:
        raise RunSpecError(f"{path}: missing required keys: {missing}")

    repo_root = Path(data["repo_root"]).expanduser()
    if not (repo_root / ".git").exists():
        raise RunSpecError(f"repo_root is not a git checkout: {repo_root}")

    base_sha = str(data.get("base_sha") or "").strip()
    base_sha_defaulted = not base_sha
    if base_sha_defaulted:
        base_sha = _resolve_head(repo_root)
        data["base_sha"] = base_sha
    work_dir = Path(data["work_dir"]).expanduser()
    if smoke:
        work_dir = work_dir.with_name(work_dir.name + "_smoke")

    models = data.get("models") or {}
    if not isinstance(models, dict):
        raise RunSpecError("models: must be a mapping of role -> provider spec")
    unknown_roles = set(models) - {"driver", "design", "verdict"}
    if unknown_roles:
        raise RunSpecError(f"models: unknown roles {sorted(unknown_roles)}")

    return RunSpec(
        bench=str(data["bench"]),
        repo_root=repo_root,
        base_sha=base_sha,
        work_dir=work_dir,
        funnel=_build_funnel(repo_root, work_dir, data.get("funnel") or {}),
        models=models,
        bench_config=data.get("bench_config") or {},
        smoke=smoke,
        base_sha_defaulted=base_sha_defaulted,
        raw=data,
    )


def _resolve_head(repo_root: Path) -> str:
    """Resolve the subject repo's HEAD when the yaml omits ``base_sha``.

    Resolved to a full sha at load time and recorded in the config snapshot,
    so the run stays pinned to the commit HEAD pointed at when it started —
    resuming after the repo gained commits trips the drift guard instead of
    silently moving the root.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RunSpecError(
            f"base_sha omitted and resolving HEAD of {repo_root} failed: "
            f"{proc.stderr.strip()}"
        )
    return proc.stdout.strip()


__all__ = ["RunSpec", "RunSpecError", "load_run_spec", "deep_merge", "SMOKE_BUILTIN"]
