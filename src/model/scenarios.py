"""Scenario modelling over a case mix.

A payment calculator prices one claim. Rate modelling asks the questions that
follow: what does this book of business pay under the current schedule, what
happens if the base rate moves 3%, what if the case mix shifts toward higher
severity, and which hospitals are most exposed.

Case Mix Index is reported alongside every result because it is the number that
explains the others -- two hospitals with identical rates and different CMI are
not comparable, and a payment change that tracks a CMI change is not a rate
change.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from model.loaders import RateTable, WeightTable
from model.nys_medicaid import Basis, Claim, Payment, RateSchedule, calculate


@dataclass(frozen=True)
class CaseMixEntry:
    apr_drg: str
    severity: int
    cases: int
    average_days: float = 1.0
    alc_days: float = 0.0
    transfer_share: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.apr_drg}-{self.severity}"


@dataclass
class CaseMix:
    """A book of inpatient business: volume by DRG and severity."""

    name: str
    entries: list[CaseMixEntry] = field(default_factory=list)

    @property
    def total_cases(self) -> int:
        return sum(entry.cases for entry in self.entries)

    def shift_severity(self, points: float) -> CaseMix:
        """Move a share of volume up one severity level.

        Severity drift is the most common reason modelled revenue misses, and it
        is not a rate change -- separating the two is the point of the exercise.
        """
        # Counts are accumulated separately from the entry templates. Writing
        # entries directly loses volume: a later entry overwrites cases that an
        # earlier one already shifted up into its key.
        templates: dict[str, CaseMixEntry] = {entry.key: entry for entry in self.entries}
        counts: dict[str, int] = {}

        for entry in self.entries:
            # Severity 4 is the ceiling, so nothing moves out of it.
            move = 0 if entry.severity >= 4 else entry.cases - round(entry.cases * (1 - points))
            counts[entry.key] = counts.get(entry.key, 0) + entry.cases - move
            if move:
                up_key = f"{entry.apr_drg}-{entry.severity + 1}"
                counts[up_key] = counts.get(up_key, 0) + move
                templates.setdefault(up_key, replace(entry, severity=entry.severity + 1, cases=0))

        entries = [replace(templates[key], cases=count) for key, count in counts.items() if count]
        return CaseMix(f"{self.name} (+{points:.0%} severity)", entries)


@dataclass(frozen=True)
class Scenario:
    """An adjustment to a hospital's published rate schedule."""

    name: str
    rate_multiplier: float = 1.0
    capital_multiplier: float = 1.0
    isaf_override: float | None = None

    def apply(self, rate: RateSchedule) -> RateSchedule:
        return replace(
            rate,
            discharge_rate=rate.discharge_rate * self.rate_multiplier,
            capital_per_discharge=rate.capital_per_discharge * self.capital_multiplier,
            capital_per_diem=rate.capital_per_diem * self.capital_multiplier,
            isaf=self.isaf_override if self.isaf_override is not None else rate.isaf,
        )


@dataclass
class ModelResult:
    scenario: str
    hospital: str
    cases: int
    total_payment: float
    case_mix_index: float
    unpriced_cases: int = 0
    by_drg: dict[str, float] = field(default_factory=dict)

    @property
    def payment_per_case(self) -> float:
        return self.total_payment / self.cases if self.cases else 0.0


def model_case_mix(
    case_mix: CaseMix,
    rate: RateSchedule,
    weights: WeightTable,
    basis: Basis = Basis.MMC,
    scenario: Scenario | None = None,
) -> ModelResult:
    """Expected reimbursement for a book of business at one hospital.

    Cases whose DRG is absent from the weight table are counted, not silently
    dropped -- an unpriced share is a caveat on the total, not a rounding error.
    """
    applied = scenario.apply(rate) if scenario else rate
    total = 0.0
    weighted_siw = 0.0
    priced = 0
    unpriced = 0
    by_drg: dict[str, float] = {}

    for entry in case_mix.entries:
        weight = weights.get(entry.apr_drg, entry.severity)
        if weight is None:
            unpriced += entry.cases
            continue

        transfers = round(entry.cases * entry.transfer_share)
        for count, is_transfer in ((entry.cases - transfers, False), (transfers, True)):
            if count <= 0:
                continue
            claim = Claim(
                apr_drg=entry.apr_drg,
                severity=entry.severity,
                total_days=max(round(entry.average_days), 1),
                alc_days=round(entry.alc_days),
                is_transfer=is_transfer,
            )
            payment: Payment = calculate(claim, applied, weight, basis)
            total += payment.total * count
            by_drg[entry.key] = by_drg.get(entry.key, 0.0) + payment.total * count

        weighted_siw += weight.siw * entry.cases
        priced += entry.cases

    return ModelResult(
        scenario=scenario.name if scenario else "current",
        hospital=applied.hospital,
        cases=priced,
        total_payment=total,
        case_mix_index=weighted_siw / priced if priced else 0.0,
        unpriced_cases=unpriced,
        by_drg=by_drg,
    )


@dataclass
class Comparison:
    baseline: ModelResult
    variant: ModelResult

    @property
    def delta(self) -> float:
        return self.variant.total_payment - self.baseline.total_payment

    @property
    def delta_pct(self) -> float:
        base = self.baseline.total_payment
        return self.delta / base if base else 0.0

    @property
    def cmi_change(self) -> float:
        return self.variant.case_mix_index - self.baseline.case_mix_index

    @property
    def driver(self) -> str:
        """Whether the change came from rates or from case mix.

        A payment move that tracks a CMI move is a case-mix effect and must not
        be reported as a rate win.
        """
        if abs(self.cmi_change) > 1e-9:
            return "case mix"
        if abs(self.delta) > 1e-9:
            return "rate"
        return "none"


def compare(baseline: ModelResult, variant: ModelResult) -> Comparison:
    return Comparison(baseline, variant)


def model_across_hospitals(
    case_mix: CaseMix,
    rates: RateTable,
    weights: WeightTable,
    licences: list[str],
    basis: Basis = Basis.MMC,
    scenario: Scenario | None = None,
) -> list[ModelResult]:
    """The same book of business priced at each hospital.

    Holding case mix constant isolates the rate and geography effect, which is
    what makes the spread between hospitals interpretable.
    """
    results = []
    for licence in licences:
        rate = rates.get(licence)
        if rate is None:
            continue
        results.append(model_case_mix(case_mix, rate, weights, basis, scenario))
    return sorted(results, key=lambda r: -r.total_payment)
