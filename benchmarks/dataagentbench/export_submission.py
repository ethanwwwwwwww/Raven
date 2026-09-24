"""Build the leaderboard submission bundle from a finished run, and refuse to
build an incomplete one.

The rubric asks for three things: every one of the 5 runs of every query, the
answer for each, and the execution trace behind it. Two of those are easy to
lose quietly -- an attempt that produced no answer file still leaves a case
directory, and a harness that stops collecting transcripts leaves a directory
that merely looks empty. This script reads the run, writes the submission JSON
in the schema the README specifies, copies each attempt's transcript under a
stable name, and exits non-zero when anything is missing, so an incomplete
bundle cannot be handed over by accident.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

_CASE_RE = re.compile(r"^dab-(?P<dataset>.+)-query(?P<query>\d+)$")


def _trace_files(attempt_dir: Path) -> list[Path]:
    sessions = attempt_dir / "agent" / "sessions"
    if not sessions.is_dir():
        return []
    return sorted(p for p in sessions.rglob("*.jsonl") if ".lock" not in p.parts)


def _trace_texts(trace: Path) -> list[str]:
    """Every string the transcript carries: message contents and the decoded
    arguments of each tool call. Decoding matters -- comparing against the raw
    file text fails on escaping alone (a quote is doubled there, a dash may be
    a \\u escape), which looks exactly like a rewritten answer.
    """
    texts: list[str] = []
    for line in trace.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = record.get("content")
        if isinstance(content, str):
            texts.append(content)
        for call in record.get("tool_calls") or []:
            arguments = (call.get("function") or {}).get("arguments")
            if not isinstance(arguments, str):
                continue
            texts.append(arguments)
            try:
                parsed = json.loads(arguments)
            except json.JSONDecodeError:
                continue
            texts.append(json.dumps(parsed, ensure_ascii=False))
    return texts


def _matches_trace(answer: str, traces: list[Path]) -> bool:
    """Does the delivered answer appear in its own transcript?

    The rubric asks that submitted answers match the traces behind them. A
    multi-item answer is written as a rows list, so the whole blob is never one
    contiguous string in the transcript -- the first line is the probe.
    """
    if not answer or not traces:
        return False
    probe = answer.splitlines()[0].strip()[:60]
    if not probe:
        return False
    return any(probe in text for trace in traces for text in _trace_texts(trace))


def collect(run_dir: Path) -> tuple[list[dict], list[str]]:
    entries: list[dict] = []
    problems: list[str] = []
    rewritten: list[str] = []
    cases = sorted((run_dir / "cases").glob("dab-*"))
    if not cases:
        problems.append(f"no case directories under {run_dir / 'cases'}")
    for case in cases:
        match = _CASE_RE.match(case.name)
        if not match:
            problems.append(f"unparsable case directory name: {case.name}")
            continue
        dataset, query = match["dataset"], match["query"]
        attempts = sorted(case.glob("attempt_*"))
        if len(attempts) != 5:
            problems.append(f"{case.name}: {len(attempts)} attempts, expected 5")
        for attempt in attempts:
            index = int(attempt.name.split("_")[-1])
            answer_path = attempt / "benchmark" / "answer.txt"
            answer = answer_path.read_text(encoding="utf-8").strip() if answer_path.is_file() else ""
            if not answer:
                problems.append(f"{case.name}/{attempt.name}: no answer")
            traces = _trace_files(attempt)
            if not traces:
                problems.append(f"{case.name}/{attempt.name}: no execution trace")
            elif answer and not _matches_trace(answer, traces):
                # A delivered answer the transcript does not contain was
                # rewritten after the agent wrote it. The delivery gate is the
                # only thing that does that, and it records both the violation
                # and the pre-gate answer: with those the difference is
                # auditable and belongs in the bundle, so it is reported but
                # not an error. Without them it is unexplained, and shipping an
                # answer nothing accounts for is the thing to refuse.
                gate = attempt / "benchmark" / "gate.json"
                if gate.is_file():
                    rewritten.append(f"{case.name}/{attempt.name}")
                else:
                    problems.append(
                        f"{case.name}/{attempt.name}: answer absent from its trace, "
                        "and no gate record explains it"
                    )
            # query and run are numbers, matching every accepted submission in
            # the benchmark's own submissions/ directory. The README example
            # quotes them as strings, but a scorer comparing against integers
            # would silently match nothing.
            entries.append(
                {
                    "dataset": dataset,
                    "query": int(query),
                    "run": index - 1,
                    "answer": answer,
                    "_traces": [str(p) for p in traces],
                }
            )
    return entries, problems, rewritten


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output", required=True, help="submission JSON path")
    parser.add_argument("--traces-dir", help="copy transcripts here as <dataset>/query<N>/run<K>.jsonl")
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir).expanduser().resolve()
    entries, problems, rewritten = collect(run_dir)

    if args.traces_dir:
        root = Path(args.traces_dir).expanduser().resolve()
        for entry in entries:
            for offset, trace in enumerate(entry["_traces"]):
                suffix = "" if offset == 0 else f".{offset}"
                target = root / entry["dataset"] / f"query{entry['query']}" / f"run{entry['run']}{suffix}.jsonl"
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(trace, target)

    payload = [{k: v for k, v in entry.items() if not k.startswith("_")} for entry in entries]
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    traced = sum(1 for e in entries if e["_traces"])
    print(f"entries: {len(payload)} | with answer: {sum(1 for e in payload if e['answer'])} | with trace: {traced}")
    print(f"datasets: {len({e['dataset'] for e in payload})} | queries: {len({(e['dataset'], e['query']) for e in payload})}")
    print(f"submission: {output}")
    if rewritten:
        print(
            f"delivery gate reformatted {len(rewritten)} answer(s) after the agent wrote them; "
            "ship benchmark/gate.json and answer_original.txt for these so the change is auditable:"
        )
        for line in rewritten:
            print(f"  {line}")
    if problems:
        print(f"\n{len(problems)} problem(s):", file=sys.stderr)
        for line in problems[:20]:
            print(f"  {line}", file=sys.stderr)
        if len(problems) > 20:
            print(f"  ... and {len(problems) - 20} more", file=sys.stderr)
        return 0 if args.allow_incomplete else 1
    print("complete: 5 runs per query, every run answered and traced")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
