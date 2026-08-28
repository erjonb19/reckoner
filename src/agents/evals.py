"""Eval harness for the entity-resolution agent.

An agent without an eval is an anecdote. This scores any matcher against a
labelled set and reports precision, recall and F1 -- separately from accuracy,
because the classes are unbalanced and accuracy alone would flatter a matcher
that answers "unknown" to everything.

Two conventions worth stating:

* **Abstention is not an error.** A matcher that declines an ambiguous string
  costs recall, not precision. That is the trade we want: a wrong canonical
  payer silently merges two contracts, while an abstention lands in the review
  queue where a human sees it.
* **Results are appended, never overwritten.** A score is only meaningful
  against the run before it, so `results.jsonl` is a history.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from agents.entity_resolution import (
    CallStats,
    Matcher,
    PayerCandidate,
    validate_proposal,
)


@dataclass(frozen=True)
class Label:
    """One human decision: what this raw string actually resolves to."""

    key: str
    canonical_payer: str | None
    labelled_by: str = ""
    labelled_on: str = ""
    note: str = ""

    @property
    def payer_raw(self) -> str:
        return self.key.partition(" || ")[0]

    @property
    def plan_raw(self) -> str:
        return self.key.partition(" || ")[2]


@dataclass
class EvalSet:
    name: str
    labels: list[Label] = field(default_factory=list)

    @property
    def by_key(self) -> dict[str, Label]:
        return {label.key: label for label in self.labels}

    def candidates(self) -> list[PayerCandidate]:
        return [PayerCandidate(payer_raw=x.payer_raw, plan_raw=x.plan_raw) for x in self.labels]

    @classmethod
    def load(cls, path: Path) -> EvalSet:
        labels = []
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    row = json.loads(line)
                    labels.append(Label(**row))
        return cls(path.stem, labels)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for label in self.labels:
                handle.write(json.dumps(asdict(label), ensure_ascii=False) + "\n")


@dataclass
class Score:
    matcher: str
    eval_set: str
    total: int = 0
    #: Predicted a payer, and it was right.
    true_positives: int = 0
    #: Predicted a payer, and it was wrong. The expensive error.
    false_positives: int = 0
    #: Abstained or was rejected, where a payer was expected.
    false_negatives: int = 0
    #: Correctly abstained where the truth is "unknown".
    true_negatives: int = 0
    rejected_by_validator: int = 0
    routed_to_review: int = 0
    cost_usd: float = 0.0
    mean_latency_ms: float = 0.0
    errors: int = 0
    recorded_at: str = ""

    @property
    def precision(self) -> float:
        predicted = self.true_positives + self.false_positives
        return self.true_positives / predicted if predicted else 0.0

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

    @property
    def coverage(self) -> float:
        """Share of candidates answered without needing a human."""
        return 1 - (self.routed_to_review / self.total) if self.total else 0.0

    def summary(self) -> str:
        return (
            f"{self.matcher:<12} P={self.precision:.3f} R={self.recall:.3f} "
            f"F1={self.f1:.3f} acc={self.accuracy:.3f} coverage={self.coverage:.3f} "
            f"cost=${self.cost_usd:.4f} latency={self.mean_latency_ms:.0f}ms"
        )


def score_matcher(
    matcher: Matcher,
    eval_set: EvalSet,
    review_threshold: float = 0.80,
    stats: CallStats | None = None,
) -> Score:
    """Run a matcher over a labelled set and score it."""
    candidates = eval_set.candidates()
    truth = eval_set.by_key
    proposals = {p.key: p for p in matcher.propose(candidates)}

    result = Score(
        matcher=matcher.name,
        eval_set=eval_set.name,
        total=len(candidates),
        recorded_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )

    for candidate in candidates:
        expected = truth[candidate.key].canonical_payer
        proposal = proposals.get(candidate.key)
        if proposal is None:
            result.false_negatives += 1 if expected else 0
            result.true_negatives += 0 if expected else 1
            result.routed_to_review += 1
            continue

        validation = validate_proposal(proposal, candidate)
        if not validation.accepted:
            result.rejected_by_validator += 1
        needs_review = not validation.accepted or proposal.confidence < review_threshold
        if needs_review:
            result.routed_to_review += 1

        # A rejected or abstaining proposal predicts nothing.
        predicted = proposal.canonical_payer if validation.accepted else None

        if predicted and expected and predicted == expected:
            result.true_positives += 1
        elif predicted and predicted != expected:
            result.false_positives += 1
        elif not predicted and expected:
            result.false_negatives += 1
        else:
            result.true_negatives += 1

    call_stats = stats or getattr(matcher, "stats", None)
    if call_stats is not None:
        result.cost_usd = call_stats.cost_usd
        result.mean_latency_ms = call_stats.mean_latency_ms
        result.errors = call_stats.errors
    return result


def compare(
    matchers: Sequence[Matcher], eval_set: EvalSet, review_threshold: float = 0.80
) -> list[Score]:
    """Score several matchers on the same set, best F1 first."""
    scores = [score_matcher(m, eval_set, review_threshold) for m in matchers]
    return sorted(scores, key=lambda s: -s.f1)


def beats_baseline(candidate: Score, baseline: Score, min_gain: float = 0.02) -> tuple[bool, str]:
    """Whether a matcher earns its place over the deterministic baseline.

    A gain inside the noise is not a gain, and a precision regression is
    disqualifying regardless of F1 -- a wrong merge is worse than an abstention.
    """
    if candidate.precision < baseline.precision:
        return False, (f"precision regressed {baseline.precision:.3f} -> {candidate.precision:.3f}")
    gain = candidate.f1 - baseline.f1
    if gain < min_gain:
        return False, f"F1 gain {gain:+.3f} is below the {min_gain:.2f} threshold"
    return True, f"F1 {baseline.f1:.3f} -> {candidate.f1:.3f} (+{gain:.3f}) at no precision cost"


def append_results(path: Path, scores: Sequence[Score]) -> None:
    """Append to the results history. Never overwrite -- trend is the point."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for score in scores:
            row = asdict(score)
            row.update(
                precision=score.precision,
                recall=score.recall,
                f1=score.f1,
                accuracy=score.accuracy,
                coverage=score.coverage,
            )
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_results(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]
