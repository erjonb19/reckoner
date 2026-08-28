"""Market position: where a rate sits against its peers.

"Maimonides is paid $8,982 per case" is a fact without a use. "Maimonides is at
the 85th percentile of NY commercial rates for this DRG, against a market median
of $7,100" is a negotiating position -- for the hospital seeking an increase, or
for the payer resisting one.

Percentiles are computed within a peer group, because a rate is only high or low
relative to something. The peer group is stated on every result: comparing an
academic medical centre in Manhattan to a community hospital upstate produces a
number that is arithmetically correct and useless.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from reconcile.provenance import Provenance

#: Below this many peers a percentile is arithmetic, not a market read.
MIN_PEERS_FOR_PERCENTILE = 5


def percentile_rank(value: float, population: list[float]) -> float:
    """Share of the population at or below ``value``.

    Uses the "less than or equal" convention, so the highest rate in a group
    ranks 1.0 rather than something just under it.
    """
    if not population:
        return 0.0
    at_or_below = sum(1 for other in population if other <= value)
    return at_or_below / len(population)


@dataclass(frozen=True)
class RateObservation:
    """One hospital's rate for one service, from one payer."""

    hospital: str
    code: str
    rate: float
    payer: str = ""
    plan: str = ""
    setting: str = ""
    product_class: str = ""
    vintage: str = ""


@dataclass
class MarketPosition:
    """Where one observation sits in its peer group."""

    observation: RateObservation
    percentile: float
    peer_count: int
    peer_median: float
    peer_min: float
    peer_max: float
    peer_group: str
    provenance: Provenance = field(default_factory=Provenance)

    @property
    def ratio_to_median(self) -> float:
        return self.observation.rate / self.peer_median if self.peer_median else 0.0

    @property
    def is_reliable(self) -> bool:
        return self.peer_count >= MIN_PEERS_FOR_PERCENTILE

    def describe(self) -> str:
        if not self.is_reliable:
            return (
                f"{self.observation.hospital}: ${self.observation.rate:,.0f} "
                f"({self.peer_count} peers -- too few to rank)"
            )
        return (
            f"{self.observation.hospital}: ${self.observation.rate:,.0f} "
            f"= {self.percentile:.0%}ile of {self.peer_group} "
            f"({self.ratio_to_median:.2f}x median of ${self.peer_median:,.0f}, "
            f"n={self.peer_count})"
        )


def rank_within_peers(
    observations: list[RateObservation],
    peer_group: str = "all peers",
    provenance: Provenance | None = None,
) -> list[MarketPosition]:
    """Rank every observation against the others for the same code.

    Observations are grouped by code first: ranking a knee replacement against
    a blood panel would be arithmetic dressed as insight.
    """
    by_code: dict[str, list[RateObservation]] = {}
    for observation in observations:
        by_code.setdefault(observation.code, []).append(observation)

    positions = []
    for code, group in by_code.items():
        rates = [o.rate for o in group]
        low, high, mid = min(rates), max(rates), median(rates)
        shared = Provenance(
            rows=len(group),
            hospitals=len({o.hospital for o in group}),
            excluded=dict(provenance.excluded) if provenance else {},
            sources=list(provenance.sources) if provenance else [],
        )
        if len(group) < MIN_PEERS_FOR_PERCENTILE:
            shared.extra_caveats.append(
                f"{code}: {len(group)} peers, below the {MIN_PEERS_FOR_PERCENTILE} "
                "needed for a meaningful percentile"
            )
        for observation in group:
            positions.append(
                MarketPosition(
                    observation=observation,
                    percentile=percentile_rank(observation.rate, rates),
                    peer_count=len(group),
                    peer_median=mid,
                    peer_min=low,
                    peer_max=high,
                    peer_group=peer_group,
                    provenance=shared,
                )
            )
    return sorted(positions, key=lambda p: (p.observation.code, -p.percentile))


@dataclass
class Spread:
    """The gap between the cheapest and dearest rate for one service."""

    code: str
    peer_count: int
    low: float
    high: float
    median_rate: float
    low_hospital: str = ""
    high_hospital: str = ""

    @property
    def ratio(self) -> float:
        return self.high / self.low if self.low else 0.0

    @property
    def spread(self) -> float:
        return self.high - self.low


def spreads(observations: list[RateObservation], min_peers: int = 2) -> list[Spread]:
    """Rank services by how much rates vary across hospitals.

    This is the shortlist a negotiator starts from: the widest spreads are where
    the most money sits per unit of effort.
    """
    by_code: dict[str, list[RateObservation]] = {}
    for observation in observations:
        by_code.setdefault(observation.code, []).append(observation)

    results = []
    for code, group in by_code.items():
        if len(group) < min_peers:
            continue
        cheapest = min(group, key=lambda o: o.rate)
        dearest = max(group, key=lambda o: o.rate)
        results.append(
            Spread(
                code=code,
                peer_count=len(group),
                low=cheapest.rate,
                high=dearest.rate,
                median_rate=median([o.rate for o in group]),
                low_hospital=cheapest.hospital,
                high_hospital=dearest.hospital,
            )
        )
    return sorted(results, key=lambda s: -s.ratio)


def weighted_opportunity(
    position: MarketPosition, annual_cases: int, target_percentile: float = 0.5
) -> float:
    """Dollars per year between a rate and its peer-group target.

    This is the step that makes a percentile actionable: a hospital priced at the
    20th percentile on a service it performs 4,000 times a year is a different
    conversation from one it performs twice.
    """
    if not position.is_reliable or position.percentile >= target_percentile:
        return 0.0
    gap = position.peer_median - position.observation.rate
    return max(gap, 0.0) * annual_cases
