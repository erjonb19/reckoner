import pytest

from reconcile.market import (
    MIN_PEERS_FOR_PERCENTILE,
    RateObservation,
    percentile_rank,
    rank_within_peers,
    spreads,
    weighted_opportunity,
)


def obs(hospital: str, rate: float, code: str = "27447") -> RateObservation:
    return RateObservation(hospital=hospital, code=code, rate=rate, payer="Aetna")


KNEE = [
    obs("Maimonides", 28000),
    obs("NYU Langone", 25000),
    obs("Mount Sinai", 22000),
    obs("Rochester Regional", 16000),
    obs("Ellis", 14000),
    obs("Unity", 12000),
]


class TestPercentileRank:
    def test_highest_ranks_top(self):
        assert percentile_rank(28000, [r.rate for r in KNEE]) == 1.0

    def test_lowest_ranks_bottom(self):
        assert percentile_rank(12000, [r.rate for r in KNEE]) == pytest.approx(1 / 6)

    def test_empty_population(self):
        assert percentile_rank(100, []) == 0.0


class TestRanking:
    def test_positions_carry_peer_context(self):
        positions = rank_within_peers(KNEE, "NY acute care")
        top = positions[0]

        assert top.observation.hospital == "Maimonides"
        assert top.percentile == 1.0
        assert top.peer_count == 6
        assert top.peer_median == pytest.approx(19000)
        assert top.ratio_to_median > 1.4
        assert top.is_reliable

    def test_thin_peer_groups_are_marked_unreliable(self):
        positions = rank_within_peers(KNEE[:3])

        assert all(not p.is_reliable for p in positions)
        assert any("below the" in c for c in positions[0].provenance.caveats)
        assert "too few to rank" in positions[0].describe()

    def test_codes_are_ranked_separately(self):
        mixed = [*KNEE, obs("Maimonides", 90, code="80053"), obs("Unity", 70, code="80053")]

        positions = rank_within_peers(mixed)
        lab = [p for p in positions if p.observation.code == "80053"]

        assert len(lab) == 2
        assert lab[0].peer_count == 2, "a lab test is not ranked against a knee replacement"

    def test_describe_is_readable(self):
        top = rank_within_peers(KNEE, "NY acute care")[0]

        assert "100%ile" in top.describe()
        assert "NY acute care" in top.describe()


class TestSpreads:
    def test_widest_spread_first(self):
        mixed = [*KNEE, obs("A", 100, code="80053"), obs("B", 110, code="80053")]

        result = spreads(mixed)

        assert result[0].code == "27447", "2.33x beats 1.1x"
        assert result[0].ratio == pytest.approx(28000 / 12000)
        assert result[0].high_hospital == "Maimonides"
        assert result[0].low_hospital == "Unity"

    def test_singletons_are_skipped(self):
        assert spreads([obs("Solo", 100)]) == []


class TestWeightedOpportunity:
    def test_gap_to_median_times_volume(self):
        positions = rank_within_peers(KNEE)
        unity = next(p for p in positions if p.observation.hospital == "Unity")

        opportunity = weighted_opportunity(unity, annual_cases=400)

        assert opportunity == pytest.approx((19000 - 12000) * 400)

    def test_above_target_has_no_opportunity(self):
        positions = rank_within_peers(KNEE)
        top = next(p for p in positions if p.observation.hospital == "Maimonides")

        assert weighted_opportunity(top, annual_cases=400) == 0.0

    def test_unreliable_peer_group_yields_nothing(self):
        thin = rank_within_peers(KNEE[:2])

        assert weighted_opportunity(thin[-1], annual_cases=1000) == 0.0
        assert MIN_PEERS_FOR_PERCENTILE > 2
