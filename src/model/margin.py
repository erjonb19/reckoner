"""Margin: what a rate is worth after the cost of delivering the service.

A rate is not a result. "Medicaid pays $8,485 for heart failure" says nothing
until you know it costs $9,200 to deliver -- at which point the conversation
changes from pricing to whether the service line survives. Percent-of-Medicare
and percent-of-Medicaid are useful comparators; margin is the number that gets
a decision.

Costs come from SPARCS `mean_cost`, which is a facility-and-DRG average, not a
per-case actual. That makes this a screening tool: it will tell you which
service lines are underwater and roughly by how much, not what any individual
case cost. Every result carries its provenance so the distinction survives
contact with a slide.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from model.loaders import WeightTable
from model.nys_medicaid import Basis, Claim, RateSchedule, calculate
from model.scenarios import CaseMix
from reconcile.provenance import Provenance


@dataclass
class MarginLine:
    """Payment against cost for one APR-DRG severity."""

    key: str
    cases: int
    payment: float
    cost: float
    payment_type: str = ""

    @property
    def margin(self) -> float:
        return self.payment - self.cost

    @property
    def margin_pct(self) -> float:
        return self.margin / self.payment if self.payment else 0.0

    @property
    def payment_per_case(self) -> float:
        return self.payment / self.cases if self.cases else 0.0

    @property
    def cost_per_case(self) -> float:
        return self.cost / self.cases if self.cases else 0.0

    @property
    def is_underwater(self) -> bool:
        return self.margin < 0


@dataclass
class MarginResult:
    hospital: str
    basis: str
    lines: list[MarginLine] = field(default_factory=list)
    unpriced_cases: int = 0
    uncosted_cases: int = 0
    #: Share of the facility's discharges this rate schedule actually governs.
    payer_share: float = 1.0
    provenance: Provenance = field(default_factory=Provenance)

    @property
    def cases(self) -> int:
        return sum(line.cases for line in self.lines)

    @property
    def total_payment(self) -> float:
        return sum(line.payment for line in self.lines)

    @property
    def total_cost(self) -> float:
        return sum(line.cost for line in self.lines)

    @property
    def margin(self) -> float:
        return self.total_payment - self.total_cost

    @property
    def margin_pct(self) -> float:
        return self.margin / self.total_payment if self.total_payment else 0.0

    @property
    def underwater_lines(self) -> list[MarginLine]:
        """Service lines paying less than they cost, worst first."""
        return sorted((x for x in self.lines if x.is_underwater), key=lambda x: x.margin)

    @property
    def underwater_share(self) -> float:
        cases = sum(line.cases for line in self.underwater_lines)
        return cases / self.cases if self.cases else 0.0


def margin_for_case_mix(
    case_mix: CaseMix,
    rate: RateSchedule,
    weights: WeightTable,
    costs: dict[str, float],
    basis: Basis = Basis.MMC,
    provenance: Provenance | None = None,
    charges: dict[str, float] | None = None,
    payer_share: float = 1.0,
    include_teaching_addons: bool = True,
) -> MarginResult:
    """Model payment against cost across a book of business.

    Cases with no weight cannot be paid; cases with no cost cannot be judged.
    Both are counted separately rather than folded into the totals, because a
    margin computed over an unknown denominator is worse than no margin.

    ``payer_share`` scales an all-payer discharge volume down to the portion this
    rate schedule actually governs. Left at 1.0 it answers "what if every patient
    were on this payer" -- a scenario, not an observed margin. Maimonides is 52%
    Medicaid, so the unweighted figure overstates Medicaid exposure about 2x.

    ``charges`` feeds the high-cost outlier test. Without it no outlier can fire,
    and payment is understated on exactly the expensive cases outliers cover.
    """
    if not 0 < payer_share <= 1:
        raise ValueError(f"payer_share must be in (0, 1]: {payer_share}")
    charges = charges or {}
    result = MarginResult(
        hospital=rate.hospital,
        basis=str(basis),
        provenance=provenance or Provenance(hospitals=1),
    )

    for entry in case_mix.entries:
        weight = weights.get(entry.apr_drg, entry.severity)
        if weight is None:
            result.unpriced_cases += entry.cases
            result.provenance.exclude("no APR-DRG weight", entry.cases)
            continue
        cost_per_case = costs.get(entry.key)
        if not cost_per_case:
            result.uncosted_cases += entry.cases
            result.provenance.exclude("no SPARCS cost for this DRG", entry.cases)
            continue

        cases = round(entry.cases * payer_share)
        if cases <= 0:
            continue
        claim = Claim(
            apr_drg=entry.apr_drg,
            severity=entry.severity,
            total_days=max(round(entry.average_days), 1),
            gross_charges=charges.get(entry.key, 0.0),
        )
        payment = calculate(claim, rate, weight, basis, include_teaching_addons)
        result.lines.append(
            MarginLine(
                key=entry.key,
                cases=cases,
                payment=payment.total * cases,
                cost=cost_per_case * cases,
                payment_type=str(payment.payment_type),
            )
        )

    result.payer_share = payer_share
    result.provenance.rows = result.cases
    if payer_share < 1.0:
        result.provenance.extra_caveats.append(
            f"scaled to the {payer_share:.0%} of discharges this payer covers"
        )
    else:
        result.provenance.extra_caveats.append(
            "EVERY discharge priced at this payer's rates; a scenario, not observed margin"
        )
    if not include_teaching_addons:
        result.provenance.extra_caveats.append(
            "IME and DME excluded; understates teaching hospitals materially"
        )
    if not charges:
        result.provenance.extra_caveats.append(
            "no charges supplied, so no high-cost outlier payments were modelled; "
            "payment is understated on expensive cases"
        )
    result.provenance.extra_caveats.append(
        "cost is a SPARCS facility-and-DRG average, not per-case actuals; "
        "this screens service lines rather than costing individual cases"
    )
    result.provenance.extra_caveats.append(
        "claim-based payment only: DSH, directed and supplemental payments are "
        "excluded, so this is a floor. Those pools are largest at safety-net and "
        "academic hospitals, which is where the floor sits furthest below actual."
    )
    return result
