"""Eval harness for A1: score any triager against a labelled CSV.

The label file is ``evals/triage_labels.csv``. It ships empty -- a header and no
rows -- because the labels are a human's judgement and nobody has made them yet.
``docs/labelling-a1.md`` says how to fill it; ``--worksheet`` writes a file to
fill from the current triage queue.

Conventions, shared with the A2 harnesses:

* **No labels means not measured, never 0% or 100%.** A score over an empty set
  is reported with status ``no_labels`` and no accuracy at all. Printing
  ``accuracy 0.000`` would read as a result.
* **Abstention is not an error.** ``insufficient_evidence``, and anything routed
  to a human, lowers coverage, not precision. The expensive error is a
  confident wrong cause, because that is the one that reaches a report.
* **Precision gates, coverage does not.** Same asymmetry as the plan matcher.
* **Results are appended.** ``evals/triage_results.jsonl`` is a history.
* **A label must match a queue row to count.** Gold moves between runs; a label
  whose finding is no longer in the queue is reported as unmatched rather than
  scored against nothing.

Run it::

    python -m agents.triage_evals --queue summary/triage_queue.csv
    python -m agents.triage_evals --queue summary/triage_queue.csv --worksheet out.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from agents.triage_agent import (
    CAUSES,
    EVIDENCE_FIELDS,
    KEY_FIELDS,
    CallLog,
    Cause,
    Outcome,
    RuleTriager,
    item_key,
)

LABELS = Path("evals/triage_labels.csv")
RESULTS = Path("evals/triage_results.jsonl")

#: The label file's columns: the finding's identity, then the human's answer.
LABEL_COLUMNS: tuple[str, ...] = (
    *KEY_FIELDS,
    "expected_cause",
    "labelled_by",
    "labelled_on",
    "note",
)

#: Below this precision a triager is not fit to write causes into a report.
MIN_PRECISION = 0.90


class Triager(Protocol):
    name: str

    def triage(self, rows: Sequence[dict[str, Any]]) -> list[Outcome]: ...


@dataclass(frozen=True)
class TriageLabel:
    key: str
    expected_cause: str
    labelled_by: str = ""
    labelled_on: str = ""
    note: str = ""


class LabelError(ValueError):
    """A label file that cannot be trusted. Raised, never skipped."""


def load_labels(path: Path) -> list[TriageLabel]:
    """Read the label file. Rows with a blank ``expected_cause`` are unlabelled.

    A worksheet half filled in is a normal state, so blank rows are skipped. A
    cause outside the vocabulary is not -- it is a typo that would otherwise be
    scored as a disagreement -- so it stops the run and names the line.
    """
    if not path.exists():
        return []
    labels: list[TriageLabel] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in ("expected_cause", *KEY_FIELDS) if c not in (reader.fieldnames or [])]
        if missing:
            raise LabelError(f"{path}: missing columns {', '.join(missing)}")
        for line, row in enumerate(reader, start=2):
            cause = (row.get("expected_cause") or "").strip()
            if not cause:
                continue
            if cause not in CAUSES:
                raise LabelError(
                    f"{path}:{line}: expected_cause {cause!r} is not one of {', '.join(CAUSES)}"
                )
            labels.append(
                TriageLabel(
                    key=item_key(row),
                    expected_cause=cause,
                    labelled_by=(row.get("labelled_by") or "").strip(),
                    labelled_on=(row.get("labelled_on") or "").strip(),
                    note=(row.get("note") or "").strip(),
                )
            )
    keys = [label.key for label in labels]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise LabelError(f"{path}: the same finding is labelled twice: {duplicates[0]}")
    return labels


#: The fixed seed for :func:`sample_queue`, so a sample is the same sample on
#: every machine and every rerun, and a resumed run keeps its findings.
SAMPLE_SEED = 20260926


def sample_queue(
    rows: Sequence[dict[str, Any]], size: int = 60, seed: int = SAMPLE_SEED
) -> list[dict[str, Any]]:
    """Every finding the rules left ``unexplained``, and a stratified spread of the rest.

    The unexplained rows are the ones the agent exists for, so all of them go
    in. The remaining places are shared across (rule, system) strata in
    proportion to their size, largest remainder first, and drawn with a fixed
    seed. It reads only the queue's own columns, never the labels: a sample
    chosen by looking at the answers would measure the choice.
    """
    import random

    rows = sorted(rows, key=item_key)
    unexplained = [r for r in rows if r.get("triage_rule") == "unexplained"]
    rest = [r for r in rows if r.get("triage_rule") != "unexplained"]
    places = max(size - len(unexplained), 0)
    if places >= len(rest):
        return unexplained + rest
    strata: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rest:
        stratum = (str(row.get("triage_rule") or ""), str(row.get("system") or ""))
        strata.setdefault(stratum, []).append(row)
    shares = {k: places * len(v) / len(rest) for k, v in strata.items()}
    counts = {k: int(share) for k, share in shares.items()}
    by_remainder = sorted(strata, key=lambda k: (-(shares[k] - counts[k]), k))
    for stratum in by_remainder[: places - sum(counts.values())]:
        counts[stratum] += 1
    rng = random.Random(seed)
    chosen = [r for k in sorted(strata) for r in rng.sample(strata[k], counts[k])]
    return unexplained + sorted(chosen, key=item_key)


def load_queue(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@dataclass
class TriageScore:
    triager: str
    status: str = "no_labels"
    labels: int = 0
    unmatched_labels: int = 0
    scored: int = 0
    #: A cause was given, it was not an abstention, and it was right.
    correct: int = 0
    #: A cause was given and it was wrong. The expensive error.
    wrong: int = 0
    #: Abstained or routed to a human.
    abstained: int = 0
    #: Of those, the findings the triager sent to the human queue: refused,
    #: low confidence or out of attempts. The rules never route one.
    routed_to_human: int = 0
    #: Findings whose call failed outright (a wrong model, a rejected key). Not
    #: abstentions: the triager was never asked. Any at all and there is no
    #: score -- 250 fatal 404s were once reported as 250 abstentions and a
    #: coverage figure (silent failure #15).
    errors: int = 0
    first_error: str = ""
    per_cause: dict[str, dict[str, int]] = field(default_factory=dict)
    confusion: dict[str, dict[str, int]] = field(default_factory=dict)
    calls: int = 0
    cost_usd: float | None = 0.0
    mean_latency_ms: float = 0.0
    recorded_at: str = ""
    #: What produced the answers. A model's score is a fact about that model:
    #: another model, or the same one on another day, is another result.
    provider: str = ""
    model: str = ""
    billing: str = ""
    #: Empty for the whole labelled queue; otherwise what the sample is. A
    #: sample's precision is not the queue's, and the record says which it is.
    sample: str = ""

    @property
    def precision(self) -> float | None:
        answered = self.correct + self.wrong
        if self.status != "scored" or not answered:
            return None
        return self.correct / answered

    @property
    def coverage(self) -> float | None:
        if self.status != "scored" or not self.scored:
            return None
        return (self.correct + self.wrong) / self.scored

    def summary(self) -> str:
        if self.status == "no_labels":
            return (
                f"{self.triager:<6} not measured: no labelled findings "
                f"({self.unmatched_labels} labels did not match the queue)"
            )
        if self.status == "errored":
            return (
                f"{self.triager:<6} not measured: {self.errors} of {self.scored} findings "
                f"never got an answer. First error: {self.first_error[:240]}"
            )
        precision = "n/a" if self.precision is None else f"{self.precision:.3f}"
        coverage = "n/a" if self.coverage is None else f"{self.coverage:.3f}"
        cost = "unknown" if self.cost_usd is None else f"${self.cost_usd:.4f}"
        scope = f"[SAMPLE: {self.sample}] " if self.sample else ""
        return (
            f"{self.triager:<6} {scope}precision={precision} coverage={coverage} "
            f"(correct={self.correct} wrong={self.wrong} abstained={self.abstained} "
            f"of {self.scored}; {self.routed_to_human} to human; "
            f"{self.unmatched_labels} unmatched) "
            f"calls={self.calls} cost={cost} latency={self.mean_latency_ms:.0f}ms"
        )

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["precision"] = self.precision
        record["coverage"] = self.coverage
        return record


def score(
    triager: Triager,
    queue: Sequence[dict[str, Any]],
    labels: Sequence[TriageLabel],
    *,
    log: CallLog | None = None,
) -> TriageScore:
    """Run ``triager`` over the labelled findings only, and score it.

    Only labelled rows are sent: an LLM triager is billed per finding, and
    triaging the whole queue to score a subset would spend money on rows that
    cannot move the number.
    """
    result = TriageScore(
        triager=triager.name,
        labels=len(labels),
        recorded_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    by_key = {item_key(row): row for row in queue}
    matched = [label for label in labels if label.key in by_key]
    result.unmatched_labels = len(labels) - len(matched)
    if not matched:
        return result

    rows = [by_key[label.key] for label in matched]
    outcomes = {o.key: o for o in triager.triage(rows)}
    result.status = "scored"
    result.scored = len(matched)

    for label in matched:
        outcome = outcomes.get(label.key)
        if outcome is not None and outcome.errored:
            result.errors += 1
            result.first_error = result.first_error or outcome.reason
            continue
        proposal = outcome.proposal if outcome and outcome.accepted else None
        if outcome is not None and not outcome.accepted:
            result.routed_to_human += 1
        got = proposal.cause if proposal else "routed_to_human"
        result.confusion.setdefault(label.expected_cause, {})
        result.confusion[label.expected_cause][got] = (
            result.confusion[label.expected_cause].get(got, 0) + 1
        )
        tally = result.per_cause.setdefault(label.expected_cause, {"labelled": 0, "correct": 0})
        tally["labelled"] += 1

        if proposal is None or proposal.cause == Cause.INSUFFICIENT_EVIDENCE:
            result.abstained += 1
        elif proposal.cause == label.expected_cause:
            result.correct += 1
            tally["correct"] += 1
        else:
            result.wrong += 1

    if result.errors:
        result.status = "errored"
    if log is not None:
        summary = log.summary()
        result.calls = int(summary["calls"])
        result.cost_usd = summary["cost_usd"]
        result.mean_latency_ms = float(summary["mean_latency_ms"])
    return result


def gate(result: TriageScore, min_precision: float = MIN_PRECISION) -> tuple[bool, str]:
    """Whether a triager may write causes into the report.

    Nothing passes without labels. That is the gate working, not failing: an
    agent that has never been measured has not earned a place in the output.
    """
    if result.status == "no_labels":
        return False, "not measured: there are no labelled findings yet"
    if result.status == "errored":
        return False, f"not measured: {result.errors} calls failed before any answer"
    if result.precision is None:
        return False, "the triager abstained on every labelled finding"
    if result.precision < min_precision:
        return False, (
            f"precision {result.precision:.3f} below {min_precision:.2f}: "
            f"{result.wrong} findings would carry the wrong cause"
        )
    return True, f"precision {result.precision:.3f}, coverage {result.coverage:.3f}"


def append_result(result: TriageScore, path: Path = RESULTS) -> None:
    if result.status == "errored":
        # The last line of defence: history is what a README number is read
        # from, and a run whose calls failed has no number to give it.
        raise ValueError(f"refusing to record an errored run: {result.first_error[:200]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result.to_record(), ensure_ascii=False) + "\n")


def write_worksheet(queue: Sequence[dict[str, Any]], path: Path) -> int:
    """A label file prefilled with every finding, the answer column blank.

    The rule's verdict is included as a hint column *after* the answer, so a
    labeller sees it -- and so the scorer, which reads only ``expected_cause``,
    never does.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    context = [c for c in EVIDENCE_FIELDS if c not in KEY_FIELDS]
    columns = [*LABEL_COLUMNS, *context, "triage_rule"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in queue:
            writer.writerow({**{c: row.get(c, "") for c in columns}, "expected_cause": ""})
    return len(queue)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--queue", type=Path, default=Path("summary/triage_queue.csv"))
    parser.add_argument("--labels", type=Path, default=LABELS)
    parser.add_argument("--worksheet", type=Path, help="write a label worksheet and stop")
    parser.add_argument("--record", action="store_true", help=f"append the score to {RESULTS}")
    args = parser.parse_args(argv)

    queue = load_queue(args.queue)
    if args.worksheet:
        written = write_worksheet(queue, args.worksheet)
        print(f"worksheet: {written} findings -> {args.worksheet}")
        return 0

    # Only the deterministic baseline runs from here. The LLM triager spends
    # money on every finding, so it is constructed deliberately in code, never
    # from a flag that is easy to pass by accident.
    labels = load_labels(args.labels)
    result = score(RuleTriager(), queue, labels)
    print(result.summary())
    passed, why = gate(result)
    print(f"gate: {'pass' if passed else 'hold'} -- {why}")
    if args.record and result.status == "scored":
        append_result(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "LABELS",
    "LABEL_COLUMNS",
    "MIN_PRECISION",
    "RESULTS",
    "SAMPLE_SEED",
    "LabelError",
    "TriageLabel",
    "TriageScore",
    "append_result",
    "gate",
    "load_labels",
    "load_queue",
    "main",
    "sample_queue",
    "score",
    "write_worksheet",
]
