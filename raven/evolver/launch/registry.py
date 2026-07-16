"""Bench registry: name -> ``module:function`` building a BenchBundle."""

from __future__ import annotations

from importlib import import_module
from typing import Callable

BENCHES: dict[str, str] = {
    "appworld": "raven.benchmarks.appworld.evolve.entry:build",
}


def load_bench(name: str) -> Callable:
    target = BENCHES.get(name)
    if target is None:
        raise ValueError(
            f"unknown bench {name!r}; registered: {sorted(BENCHES)} "
            "(add yours to raven.evolver.launch.registry.BENCHES)"
        )
    mod_name, _, fn_name = target.partition(":")
    return getattr(import_module(mod_name), fn_name)


__all__ = ["BENCHES", "load_bench"]
