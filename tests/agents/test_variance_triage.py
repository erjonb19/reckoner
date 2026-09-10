"""A1 tests.

The shape being tested comes from the measured residual: Mount Sinai against
five carriers on shard 7, where 2,070 unexplained-and-material rows collapse to
718 service-and-carrier items, 92% of them one carrier, and only 28% of repeated
services hold a tight ratio spread.

Two properties matter most. Grouping must not average away plan-level variation,
because the repeats are genuinely different rates rather than duplicates -- that
was checked before the rule was written, and it is the reason spread is reported
instead of collapsed. And ranking must treat a 0.5x disagreement as the equal of
a 2x one, or every hospital-higher item sinks below every payer-higher one
regardless of size.
"""

from __future__ import annotations

from collections.abc import Sequence

from agents.variance_triage import (
    TIGHT_SPREAD,
    RuleBasedTriager,
    TriageClass,
    TriageItem,
    is_triageable,
    triage,
)
from reconcile.comparability import ComparableRate
from reconcile.variance import Explanation, Variance


def rate(source: str, dollars: float, facility: str = "Tisch", plan: str | None = None):
    return ComparableRate(
        source=source,
        hospital=facility,
        code="77371",
        code_type="HCPCS",
        payer="Anthem / Empire BCBS",
        plan=plan,
        rate_dollar=dollars,
    )


def variance(
    hospital_rate: float,
    payer_rate: float,
    *,
    code: str = "77371",
    payer: str = "Anthem / Empire BCBS",
    facility: str = "Tisch",
    plan: str | None = None,
    explanation: str = str(Explanation.UNEXPLAINED),
) -> Variance:
    left = rate("hospital", hospital_rate, facility, plan)
    right = rate("payer", payer_rate, facility, plan)
    return Variance(
        code=code,
        code_type="HCPCS",
        payer=payer,
        setting=None,
        left=left,
        right=right,
        explanation=explanation,
    )


class TestWhatEntersTheQueue:
    def test_only_unexplained_rows_are_triaged(self):
        """Everything else already has an answer; re-triaging it is noise."""
        explained = variance(100, 200, explanation=str(Explanation.VINTAGE_ARTIFACT))

        assert not is_triageable(explained)
        assert triage([explained]).items == []

    def test_an_unexplained_row_is_triaged(self):
        assert is_triageable(variance(100, 200))
        assert len(triage([variance(100, 200)]).items) == 1

    def test_rows_in_counts_the_residual_not_the_mart(self):
        """Otherwise the collapse ratio flatters itself with already-explained rows."""
        rows = [variance(100, 200)] + [
            variance(100, 200, explanation=str(Explanation.PLAN_UNRESOLVED)) for _ in range(50)
        ]

        assert triage(rows).rows_in == 1


class TestGroupingKeepsWhatItShouldKeep:
    def test_one_service_and_carrier_becomes_one_item(self):
        rows = [variance(100, 200, facility=f) for f in ("Tisch", "Queens", "Brooklyn")]

        queue = triage(rows)

        assert len(queue.items) == 1
        assert queue.items[0].observations == 3
        assert queue.rows_in == 3

    def test_different_carriers_stay_separate(self):
        rows = [variance(100, 200), variance(100, 200, payer="Cigna")]

        assert len(triage(rows).items) == 2

    def test_plan_level_variation_is_reported_not_averaged(self):
        """The repeats are not duplicates: 72% of real ones vary by more than 10%."""
        rows = [
            variance(100, 150, plan="PPO"),
            variance(100, 250, plan="HMO"),
            variance(100, 350, plan="EPO"),
        ]

        item = triage(rows).items[0]

        assert item.triage_class is TriageClass.PLAN_DEPENDENT
        assert item.spread > TIGHT_SPREAD
        assert item.median_ratio == 2.5
        assert item.plans == ("EPO", "HMO", "PPO")


class TestClassification:
    def test_plans_agreeing_is_one_contract_level_fact(self):
        rows = [variance(100, 200, plan="PPO"), variance(100, 202, plan="HMO")]

        assert triage(rows).items[0].triage_class is TriageClass.CONSISTENT_ACROSS_PLANS

    def test_one_observation_is_labelled_as_weak_evidence(self):
        item = triage([variance(100, 900)]).items[0]

        assert item.triage_class is TriageClass.SINGLE_OBSERVATION
        assert item.spread == 0.0

    def test_direction_is_reported(self):
        assert triage([variance(100, 200)]).items[0].direction == "payer higher"
        assert triage([variance(200, 100)]).items[0].direction == "hospital higher"
        assert (
            triage([variance(100, 200, plan="a"), variance(200, 100, plan="b")]).items[0].direction
            == "mixed"
        )


class TestRanking:
    def test_a_halving_ranks_with_a_doubling(self):
        """Otherwise every hospital-higher item sinks below every payer-higher one."""
        doubled = triage([variance(100, 200)]).items[0]
        halved = triage([variance(200, 100)]).items[0]

        assert doubled.magnitude == halved.magnitude == 2.0

    def test_evidence_breaks_a_tie_but_cannot_buy_the_lead(self):
        """The fan-out already inflates repetition; it must not also buy priority."""
        rows = [variance(100, 900, code="BIG")] + [
            variance(100, 120, code="SMALL", plan=f"p{i}") for i in range(9)
        ]

        items = triage(rows).items

        assert items[0].code == "BIG", "a 9x lead outranks a 1.2x pattern"

    def test_between_equal_magnitudes_more_evidence_wins(self):
        rows = [
            variance(100, 200, code="ONCE"),
            *[variance(100, 200, code="OFTEN", plan=f"p{i}") for i in range(4)],
        ]

        assert triage(rows).items[0].code == "OFTEN"


class TestTheQueueDescribesItself:
    def test_carrier_concentration_is_reported(self):
        """92% in one carrier is one relationship, not hundreds of findings."""
        rows = [variance(100, 200, code=f"C{i}") for i in range(9)]
        rows.append(variance(100, 200, code="X", payer="Cigna"))

        queue = triage(rows)

        assert queue.concentration == 0.9
        assert queue.summary()["largest_carrier_share"] == 0.9

    def test_an_empty_queue_does_not_divide_by_zero(self):
        queue = triage([])

        assert queue.items == []
        assert queue.concentration == 0.0
        assert queue.summary()["collapsed_by"] == 0

    def test_a_custom_triager_can_replace_the_rules(self):
        """The seam an LLM triager would use."""

        class Nothing:
            def triage(self, variances: Sequence[Variance]) -> list[TriageItem]:
                return []

        assert triage([variance(100, 200)], triager=Nothing()).items == []
        assert RuleBasedTriager().name == "rules"
