"""submit_answer: serialized, adjudicated final-answer delivery.

Trace analysis across models keeps finding the same two post-computation
failures: a correctly computed result set gets retyped by hand and truncated
on the way out, and when several candidate readings were all computed, the
final pick happens silently with no recorded reason. Neither is a reasoning
failure -- both are delivery-path failures, so the fix lives in the delivery
path: one tool that takes the rows as data (not prose) and refuses to write
until the reading that produced them is committed with a reason.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from raven.agent.tools.base import Tool
from raven.plugin.context import PluginContext
from raven.plugin.data_agent.calibration import GENERAL, DataAgentProfile, resolve_profile

_MIN_REASON_CHARS = 40
_MAX_ROWS = 10_000
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?%?")
_FOR_EACH_RE = re.compile(r"\bfor (?:each|every)\b", re.IGNORECASE)
# A rejected reading needs the value it produced when computed -- unless it is
# structurally impossible to compute, which these phrasings assert.
_STRUCTURAL_RE = re.compile(
    r"no such|not present|not stored|does not exist|absent|no column|no field|no table",
    re.IGNORECASE,
)


class SubmitAnswerTool(Tool):
    """Write the final answer from a rows list plus an adjudication record."""

    timeout_seconds = 30.0

    def __init__(
        self,
        answer_path: str = "/workspace/answer.txt",
        question_path: str = "/workspace/question.txt",
        profile: DataAgentProfile | None = None,
    ) -> None:
        self._answer_path = answer_path
        self._question_path = question_path
        self._profile = profile or GENERAL
        self._for_each_warned = False
        self._single_reading_warned = False

    @property
    def name(self) -> str:
        return "submit_answer"

    @property
    def description(self) -> str:
        return (
            "The sanctioned way to deliver the final answer. Pass the result as rows -- one "
            "element per item, taken from your computed result object (iterate it; never "
            "retype values by hand, and never summarize N items into fewer lines). Every "
            "value is carried whole as stored: a compound or list-valued field ('A, B, C') "
            "is delivered as the entire stored string, never trimmed to one element of it. "
            "When the question says 'for each X', rows carries one element per X in the "
            "data: a superlative (best/highest/latest) selects WITHIN each group, it never "
            "filters which groups appear. When the question itself asks for exactly the "
            "top k items, check the boundary before submitting: name the ranking metric, "
            "the margin between rank k and rank k+1, and how many items sit at the "
            "boundary value -- a tie or hairline margin there means re-reading which "
            "metric the question actually asks. Run that boundary count over the "
            "population as the question defines it, BEFORE any predicate you added "
            "yourself: a self-imposed filter that drops the tied items leaves a clean "
            "boundary behind and hides exactly what this check exists to catch. It never "
            "applies to a 'for each'/'per group' question, where every group is still "
            "delivered. "
            "A delivered count or total must come from a "
            "computation over the full filtered population, never scaled up from a "
            "sample or a labeled subset. A filter or exclusion is decided only by "
            "values stored in the data: when the deciding column has no stored value "
            "for a candidate, the predicate is unverifiable and that candidate is "
            "excluded -- as in SQL, where a comparison against a missing value is "
            "never satisfied -- never waved through on outside knowledge about the "
            "entity. That rule covers the column the question's own condition reads, "
            "and it does not travel to any other column: a row whose unrelated or "
            "optional fields are empty is still a member of the population it sits in. "
            "Membership comes from the table the question points at, not from how "
            "completely a row happens to be filled in -- treating sparsely populated "
            "rows as second-class records is a completeness opinion, not a predicate "
            "the question asked for, and it shrinks the population without leaving a "
            "trace in the answer. Before submitting, check that each qualifier stated in the "
            "question actually changed the cohort: one that removed zero rows while "
            "such values exist elsewhere in that column usually means the wrong "
            "column or grain was filtered -- recompute there instead. Before "
            "adjudicating, walk the standard reading families: record-weighted vs "
            "entity-weighted (each raw record counts vs each resolved entity once), "
            "exact vs contains matching, raw vs deduplicated grain, strict vs lenient "
            "predicate, time-window anchoring, and -- the one that hides best -- any "
            "subpopulation you decided to leave out on your own judgment, because rows "
            "of that shape looked malformed, synthetic, secondary or off-topic. Dropping "
            "them is a reading, not cleanup: the question did not ask for it, so it has "
            "to be declared in excluded_populations and computed both ways. Any family "
            "where both sides are "
            "defensible for this question is an alternative reading -- compute it "
            "rather than argue it away. When a time window or anchor is genuinely "
            "ambiguous, compute every defensible variant in one pass: variants that "
            "agree make the ambiguity immaterial (record the shared value); variants "
            "that disagree mark exactly the fork the adjudication must decide from "
            "data evidence. Commit the "
            "interpretation that produced it: "
            "selected_reading names the reading you stand behind, rejected_readings lists "
            "the alternatives you computed or considered, and reason must say what evidence "
            "in the data decided between them. Refuses to write when the adjudication is "
            "missing or hollow."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "rows": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The answer, one item per element, serialized from the "
                    "computed result, each value carried whole (never a subset of a "
                    "compound value). A single-value answer is a one-element list.",
                },
                "selected_reading": {
                    "type": "string",
                    "description": "The interpretation of the question this answer commits to.",
                },
                "rejected_readings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Alternative readings that were considered. Each entry "
                    "carries the value that reading produced when computed -- a reading "
                    "you did not compute is not rejected, it is ignored -- or states that "
                    "it is structurally impossible (no such column/field). Why-summaries "
                    "belong in reason. Empty only if the question admits a single reading.",
                },
                "reason": {
                    "type": "string",
                    "description": "The evidence that decided the adjudication: which columns/"
                    "probes support the selected reading and what disqualified the others. "
                    "When the candidates are competing rules or explanations, evidence means "
                    "population base rates computed from the data -- for each candidate, how "
                    "many of ALL records trigger it (one triggered by nearly everything, or "
                    "nothing, is background noise, not the answer) -- and whether its scope "
                    "actually covers this case. When the answer selects a winner by ranking "
                    "or superlative, state the winner's margin over the runner-up: a wide "
                    "margin makes the pick robust to residual set drift, a hairline one "
                    "means the predicate deserves one more scrub before delivery.",
                },
                "excluded_populations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Every predicate you applied that the question did not "
                    "state -- rows skipped because their shape looked malformed, synthetic, "
                    "duplicated, secondary or off-topic. One entry each, carrying how many "
                    "rows or entities it removed AND what the answer becomes without it. "
                    "Empty list only if the answer was computed over exactly the population "
                    "the question describes. This is the field that catches the error no "
                    "reviewer can see from the outside: the rows that never reached the "
                    "result leave no trace in it.",
                },
            },
            "required": ["rows", "selected_reading", "reason", "excluded_populations"],
        }

    async def execute(
        self,
        rows: list[str],
        selected_reading: str,
        reason: str,
        rejected_readings: list[str] | None = None,
        excluded_populations: list[str] | None = None,
    ) -> str:
        rows = [str(r).rstrip("\n") for r in (rows or []) if str(r).strip()]
        if not rows:
            return "[SUBMIT_ANSWER REFUSED] rows is empty; pass every item of the result."
        if len(rows) > _MAX_ROWS:
            return f"[SUBMIT_ANSWER REFUSED] {len(rows)} rows exceeds {_MAX_ROWS}."
        if len((reason or "").strip()) < _MIN_REASON_CHARS:
            return (
                "[SUBMIT_ANSWER REFUSED] reason is too thin to count as an adjudication; "
                "state the evidence (columns, probe results) that decided between readings."
            )
        if not (selected_reading or "").strip():
            return "[SUBMIT_ANSWER REFUSED] selected_reading is required."
        advisories: list[str] = []

        def gate(gate_id: str, message: str) -> str | None:
            """Route one fired gate through the profile: refuse returns the
            blocking reply, warn records an advisory that rides on the write."""
            mode = self._profile.gate_mode(gate_id)
            if mode == "refuse":
                return f"[SUBMIT_ANSWER REFUSED] {message}"
            if mode == "warn":
                advisories.append(message)
            return None

        # One in-session refusal while the tools are still in hand beats any
        # post-hoc repair: a gate can reformat a delivered answer but cannot
        # compute the fields the agent never fetched. Refuse once, then defer
        # to the agent's judgment (some questions genuinely have few groups).
        if (
            len(rows) < self._profile.thresholds.for_each_min_rows
            and not self._for_each_warned
            and self._profile.gate_mode("for_each") != "off"
        ):
            question = self._read_question()
            if question and _FOR_EACH_RE.search(question):
                self._for_each_warned = True
                refused = gate(
                    "for_each",
                    "the question says 'for each' a group, yet "
                    f"only {len(rows)} rows are delivered. 'For each G' means one rows "
                    "element per G present in the data, each carrying every field the "
                    "question asks (names/titles included) -- a best/highest/latest "
                    "selects WITHIN each group, it never filters which groups appear. "
                    "Recompute the full per-group result, then submit again. If the data "
                    "truly contains fewer than 3 groups, resubmit as is.",
                )
                if refused:
                    return refused
        # The R18a failure mode is not a bad adjudication but a missing one: the
        # agent never suspects a second reading exists, leaves rejected_readings
        # empty, and every reason-side gate is bypassed. A short answer with no
        # alternatives is where that mode concentrates, so refuse once while the
        # tools are still in hand, then defer to the agent's judgment.
        if (
            not rejected_readings
            and len(rows) <= self._profile.thresholds.single_reading_max_rows
            and not self._single_reading_warned
            and self._profile.gate_mode("single_reading") != "off"
        ):
            self._single_reading_warned = True
            refused = gate(
                "single_reading",
                f"a {len(rows)}-row answer delivered with no "
                "alternative readings usually means none were looked for, not that none "
                "exist. Walk the reading families -- record-weighted vs entity-weighted, "
                "exact vs contains matching, raw vs deduplicated grain, strict vs lenient "
                "predicate, time-window anchoring -- and compute any that is defensible "
                "for this question (each listed in rejected_readings with the value it "
                "produced). If the question truly reads only one way, resubmit as is.",
            )
            if refused:
                return refused
        # An adjudication between readings is only evidence-backed if it cites
        # measured quantities. Single-record inspection produces prose; probes
        # produce numbers -- population-wide counts or rates for each reading
        # (how often each candidate fires across ALL records, not just the
        # target) are what separates the real hit from plausible background.
        if rejected_readings and len(_NUMBER_RE.findall(reason or "")) < 2:
            refused = gate(
                "numbers_in_reason",
                "the adjudication rejects alternative readings but "
                "cites no measured quantities. Run the probes and put their numbers in "
                "reason: population-wide counts or rates per candidate reading (a candidate "
                "that fires on nearly every record, or on none, is background, not the "
                "answer), then submit again.",
            )
            if refused:
                return refused
        uncomputed = [
            str(r)
            for r in (rejected_readings or [])
            if not _NUMBER_RE.search(str(r)) and not _STRUCTURAL_RE.search(str(r))
        ]
        if uncomputed:
            preview = "; ".join(u[:80] for u in uncomputed[:2])
            refused = gate(
                "uncomputed_rejections",
                f"{len(uncomputed)} rejected reading(s) carry no "
                f"computed value: {preview!r}. A reading you did not compute is not "
                "rejected -- it is ignored. Rerun each as a query and put the value it "
                "produced inside its entry (or state that its column/field does not exist "
                "in the data), then submit again.",
            )
            if refused:
                return refused
        # An exclusion the question never asked for silently redefines the
        # population, and nothing in the delivered rows shows it happened: the
        # dropped rows leave no trace in the result. Declaring one is therefore
        # only half the job -- the answer without it has to exist as a number
        # before the choice between them can be called a decision.
        unmeasured = [str(e) for e in (excluded_populations or []) if not _NUMBER_RE.search(str(e))]
        if unmeasured:
            preview = "; ".join(u[:80] for u in unmeasured[:2])
            refused = gate(
                "unmeasured_exclusions",
                f"{len(unmeasured)} self-imposed exclusion(s) are "
                f"declared without measurement: {preview!r}. For each one, report how many "
                "rows or entities it removed and what the answer becomes when those rows "
                "stay in -- an exclusion whose effect you have not computed is a guess "
                "about which data belongs to the question. Then submit again.",
            )
            if refused:
                return refused

        import json

        path = Path(self._answer_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        record = {
            "selected_reading": selected_reading.strip(),
            "rejected_readings": [str(r) for r in (rejected_readings or [])],
            "excluded_populations": [str(e) for e in (excluded_populations or [])],
            "reason": reason.strip(),
            "rows_delivered": len(rows),
        }
        adjudication = path.parent / "adjudication.json"
        adjudication.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        out = (
            f"wrote {len(rows)} rows to {path}; adjudication recorded. Re-read the question "
            "once against the delivered rows: every part answered, at the right grain, in the "
            "right unit and format?"
        )
        if advisories:
            out += "".join(f"\n[ADVISORY] {a}" for a in advisories) + (
                "\n(The answer was written as delivered; if any advisory changes the "
                "result, submit again to replace it.)"
            )
        return out


    def _read_question(self) -> str:
        try:
            return Path(self._question_path).read_text(encoding="utf-8")
        except OSError:
            return ""


def make_submit_answer_tool(ctx: PluginContext) -> Tool:
    config = ctx.config or {}
    return SubmitAnswerTool(
        answer_path=str(config.get("answer_path") or "/workspace/answer.txt"),
        question_path=str(config.get("question_path") or "/workspace/question.txt"),
        profile=resolve_profile(config),
    )
