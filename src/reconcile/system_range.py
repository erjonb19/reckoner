"""Compare a payer's system-level rate against the range its hospitals publish.

The two disclosures do not name a provider at the same grain, and pretending
otherwise is what made the first cross-source mart useless. A Transparency in
Coverage file resolves no finer than the health system, because the
provider-group boundary is dissolved into matched tax IDs upstream. A hospital
file names a facility. Attributing the payer's single number to each facility in
turn and differencing produced one manufactured variance per facility: 1,458
pairs at a constant 1.298x and 1,458 more at 1.410x, which were Queens and
Brooklyn measured against the same system-wide figure.

The honest comparison is therefore not "does the payer's number equal this
hospital's" but **"does the payer's number fall inside what this system's
hospitals publish"**. One comparison per service and payer, at the grain the
payer data actually supports, and no invented per-facility difference.

Two things to keep in view when reading a verdict:

* ``INSIDE`` is a weaker claim than agreement. A system whose hospitals span
  $8,000 to $30,000 for one service is easy to land inside, so
  :attr:`RangeComparison.width` is reported alongside the verdict and a narrow
  range makes ``INSIDE`` mean much more.
* Matching is on the **carrier**, not the plan. A hospital that publishes one
  blended ``Aetna Hmo/Pos/Ppo`` rate cannot be attributed to a single network,
  but it is still an Aetna rate and the carrier's networks are still the right
  comparator. That is what lets Aetna and UnitedHealthcare be compared at all;
  plan-level matching reached only Cigna.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from statistics import median

#: Beyond this multiple the two sides are not describing the same thing. Matches
#: the bound the pairwise mart already used; the range comparison shipped without
#: one, so it reported 193x gaps as findings.
IMPLAUSIBLE_RATIO = 10.0


class RangeVerdict(StrEnum):
    """Where the payer's published rate sits against the hospitals' own."""

    #: Within what the system's hospitals publish. The two disclosures are
    #: consistent, to the precision the range allows.
    INSIDE = "inside"
    #: Below every hospital in the system.
    BELOW = "below"
    #: Above every hospital in the system.
    ABOVE = "above"
    #: So far apart that it is not a price difference. A hospital publishing $270
    #: against a payer's $52,078 for one service is a unit or coding mismatch,
    #: and counting it as a disagreement puts nonsense in the denominator that
    #: every share is measured against.
    IMPLAUSIBLE = "implausible"


@dataclass(frozen=True)
class RangeComparison:
    """One service, one carrier: what the hospitals published against the payer."""

    code: str
    code_type: str | None
    payer: str
    description: str = ""
    #: Facility name -> that facility's representative rate.
    facility_rates: dict[str, float] = field(default_factory=dict)
    payer_rate: float = 0.0
    #: How many payer rate rows backed ``payer_rate``, so a single-row median is
    #: not mistaken for a well-supported one.
    payer_rows: int = 1
    #: The bound past which a difference stops being a price difference.
    implausible_ratio: float = IMPLAUSIBLE_RATIO

    @property
    def low(self) -> float:
        return min(self.facility_rates.values()) if self.facility_rates else 0.0

    @property
    def high(self) -> float:
        return max(self.facility_rates.values()) if self.facility_rates else 0.0

    @property
    def hospital_median(self) -> float:
        return median(self.facility_rates.values()) if self.facility_rates else 0.0

    @property
    def width(self) -> float:
        """How far apart the hospitals are. A wide range makes INSIDE cheap."""
        return self.high / self.low if self.low else 0.0

    @property
    def gap(self) -> float:
        """How far outside the range the payer sits, as a multiple. 1.0 inside.

        Computed before the verdict rather than from it, because the verdict now
        depends on the size of the gap.
        """
        if self.payer_rate < self.low and self.payer_rate:
            return self.low / self.payer_rate
        if self.payer_rate > self.high and self.high:
            return self.payer_rate / self.high
        return 1.0

    @property
    def verdict(self) -> RangeVerdict:
        if self.gap >= self.implausible_ratio:
            return RangeVerdict.IMPLAUSIBLE
        if self.payer_rate < self.low:
            return RangeVerdict.BELOW
        if self.payer_rate > self.high:
            return RangeVerdict.ABOVE
        return RangeVerdict.INSIDE

    @property
    def vs_median(self) -> float:
        """The payer's rate as a multiple of the hospitals' median.

        Reported because it does not depend on the range's width, so it stays
        meaningful where ``verdict`` is flattered by a wide spread.
        """
        centre = self.hospital_median
        return self.payer_rate / centre if centre else 0.0

    def describe(self) -> str:
        return (
            f"{self.code} / {self.payer}: hospitals ${self.low:,.0f}-${self.high:,.0f}, "
            f"payer ${self.payer_rate:,.0f} ({self.verdict})"
        )


def compare_to_system_range(
    hospital: dict[tuple[str, str, str], dict[str, float]],
    payer: dict[tuple[str, str, str], list[float]],
    descriptions: dict[tuple[str, str], str] | None = None,
    *,
    min_facilities: int = 2,
) -> list[RangeComparison]:
    """Build one comparison per service and carrier the two sides share.

    Both sides are keyed ``(code, code_type, carrier)``. The hospital side maps
    facility to rate; the payer side is the rates its files carry, from which
    the median is taken -- a payer publishes many rows per service and one
    outlier should not decide the verdict.

    ``min_facilities`` guards the range itself: a "range" from one hospital is a
    point, and calling a payer rate inside or outside it claims more than one
    observation can support.
    """
    descriptions = descriptions or {}
    out: list[RangeComparison] = []
    for key, rates in hospital.items():
        theirs = payer.get(key)
        if not theirs or len(rates) < min_facilities:
            continue
        code, code_type, carrier = key
        out.append(
            RangeComparison(
                code=code,
                code_type=code_type,
                payer=carrier,
                description=descriptions.get((code, code_type), ""),
                facility_rates=dict(rates),
                payer_rate=median(theirs),
                payer_rows=len(theirs),
            )
        )
    return sorted(out, key=lambda c: (-c.gap, -c.payer_rate))


def summarise(comparisons: list[RangeComparison]) -> dict[str, float | int]:
    """Headline counts: how often the two disclosures actually agree."""
    total = len(comparisons)
    if not total:
        return {"total": 0}
    counts = {v: 0 for v in RangeVerdict}
    for row in comparisons:
        counts[row.verdict] += 1
    # Implausible pairs are not a disagreement about price, so they are counted
    # and then kept out of every share. Leaving them in measured agreement
    # against a denominator holding unit mismatches.
    comparable = [c for c in comparisons if c.verdict is not RangeVerdict.IMPLAUSIBLE]
    outside = [c.gap for c in comparable if c.verdict is not RangeVerdict.INSIDE]
    denominator = len(comparable) or 1
    return {
        "total": total,
        "comparable": len(comparable),
        "implausible_excluded": counts[RangeVerdict.IMPLAUSIBLE],
        "inside": counts[RangeVerdict.INSIDE],
        "below": counts[RangeVerdict.BELOW],
        "above": counts[RangeVerdict.ABOVE],
        "inside_share": counts[RangeVerdict.INSIDE] / denominator,
        "median_gap_when_outside": median(outside) if outside else 0.0,
        "median_range_width": median(c.width for c in comparable) if comparable else 0.0,
    }


__all__ = [
    "IMPLAUSIBLE_RATIO",
    "RangeComparison",
    "RangeVerdict",
    "compare_to_system_range",
    "summarise",
]
