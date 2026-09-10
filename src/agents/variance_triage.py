"""A1: turn surviving variances into a queue someone can actually work.

:func:`reconcile.variance.explain` rules out the causes that can be ruled out
deterministically -- vintage, entity resolution, granularity, unresolved plans, a
systematic offset. What is left is labelled ``unexplained``, and on one shard of
one system that is 2,070 rows. Handing a person 2,070 rows is the same as
handing them nothing.

Unlike A3 and A4, this one has a real long tail to work on. The numbers below are
measured, from Mount Sinai against five carriers on shard 7:

* 2,070 unexplained and material rows collapse to **718 distinct
  (code, code type, carrier)** triples. The inflation is not the facility
  fan-out alone -- it is mostly plan multiplicity, since one service and carrier
  appear under several plan pairings.
* Of the 224 services appearing more than once, only 28% hold a ratio spread
  under 10%. **The repeats are not duplicates.** Collapsing each service to a
  single number would silently average away real plan-level variation, so the
  spread is kept and reported rather than removed.
* 88.6% of the residual belongs to a single carrier. A queue that lists those as
  1,834 independent findings is describing one relationship 1,834 times.

So the unit of triage is the service and carrier, and the shape of the spread
within it is the finding:

* a **tight** spread across several plans is one fact about a contract;
* a **wide** spread is a claim about plans differing from each other, which is a
  different question and often a different owner;
* a **single** observation is the weakest evidence there is and should not
  outrank either.

**The agent half is not built.** The build order in CLAUDE.md puts deterministic
work first, and the deterministic work is what produces the queue an agent would
read. What is deliberately absent is the model that would look at a triage item
and propose *why* -- a units mismatch, a carve-out, a genuinely different
contract. That needs labels, and the labels are what this queue produces once a
human works it. The seam is :class:`Triager`, and the queue is the eval set.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from reconcile.variance import Explanation, Variance

#: Ratio spread, relative to the median, under which several observations of one
#: service are treated as saying the same thing. Measured: 28% of repeated
#: services sit below this, so it separates a real minority rather than being a
#: threshold that captures everything or nothing.
TIGHT_SPREAD = 0.10

#: Below this many observations there is no spread to speak of and no weight
#: behind the item.
MIN_OBSERVATIONS_FOR_A_PATTERN = 2


class TriageClass(StrEnum):
    """What kind of thing one service-and-carrier group is."""

    #: Several plans, all saying the same thing. One contract-level fact.
    CONSISTENT_ACROSS_PLANS = "consistent_across_plans"
    #: Several plans disagreeing with each other as much as with the hospital.
    #: The question is about plans, not about this service.
    PLAN_DEPENDENT = "plan_dependent"
    #: One observation. The weakest evidence available.
    SINGLE_OBSERVATION = "single_observation"


@dataclass(frozen=True)
class TriageItem:
    """One service and carrier, with the evidence behind it. Guardrail 3."""

    code: str
    code_type: str | None
    payer: str
    triage_class: TriageClass
    observations: int
    median_ratio: float
    spread: float
    payer_higher: int
    hospital_higher: int
    facilities: tuple[str, ...] = ()
    plans: tuple[str, ...] = ()

    @property
    def direction(self) -> str:
        if self.payer_higher and not self.hospital_higher:
            return "payer higher"
        if self.hospital_higher and not self.payer_higher:
            return "hospital higher"
        return "mixed"

    @property
    def magnitude(self) -> float:
        """How far from agreement, symmetric in direction.

        1.5x and 0.667x are the same size of disagreement and must rank together;
        using the raw ratio would sort every hospital-higher row below every
        payer-higher one regardless of size.
        """
        return max(self.median_ratio, 1 / self.median_ratio) if self.median_ratio else 1.0

    def describe(self) -> str:
        return (
            f"{self.code} ({self.code_type}) / {self.payer}: {self.median_ratio:.2f}x "
            f"{self.direction}, {self.observations} obs, spread {self.spread:.0%} "
            f"-- {self.triage_class}"
        )


class Triager(Protocol):
    """The seam an LLM triager would implement. Nothing does yet but the rules."""

    def triage(self, variances: Sequence[Variance]) -> list[TriageItem]: ...


def _classify(ratios: Sequence[float]) -> tuple[TriageClass, float]:
    if len(ratios) < MIN_OBSERVATIONS_FOR_A_PATTERN:
        return TriageClass.SINGLE_OBSERVATION, 0.0
    median = statistics.median(ratios)
    spread = (max(ratios) - min(ratios)) / median if median else 0.0
    if spread < TIGHT_SPREAD:
        return TriageClass.CONSISTENT_ACROSS_PLANS, spread
    return TriageClass.PLAN_DEPENDENT, spread


class RuleBasedTriager:
    """Group the residual by service and carrier, and rank it.

    This does not explain anything, and does not pretend to. It reduces 2,070
    rows to 718 items, says which have weight behind them, and puts the ones
    worth a person's attention first. Explaining is the agent's job, once these
    items have been worked into labels.
    """

    name = "rules"

    def triage(self, variances: Sequence[Variance]) -> list[TriageItem]:
        groups: dict[tuple[str, str | None, str], list[Variance]] = {}
        for row in variances:
            if not is_triageable(row):
                continue
            groups.setdefault((row.code, row.code_type, row.payer), []).append(row)

        items = []
        for (code, code_type, payer), rows in groups.items():
            ratios = [r.ratio for r in rows if r.ratio]
            triage_class, spread = _classify(ratios)
            items.append(
                TriageItem(
                    code=code,
                    code_type=code_type,
                    payer=payer,
                    triage_class=triage_class,
                    observations=len(rows),
                    median_ratio=statistics.median(ratios),
                    spread=spread,
                    payer_higher=sum(1 for r in ratios if r > 1),
                    hospital_higher=sum(1 for r in ratios if r < 1),
                    facilities=tuple(sorted({r.left.hospital for r in rows if r.left.hospital})),
                    plans=tuple(sorted({r.left.plan or "" for r in rows} - {""})),
                )
            )
        return sorted(items, key=_priority, reverse=True)


def _priority(item: TriageItem) -> tuple[float, int]:
    """Rank by how much disagreement, weighted by how much evidence.

    A single observation at 9x is a lead; five plans agreeing at 2x is a finding.
    Evidence is capped rather than multiplied so that a service appearing under
    many plans cannot outrank a larger disagreement purely on repetition -- the
    fan-out already inflates repetition and should not also buy priority.
    """
    weight = min(item.observations, 5) / 5
    return (item.magnitude * (0.5 + 0.5 * weight), item.observations)


def is_triageable(row: Variance) -> bool:
    """Rows the triage queue is built from: material, unexplained, comparable."""
    return row.explanation == str(Explanation.UNEXPLAINED) and row.is_material and bool(row.ratio)


@dataclass
class TriageQueue:
    """The worked output: what a person should look at, in order."""

    items: list[TriageItem] = field(default_factory=list)
    #: Rows that entered triage, not rows in the mart. Using the mart total
    #: would make the collapse ratio flattering and meaningless -- most of a
    #: mart is already explained and never reaches here.
    rows_in: int = 0

    @property
    def by_carrier(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.payer] = counts.get(item.payer, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    @property
    def concentration(self) -> float:
        """Share of the queue held by its largest carrier.

        Reported because a high number changes what the queue *is*: 88.6% in one
        carrier is one relationship to investigate, not hundreds of findings.
        """
        counts = self.by_carrier
        return max(counts.values()) / len(self.items) if self.items else 0.0

    def summary(self) -> dict[str, object]:
        classes: dict[str, int] = {}
        for item in self.items:
            classes[str(item.triage_class)] = classes.get(str(item.triage_class), 0) + 1
        return {
            "rows_in": self.rows_in,
            "items": len(self.items),
            "collapsed_by": round(self.rows_in / len(self.items), 1) if self.items else 0,
            "by_class": classes,
            "by_carrier": self.by_carrier,
            "largest_carrier_share": round(self.concentration, 3),
            "top": [item.describe() for item in self.items[:10]],
        }


def triage(variances: Iterable[Variance], triager: Triager | None = None) -> TriageQueue:
    """Reduce a mart's unexplained residual to a ranked queue."""
    rows = list(variances)
    worker = triager or RuleBasedTriager()
    return TriageQueue(
        items=worker.triage(rows), rows_in=sum(1 for row in rows if is_triageable(row))
    )


__all__ = [
    "MIN_OBSERVATIONS_FOR_A_PATTERN",
    "TIGHT_SPREAD",
    "RuleBasedTriager",
    "TriageClass",
    "TriageItem",
    "TriageQueue",
    "Triager",
    "is_triageable",
    "triage",
]
