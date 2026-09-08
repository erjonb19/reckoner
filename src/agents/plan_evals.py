"""Eval harness for the plan-level matcher.

:mod:`agents.evals` scores the payer matcher, whose answer is a canonical name.
This scores the plan matcher, whose answer is one of four verdicts, so it needs
its own scoring: precision and recall are computed over ``MATCH`` alone, because
that is the only verdict that lets a pair be differenced and therefore the only
one that can put a wrong number in the mart.

The conventions are the payer harness's, for the same reasons:

* **Abstention is not an error.** ``UNKNOWN`` where a match was expected costs
  recall, not precision. A pair the matcher declines stays ``plan_unresolved``
  and is reported as unresolved; a pair it matches wrongly becomes a variance
  that reads like a finding.
* **Results are appended, never overwritten**, so a score is readable against
  the run before it.

One caveat belongs in the numbers rather than a footnote: the positive class is
small. Only Cigna's hospital vocabulary and payer network labels line up, so
there are 17 ``MATCH`` labels among 190. Precision on so few positives moves in
large steps, so treat the set as a regression guard as much as a benchmark.

The labels were proposed by a model and then reviewed and confirmed by a person,
which is why :attr:`PlanEvalSet.reviewed_share` is reported beside every score:
a number measured against unreviewed labels says how well a matcher agrees with
whoever proposed them, which is not the same claim.

The set itself is the source of truth and is edited by hand from here. It was
seeded from the real plan strings in the lake, but no generator is shipped: one
would overwrite a reviewer's corrections the next time it ran, which is the
opposite of what a labelled set is for.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from agents.plan_resolution import PlanVerdict, resolve_plan

#: Separates the hospital plan from the payer network inside a label key.
KEY_SEPARATOR = " || "


@dataclass(frozen=True)
class PlanLabel:
    """One decision about what a hospital plan means against a payer network."""

    key: str
    expected: str
    labelled_by: str = ""
    labelled_on: str = ""
    #: False until a person has signed the label off. Model-proposed labels are
    #: a starting point, and a score against unreviewed labels measures
    #: agreement with the proposer as much as correctness.
    reviewed: bool = False
    note: str = ""

    @property
    def plan_raw(self) -> str:
        return self.key.partition(KEY_SEPARATOR)[0]

    @property
    def network(self) -> str:
        return self.key.partition(KEY_SEPARATOR)[2]


@dataclass
class PlanEvalSet:
    name: str
    labels: list[PlanLabel] = field(default_factory=list)

    @property
    def reviewed_share(self) -> float:
        if not self.labels:
            return 0.0
        return sum(1 for x in self.labels if x.reviewed) / len(self.labels)

    @classmethod
    def load(cls, path: Path) -> PlanEvalSet:
        labels = []
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    labels.append(PlanLabel(**json.loads(line)))
        return cls(path.stem, labels)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for label in self.labels:
                handle.write(json.dumps(asdict(label), ensure_ascii=False) + "\n")


@dataclass
class PlanScore:
    """Precision and recall over ``MATCH``, plus where the other verdicts went."""

    matcher: str
    eval_set: str
    total: int = 0
    #: Called it a match, and it was one. Licenses a comparison, correctly.
    true_positives: int = 0
    #: Called it a match when it was not. The expensive error: it puts a
    #: difference between two unrelated contracts into the mart as a variance.
    false_positives: int = 0
    #: Missed a real match. Costs coverage, and the pair stays unresolved.
    false_negatives: int = 0
    true_negatives: int = 0
    #: Right that it is not a match, wrong about which kind of non-match.
    confused_non_matches: int = 0
    reviewed_share: float = 0.0
    recorded_at: str = ""

    @property
    def precision(self) -> float:
        called = self.true_positives + self.false_positives
        return self.true_positives / called if called else 0.0

    @property
    def recall(self) -> float:
        actual = self.true_positives + self.false_negatives
        return self.true_positives / actual if actual else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def accuracy(self) -> float:
        correct = self.true_positives + self.true_negatives
        return correct / self.total if self.total else 0.0

    def describe(self) -> str:
        caveat = "" if self.reviewed_share == 1.0 else f" [{self.reviewed_share:.0%} reviewed]"
        return (
            f"{self.matcher:<14} P={self.precision:.3f} R={self.recall:.3f} "
            f"F1={self.f1:.3f} acc={self.accuracy:.3f} "
            f"(TP={self.true_positives} FP={self.false_positives} "
            f"FN={self.false_negatives} confused={self.confused_non_matches})"
            f"{caveat}"
        )


def score_plan_matcher(
    eval_set: PlanEvalSet, name: str = "rule-based"
) -> tuple[PlanScore, list[tuple[PlanLabel, str]]]:
    """Score the matcher and return the disagreements alongside the numbers.

    The disagreements are returned rather than counted away: with a positive
    class this small, the list of what it got wrong is more informative than the
    F1, and it is what a reviewer needs to fix either the matcher or the label.
    """
    score = PlanScore(
        matcher=name,
        eval_set=eval_set.name,
        total=len(eval_set.labels),
        reviewed_share=eval_set.reviewed_share,
        recorded_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    misses: list[tuple[PlanLabel, str]] = []

    for label in eval_set.labels:
        got = resolve_plan(label.plan_raw, label.network).verdict
        expected_match = label.expected == str(PlanVerdict.MATCH)
        got_match = got is PlanVerdict.MATCH

        if expected_match and got_match:
            score.true_positives += 1
        elif got_match and not expected_match:
            score.false_positives += 1
            misses.append((label, str(got)))
        elif expected_match and not got_match:
            score.false_negatives += 1
            misses.append((label, str(got)))
        else:
            score.true_negatives += 1
            if str(got) != label.expected:
                score.confused_non_matches += 1
                misses.append((label, str(got)))
    return score, misses


def append_result(score: PlanScore, path: Path) -> None:
    """Append one score to the history. Never rewrites an earlier run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(score), ensure_ascii=False) + "\n")


def gate(score: PlanScore, min_precision: float = 0.95) -> tuple[bool, str]:
    """Whether a matcher is fit to run against the mart.

    Precision is the gate and recall is not. A missed match leaves a pair
    ``plan_unresolved``, which is visible and honest; a false match puts a
    variance between two different contracts into the mart, where it reads as a
    finding. The two errors are not symmetric, so the threshold is not either.
    """
    if score.true_positives + score.false_positives == 0:
        return False, "the matcher never matched anything; nothing to gate on"
    if score.precision < min_precision:
        return False, (
            f"precision {score.precision:.3f} below {min_precision:.2f}: "
            f"{score.false_positives} pairs would be differenced wrongly"
        )
    return True, f"precision {score.precision:.3f}, recall {score.recall:.3f}"


def load_default(root: Path = Path("evals")) -> PlanEvalSet:
    return PlanEvalSet.load(root / "plan_matching.jsonl")


__all__ = [
    "KEY_SEPARATOR",
    "PlanEvalSet",
    "PlanLabel",
    "PlanScore",
    "append_result",
    "gate",
    "load_default",
    "score_plan_matcher",
]
