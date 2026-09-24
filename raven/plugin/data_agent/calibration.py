"""Calibration profiles: gate strengths, thresholds, lexicons, text fragments.

The data-agent mechanisms are general; what varies by deployment is their
calibration: how hard the delivery gates push back (refuse vs warn vs off),
the survey census thresholds, the placeholder/sentinel vocabularies, and
short deployment-specific fragments spliced into tool descriptions and
survey lines. All of that lives here as data, so the shipped default stays
general-purpose and a run config can overlay its own calibration -- inline
under ``profile_overrides`` or from a yaml file named by ``profile_file`` --
without touching code.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Literal

GateMode = Literal["refuse", "warn", "off"]

_GATE_MODES = ("refuse", "warn", "off")

# Every policy-controlled gate sits in submit_answer's delivery path. Hard
# input validation (empty rows, missing reading, too-thin reason) is not a
# gate: a malformed call is refused in every profile.
GATE_IDS = (
    "for_each",
    "single_reading",
    "numbers_in_reason",
    "uncomputed_rejections",
    "unmeasured_exclusions",
    "exclusion_basis",
    "conjunctive_request",
)


@dataclass(frozen=True)
class Thresholds:
    """Numeric calibration. Defaults are the shipped general-purpose values;
    none of them is validated across domains, which is exactly why they are
    declared here instead of buried as module constants."""

    synonym_token_min: int = 5
    synonym_stem_chars: int = 8
    template_families: int = 6
    template_min_rows: int = 20
    template_min_coverage: float = 0.6
    near_fd_purity: float = 0.95
    phrasing_min_avg_len: int = 40
    phrasing_prefix_chars: int = 32
    free_text_shape_ratio: float = 0.05
    single_reading_max_rows: int = 2
    for_each_min_rows: int = 3


@dataclass(frozen=True)
class Lexicons:
    """Value vocabularies. The base sets carry only cross-domain conventions;
    store-specific spellings belong in a run config's ``extra_*`` overlays,
    never here."""

    sentinels: frozenset[str] = frozenset(
        {"", "na", "n/a", "nan", "null", "none", "-", "--", "?", "unknown", "-999"}
    )
    extra_sentinels: frozenset[str] = frozenset()
    placeholders: tuple[str, ...] = (
        "", "n/a", "na", "n.a.", "null", "none", "nil", "nan", "n.d.", "nd", "tbd",
        "tba", "unknown", "untitled", "unnamed", "no title", "not available",
        "missing", "misc", "other", "others", "various", "unk", "unk.", "-",
    )
    extra_placeholders: tuple[str, ...] = ()

    def sentinel_values(self) -> frozenset[str]:
        return self.sentinels | frozenset(v.lower() for v in self.extra_sentinels)

    def placeholder_values(self) -> tuple[str, ...]:
        return self.placeholders + self.extra_placeholders


@dataclass(frozen=True)
class Features:
    """Behavior switches for mechanisms added after a scored baseline. A run
    that must reproduce an earlier stack byte-for-byte turns the additions off
    here instead of pinning an old code revision."""

    bracketed_placeholders: bool = True
    repeated_extreme: bool = True


@dataclass(frozen=True)
class DataAgentProfile:
    name: str = "general"
    gates: dict[str, str] = field(
        default_factory=lambda: {g: "warn" for g in GATE_IDS}
    )
    thresholds: Thresholds = field(default_factory=Thresholds)
    lexicons: Lexicons = field(default_factory=Lexicons)
    features: Features = field(default_factory=Features)
    fragments: dict[str, str] = field(default_factory=dict)

    def gate_mode(self, gate_id: str) -> str:
        return self.gates.get(gate_id, "warn")

    def fragment(self, slot: str, default: str = "") -> str:
        return self.fragments.get(slot, default)


GENERAL = DataAgentProfile()

# Same mechanisms at full strength: every gate refuses instead of advising.
# The right default for unattended batch runs, where nobody reads advisories.
STRICT = replace(GENERAL, name="strict", gates={g: "refuse" for g in GATE_IDS})

_NAMED = {"general": GENERAL, "strict": STRICT}


def apply_overrides(base: DataAgentProfile, overrides: dict[str, Any]) -> DataAgentProfile:
    """Overlay a config mapping onto a profile. Unknown keys raise: silently
    dropped calibration is worse than a startup error."""
    unknown = set(overrides) - {"name", "gates", "thresholds", "lexicons", "features", "fragments"}
    if unknown:
        raise ValueError(f"unknown profile override section(s): {sorted(unknown)}")
    profile = base
    if overrides.get("name"):
        profile = replace(profile, name=str(overrides["name"]))
    if overrides.get("gates"):
        gates = dict(profile.gates)
        for gate_id, mode in dict(overrides["gates"]).items():
            if gate_id not in GATE_IDS:
                raise ValueError(f"unknown gate {gate_id!r}; known: {GATE_IDS}")
            if mode not in _GATE_MODES:
                raise ValueError(f"gate {gate_id!r} mode {mode!r} not in {_GATE_MODES}")
            gates[gate_id] = str(mode)
        profile = replace(profile, gates=gates)
    if overrides.get("thresholds"):
        known = {f.name: f.type for f in fields(Thresholds)}
        values = dict(overrides["thresholds"])
        bad = set(values) - set(known)
        if bad:
            raise ValueError(f"unknown threshold(s): {sorted(bad)}")
        profile = replace(profile, thresholds=replace(profile.thresholds, **values))
    if overrides.get("lexicons"):
        lex = dict(overrides["lexicons"])
        bad = set(lex) - {"extra_sentinels", "extra_placeholders"}
        if bad:
            raise ValueError(
                f"unknown lexicon key(s): {sorted(bad)}; only extra_sentinels and "
                "extra_placeholders may be overlaid"
            )
        lexicons = replace(
            profile.lexicons,
            extra_sentinels=profile.lexicons.extra_sentinels
            | frozenset(str(v) for v in lex.get("extra_sentinels") or ()),
            extra_placeholders=profile.lexicons.extra_placeholders
            + tuple(str(v) for v in lex.get("extra_placeholders") or ()),
        )
        profile = replace(profile, lexicons=lexicons)
    if overrides.get("features"):
        known_features = {f.name for f in fields(Features)}
        values = dict(overrides["features"])
        bad = set(values) - known_features
        if bad:
            raise ValueError(f"unknown feature(s): {sorted(bad)}")
        profile = replace(
            profile, features=replace(profile.features, **{k: bool(v) for k, v in values.items()})
        )
    if overrides.get("fragments"):
        fragments = dict(profile.fragments)
        fragments.update({str(k): str(v) for k, v in dict(overrides["fragments"]).items()})
        profile = replace(profile, fragments=fragments)
    return profile


def resolve_profile(config: dict[str, Any] | None) -> DataAgentProfile:
    """Build the effective profile from plugin config: named base (``profile``),
    then file overlay (``profile_file``), then inline ``profile_overrides``."""
    config = config or {}
    base_name = str(config.get("profile") or "general").lower()
    if base_name not in _NAMED:
        raise ValueError(f"unknown profile {base_name!r}; known: {sorted(_NAMED)}")
    profile = _NAMED[base_name]
    path = config.get("profile_file")
    if path:
        import yaml

        loaded = yaml.safe_load(Path(str(path)).read_text(encoding="utf-8")) or {}
        # A pack file may carry the overrides at top level or under the same
        # profile_overrides key used inline, so one file can be pasted into a
        # run config or pointed at directly.
        loaded = loaded.get("profile_overrides", loaded)
        profile = apply_overrides(profile, dict(loaded))
    inline = config.get("profile_overrides")
    if inline:
        profile = apply_overrides(profile, dict(inline))
    return profile
