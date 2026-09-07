"""The variance mart: where two disclosures disagree, by how much, and why.

This is the deliverable docs/SPEC.md names. It runs in two modes, and they are
the same machinery pointed at different pairs:

* **Cross-source.** The same service, payer and hospital, as published by the
  hospital under 45 CFR 180 and by the payer under Transparency in Coverage.
  This is the headline: two federal disclosures of one negotiated rate.
* **Cross-hospital.** The same service and payer at different hospitals. This
  needs only the hospital side, so it runs today, and it is what makes a rate
  interpretable -- a number is high or low only against something.

Every row carries a **candidate explanation** rather than a verdict. A variance
is not evidence of a real price difference until timing, methodology and entity
resolution have been ruled out, and those three account for most of what looks
like disagreement. Assigning the explanation is agent A1's job; this module
produces the structured input it classifies and the deterministic explanations
that need no judgement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from statistics import median

from reconcile.comparability import (
    ComparableRate,
    NotComparable,
    can_compare,
    setting_key,
)
from reconcile.provenance import Provenance, parse_vintage


class Explanation(StrEnum):
    """Candidate explanations for a variance, in the order they must be ruled out."""

    #: The two files describe different points in time, *and* the size of the
    #: difference is one that drift over that gap could actually produce.
    VINTAGE_ARTIFACT = "vintage_artifact"
    #: The payer or plan strings were merged when they are different contracts.
    ENTITY_RESOLUTION_SUSPECT = "entity_resolution_suspect"
    #: One side aggregates several plans behind one name.
    GRANULARITY_MISMATCH = "granularity_mismatch"
    #: The two sides name plans, but from vocabularies that have not been matched
    #: to each other. Not a claim that the contracts differ -- a claim that we do
    #: not yet know whether they do.
    PLAN_UNRESOLVED = "plan_unresolved"
    #: Survived the deterministic checks. A real disagreement, or close to it.
    UNEXPLAINED = "unexplained"


#: Above this ratio a "variance" is more likely a unit or methodology error than
#: a negotiated difference. Rates ten times apart for the same service and payer
#: do not happen in a real contract; they happen when a per diem meets a case
#: rate, or when a decimal moved.
IMPLAUSIBLE_RATIO = 10.0

#: The most a negotiated rate could plausibly move in a year, as a share of
#: itself. Deliberately generous: this is not a claim about typical escalators,
#: which run at a few percent. It is the bound above which *timing cannot be the
#: explanation* -- a service whose price doubled in seven months did not do so by
#: drifting, it was rebased, recoded, or matched to the wrong contract.
#:
#: A bound is needed because a vintage gap on its own cannot discriminate.
#: Hospital files update at least annually and payer files monthly, so in
#: cross-source mode *every* pair has a large gap. Explaining a row by the gap
#: alone therefore sorts rows by which file they came from rather than by
#: anything about the rates: it labelled 72% of one Aetna run ``vintage_artifact``
#: purely because ``AetnaALIC`` sits 216 days from the hospital vintage while
#: ``Aetna_NY`` sits 155, and left ``unexplained`` empty.
PLAUSIBLE_ANNUAL_DRIFT = 0.40

#: A plan name that aggregates rather than identifies. One side publishing
#: "All Commercial Plans" against another's named plan is a granularity
#: mismatch, not a price difference.
_AGGREGATE_PLAN_CLASSES = frozenset({"commercial_aggregate", "ambiguous_all_products"})


@dataclass(frozen=True)
class Variance:
    """One measured disagreement between two rates for the same thing."""

    code: str
    code_type: str | None
    payer: str
    setting: str | None
    left: ComparableRate
    right: ComparableRate
    explanation: str = str(Explanation.UNEXPLAINED)
    notes: tuple[str, ...] = ()

    @property
    def left_rate(self) -> float:
        return self.left.rate_dollar or 0.0

    @property
    def right_rate(self) -> float:
        return self.right.rate_dollar or 0.0

    @property
    def difference(self) -> float:
        return self.right_rate - self.left_rate

    @property
    def ratio(self) -> float:
        return self.right_rate / self.left_rate if self.left_rate else 0.0

    @property
    def relative_difference(self) -> float:
        """Difference as a share of the lower rate, so it is symmetric in sign."""
        base = min(self.left_rate, self.right_rate)
        return abs(self.difference) / base if base else 0.0

    @property
    def is_material(self) -> bool:
        return self.relative_difference >= 0.05

    @property
    def is_implausible(self) -> bool:
        low, high = sorted((self.left_rate, self.right_rate))
        return bool(low) and high / low >= IMPLAUSIBLE_RATIO

    def describe(self) -> str:
        return (
            f"{self.code} / {self.payer}: {self.left.hospital} ${self.left_rate:,.0f} "
            f"vs {self.right.hospital} ${self.right_rate:,.0f} "
            f"({self.relative_difference:+.1%}, {self.explanation})"
        )


@dataclass
class VarianceMart:
    """The mart, plus everything excluded from it and why."""

    rows: list[Variance] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)
    provenance: Provenance = field(default_factory=Provenance)

    @property
    def material(self) -> list[Variance]:
        return [row for row in self.rows if row.is_material]

    @property
    def unexplained(self) -> list[Variance]:
        """Variances no deterministic rule accounts for -- the actual finding."""
        return [
            row
            for row in self.rows
            if row.explanation == str(Explanation.UNEXPLAINED) and row.is_material
        ]

    @property
    def comparable_share(self) -> float:
        total = len(self.rows) + sum(self.excluded.values())
        return len(self.rows) / total if total else 0.0

    def exclude(self, reason: str, count: int = 1) -> None:
        self.excluded[reason] = self.excluded.get(reason, 0) + count
        self.provenance.exclude(reason, count)

    def by_explanation(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.rows:
            counts[row.explanation] = counts.get(row.explanation, 0) + 1
        return counts

    def summary(self) -> str:
        return (
            f"{len(self.rows):,} comparable pairs "
            f"({self.comparable_share:.1%} of candidates), "
            f"{len(self.material):,} material, {len(self.unexplained):,} unexplained"
        )


def explain(left: ComparableRate, right: ComparableRate) -> tuple[str, tuple[str, ...]]:
    """Assign the deterministic explanation for a pair, before any agent sees it.

    Only explanations that can be established from the data are assigned here.
    Anything left is ``unexplained``, which is not a claim that the difference is
    real -- it is a claim that the cheap explanations have been ruled out.
    """
    notes: list[str] = []

    # Ordered by what each check can rule *out*, cheapest disqualifier first.
    # The magnitude checks come before the timing one because a difference too
    # large for drift is not explained by drift, however far apart the vintages
    # are -- the previous order let any sufficiently stale pair claim
    # ``vintage_artifact`` no matter how implausible the gap in price.
    ratio_pair = sorted((left.rate_dollar or 0.0, right.rate_dollar or 0.0))
    if ratio_pair[0] and ratio_pair[1] / ratio_pair[0] >= IMPLAUSIBLE_RATIO:
        notes.append(f"{ratio_pair[1] / ratio_pair[0]:.0f}x apart for one service and payer")
        return str(Explanation.ENTITY_RESOLUTION_SUSPECT), tuple(notes)

    if {left.product_class, right.product_class} & _AGGREGATE_PLAN_CLASSES and (
        left.product_class != right.product_class
    ):
        notes.append(f"{left.product_class} vs {right.product_class}")
        return str(Explanation.GRANULARITY_MISMATCH), tuple(notes)

    drift = _drift_could_explain(left, right)
    if drift is not None:
        span, implied = drift
        notes.append(
            f"vintages {span} days apart; the {_relative(left, right):.1%} difference "
            f"implies {implied:.1%} a year, within the {PLAUSIBLE_ANNUAL_DRIFT:.0%} bound"
        )
        return str(Explanation.VINTAGE_ARTIFACT), tuple(notes)

    if _plans_differ(left, right):
        if _across_sources(left, right):
            # A hospital publishes plan names and a payer file is one network
            # label; the two vocabularies have not been matched to each other,
            # so a string difference is not evidence that the contracts differ.
            # Calling it a granularity mismatch would assert a finding that the
            # A2 plan matcher has not yet earned.
            notes.append(f"plans not matched across sources: {left.plan!r} vs {right.plan!r}")
            return str(Explanation.PLAN_UNRESOLVED), tuple(notes)
        notes.append(f"plans differ: {left.plan!r} vs {right.plan!r}")
        return str(Explanation.GRANULARITY_MISMATCH), tuple(notes)

    return str(Explanation.UNEXPLAINED), tuple(notes)


def _relative(left: ComparableRate, right: ComparableRate) -> float:
    """Difference as a share of the lower rate, so it is symmetric in sign."""
    low, high = sorted((left.rate_dollar or 0.0, right.rate_dollar or 0.0))
    return (high - low) / low if low else 0.0


def _drift_could_explain(left: ComparableRate, right: ComparableRate) -> tuple[int, float] | None:
    """The gap and the implied annual drift, when timing is a live explanation.

    Returns ``None`` when the vintages are unknown, when there is no gap at all,
    or when the difference is too large for drift over that gap to produce --
    in which case timing has been ruled out rather than confirmed.
    """
    left_date, right_date = parse_vintage(left.vintage), parse_vintage(right.vintage)
    if left_date is None or right_date is None:
        return None
    span = abs((left_date - right_date).days)
    if span == 0:
        return None
    implied = _relative(left, right) / (span / 365.0)
    return (span, implied) if implied <= PLAUSIBLE_ANNUAL_DRIFT else None


def _across_sources(left: ComparableRate, right: ComparableRate) -> bool:
    """True when the pair spans the hospital and payer disclosures."""
    return bool(left.source and right.source and left.source != right.source)


def _plans_differ(left: ComparableRate, right: ComparableRate) -> bool:
    if not left.plan or not right.plan:
        return False
    return left.plan.strip().casefold() != right.plan.strip().casefold()


def _key(rate: ComparableRate) -> tuple[str, str, str]:
    """Join key: the service, the contracting party, and the setting.

    The setting is bucketed rather than taken literally, so a rate that applies
    in either setting lands in the wildcard bucket instead of a third one of its
    own. :func:`_setting_buckets` says which buckets a rate must be looked up in.
    """
    return (
        (rate.code or "").strip().upper(),
        (rate.payer or "").strip().casefold(),
        setting_key(rate.setting),
    )


def _setting_buckets(rate: ComparableRate) -> tuple[str, ...]:
    """The setting buckets a rate may match, widest last.

    A rate naming a specific setting can meet its own kind *or* one that is not
    setting-specific, so it has to be looked up in both. A wildcard rate is only
    ever indexed under the wildcard, and every specific rate reaches it.
    """
    bucket = setting_key(rate.setting)
    return ("",) if bucket == "" else (bucket, "")


def cross_source_variance(
    hospital_side: list[ComparableRate],
    payer_side: list[ComparableRate],
    *,
    max_vintage_days: int = 400,
) -> VarianceMart:
    """Compare the two federal disclosures of the same negotiated rate.

    Pairs are formed on service, payer and setting, within one hospital. Rows
    that cannot be compared are counted by reason rather than dropped -- the
    share that is uncomparable is part of what this project set out to measure.
    """
    mart = VarianceMart()
    mart.provenance.add_source("hospital MRF (45 CFR 180)", rows=len(hospital_side))
    mart.provenance.add_source("payer TiC", rows=len(payer_side))

    payer_index: dict[tuple[str, str, str, str], list[ComparableRate]] = {}
    for rate in payer_side:
        code, payer, setting = _key(rate)
        payer_index.setdefault((rate.hospital.casefold(), code, payer, setting), []).append(rate)

    for left in hospital_side:
        code, payer, _ = _key(left)
        candidates = [
            candidate
            for bucket in _setting_buckets(left)
            for candidate in payer_index.get((left.hospital.casefold(), code, payer, bucket), ())
        ]
        if not candidates:
            mart.exclude("no payer-side counterpart")
            continue
        for right in candidates:
            verdict = can_compare(left, right, cross_source=True, max_vintage_days=max_vintage_days)
            if not verdict:
                mart.exclude(verdict.reason)
                continue
            explanation, notes = explain(left, right)
            mart.rows.append(
                Variance(
                    code=left.code,
                    code_type=left.code_type,
                    payer=left.payer,
                    setting=left.setting,
                    left=left,
                    right=right,
                    explanation=explanation,
                    notes=notes,
                )
            )

    mart.provenance.rows = len(mart.rows)
    mart.provenance.hospitals = len({r.left.hospital for r in mart.rows})
    mart.provenance.extra_caveats.append(
        "hospital files update at least annually and payer files monthly, so a "
        "variance may be a timing artifact; the explanation column says which "
        "pairs that applies to"
    )
    return mart


def cross_hospital_variance(
    rates: list[ComparableRate],
    *,
    max_vintage_days: int = 400,
    min_hospitals: int = 2,
) -> VarianceMart:
    """Compare the same service and payer across hospitals.

    Uses only the hospital side, so it runs without a payer file. Each hospital
    contributes at most one rate per key -- its median, where a hospital
    publishes several -- so a hospital with many plan rows cannot dominate the
    spread by weight of volume alone.
    """
    mart = VarianceMart()
    mart.provenance.add_source("hospital MRF (45 CFR 180)", rows=len(rates))

    grouped: dict[tuple[str, str, str], dict[str, list[ComparableRate]]] = {}
    for rate in rates:
        if rate.rate_kind != "dollar" or not rate.rate_dollar:
            mart.exclude(str(NotComparable.NOT_DOLLAR_DENOMINATED))
            continue
        grouped.setdefault(_key(rate), {}).setdefault(rate.hospital, []).append(rate)

    for by_hospital in grouped.values():
        if len(by_hospital) < min_hospitals:
            mart.exclude("only one hospital publishes this service and payer")
            continue
        representatives = [_median_rate(group) for group in by_hospital.values()]
        representatives.sort(key=lambda r: r.rate_dollar or 0.0)

        # Compare the extremes: the spread is the negotiating fact, and pairing
        # every hospital with every other would weight large systems by n^2.
        low, high = representatives[0], representatives[-1]
        verdict = can_compare(low, high, max_vintage_days=max_vintage_days)
        if not verdict:
            mart.exclude(verdict.reason)
            continue
        explanation, notes = explain(low, high)
        mart.rows.append(
            Variance(
                code=low.code,
                code_type=low.code_type,
                payer=low.payer,
                setting=low.setting,
                left=low,
                right=high,
                explanation=explanation,
                notes=(*notes, f"{len(by_hospital)} hospitals publish this key"),
            )
        )

    mart.provenance.rows = len(mart.rows)
    mart.provenance.hospitals = len({r.hospital for r in rates})
    mart.provenance.extra_caveats.append(
        "cross-hospital spread compares the cheapest and dearest publisher of "
        "each service; it is not a hospital-versus-payer reconciliation"
    )
    return mart


def _median_rate(group: list[ComparableRate]) -> ComparableRate:
    """The median-priced row for one hospital, kept as a whole row.

    Returning a row rather than a number keeps the payer, plan and vintage
    attached, which is what the explanation rules need.
    """
    if len(group) == 1:
        return group[0]
    ordered = sorted(group, key=lambda r: r.rate_dollar or 0.0)
    return ordered[len(ordered) // 2]


def spread_by_code(mart: VarianceMart) -> list[tuple[str, float, int]]:
    """Services ranked by how far apart their rates are, widest first.

    The shortlist a negotiator starts from: the widest spreads are where the
    most money sits per unit of effort.
    """
    by_code: dict[str, list[float]] = {}
    for row in mart.rows:
        by_code.setdefault(row.code, []).append(row.relative_difference)
    return sorted(
        ((code, median(values), len(values)) for code, values in by_code.items()),
        key=lambda item: -item[1],
    )
