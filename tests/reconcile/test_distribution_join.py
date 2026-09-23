"""The published join: billing class in the key (ADR 0005), one outcome per
hospital rate against the carrier's distribution (ADR 0006).

The fixtures here are built around what the change is *for*: one hospital rate
meeting many plan-level payer rates of one carrier, both billing classes at
once, and every refusal step. A fixture with one payer rate per key cannot tell
the two grains apart, which is why the older gold tests still pass unchanged.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from reconcile.comparability import ComparableRate
from reconcile.gold import Reconciliation, stream_shard
from reconcile.variance import (
    NO_COUNTERPART,
    Explanation,
    PayerSpread,
    VarianceMart,
    iter_distribution_variance,
)

FACILITY = "NYU Langone|Tisch Hospital"


def hospital(code: str = "70450", rate: float = 500.0, **kw: object) -> ComparableRate:
    base = ComparableRate(
        source="hospital",
        hospital=FACILITY,
        code=code,
        code_type="CPT",
        payer="UnitedHealthcare",
        plan="United Healthcare EPO",
        product_class="commercial",
        billing_class="facility",
        setting="outpatient",
        rate_dollar=rate,
        vintage="2026-04-01",
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def payer(rate: float, plan: str = "ChoicePlus", **kw: object) -> ComparableRate:
    base = ComparableRate(
        source="payer",
        hospital=FACILITY,
        code="70450",
        code_type="CPT",
        payer="UnitedHealthcare",
        plan=plan,
        product_class="commercial",
        billing_class="facility",
        setting="outpatient",
        rate_dollar=rate,
        vintage="2026-09-01",
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def run(left: list[ComparableRate], right: list[ComparableRate], **kw: object):
    mart = VarianceMart()
    rows = list(iter_distribution_variance(left, right, mart=mart, **kw))  # type: ignore[arg-type]
    return rows, mart


class TestOneOutcomePerHospitalRate:
    def test_many_plan_rates_become_one_comparison_not_one_each(self):
        """The fan-out #70 was about: 290 plans, one disagreement counted 290 times."""
        plans = [payer(400.0 + i, plan=f"Plan{i}") for i in range(290)]

        rows, mart = run([hospital()], plans)

        assert len(rows) == 1
        assert sum(mart.excluded.values()) == 0
        assert rows[0].spread is not None
        assert rows[0].spread.count == 290

    def test_the_row_compares_against_the_median(self):
        rows, _ = run([hospital(rate=1_000.0)], [payer(100.0), payer(200.0), payer(300.0)])

        assert rows[0].right_rate == 200.0
        assert rows[0].ratio == pytest.approx(0.2)

    def test_every_hospital_rate_is_exactly_one_outcome(self):
        left = [
            hospital("70450"),
            hospital("99999"),  # no counterpart
            hospital("70450", product_class="medicare_advantage"),  # exempt
        ]

        rows, mart = run(left, [payer(500.0)])

        assert len(rows) + sum(mart.excluded.values()) == len(left)


class TestWithinThePayersRange:
    def test_a_rate_inside_the_carriers_spread_is_explained_as_such(self):
        rows, _ = run(
            [hospital(rate=520.0)],
            [payer(300.0, plan="SelectEPO"), payer(400.0), payer(900.0, plan="NationalPPO")],
        )

        assert rows[0].explanation == Explanation.WITHIN_PAYER_RANGE
        assert "3 networks" in rows[0].notes[-1]

    def test_outside_the_range_it_is_not(self):
        rows, _ = run([hospital(rate=5_000.0)], [payer(300.0), payer(400.0, plan="SelectEPO")])

        assert rows[0].explanation != Explanation.WITHIN_PAYER_RANGE

    def test_a_single_payer_rate_is_no_range_to_be_inside(self):
        rows, _ = run([hospital(rate=500.0)], [payer(500.0)])

        assert rows[0].explanation != Explanation.WITHIN_PAYER_RANGE


class TestBillingClassIsInTheKey:
    def test_other_class_rates_are_never_compared(self):
        """A professional fee is not the facility's price, whatever it is."""
        rows, _ = run(
            [hospital(rate=500.0)],
            [payer(510.0), payer(90.0, billing_class="professional", plan="Prof")],
        )

        assert rows[0].spread is not None
        assert rows[0].spread.count == 1
        assert rows[0].spread.minimum == 510.0

    def test_counterparts_only_in_the_other_class_are_that_refusal(self):
        rows, mart = run([hospital()], [payer(90.0, billing_class="professional")])

        assert rows == []
        assert mart.excluded == {"different_billing_class": 1}

    def test_an_unstated_class_joins_as_facility_only_where_that_is_safe(self):
        unstated = hospital(billing_class=None)

        refused, refused_mart = run([unstated], [payer(500.0)])
        assumed, _ = run(
            [unstated], [payer(500.0)], assume_facility_when_unstated=frozenset({FACILITY})
        )

        assert refused == [] and refused_mart.excluded == {"billing_class_unstated": 1}
        assert len(assumed) == 1
        assert any("assumed facility" in note for note in assumed[0].notes)


class TestTheRefusalOrder:
    def test_a_tic_exempt_rate_is_refused_by_rule_before_anything(self):
        """Even with no counterpart at all: the rule is the reason, not the absence."""
        _, mart = run([hospital("99999", product_class="medicare_advantage")], [payer(1.0)])

        assert mart.excluded == {"tic_exempt_product": 1}

    def test_no_counterpart_in_any_class(self):
        _, mart = run([hospital("99999")], [payer(500.0)])

        assert mart.excluded == {NO_COUNTERPART: 1}

    def test_like_class_rates_all_refused_take_the_majority_reason(self):
        right = [payer(0.0, plan="A"), payer(0.0, plan="B"), payer(500.0, rate_kind="percentage")]

        _, mart = run([hospital()], right)

        assert mart.excluded == {"zero_rate": 1}

    def test_one_comparable_rate_is_enough(self):
        rows, mart = run([hospital()], [payer(0.0), payer(480.0)])

        assert len(rows) == 1
        assert rows[0].spread is not None and rows[0].spread.count == 1
        assert mart.excluded == {}


class TestTheShares:
    def test_raw_and_like_class_are_reported_side_by_side(self):
        left = [hospital("70450"), hospital("70451"), hospital("70452")]
        right = [
            payer(500.0),
            payer(90.0, code="70451", billing_class="professional"),
            payer(0.0, code="70452"),
        ]
        recon = Reconciliation(hospital="NYU", system="NYU Langone", hospital_slug="nyu")
        mart, rows = stream_shard(left, right)
        recon.add_shard("7", mart, rows=rows)
        recon.close()

        row = recon.coverage_row()

        assert (row["candidates"], row["pairs_formed"]) == (3, 1)
        assert row["comparable_share"] == pytest.approx(1 / 3)
        assert row["like_class_candidates"] == 2
        assert row["like_class_share"] == pytest.approx(1 / 2)


class TestOffsetsStillWork:
    def test_a_constant_ratio_across_codes_is_still_one_offset(self):
        """The contract key uses the same payer label the residual row carries."""
        left, right = [], []
        for i in range(30):
            code = f"{i % 9 + 1}{i:04d}"
            left.append(hospital(code, 100.0 + i))
            right.append(payer(1.4 * (100.0 + i), code=code, plan="ChoicePlus"))
            right.append(payer(1.4 * (100.0 + i), code=code, plan="SelectEPO"))
        recon = Reconciliation(hospital="NYU", system="NYU Langone", hospital_slug="nyu")
        mart, rows = stream_shard(left, right)
        recon.add_shard("all", mart, rows=rows)
        recon.close()

        assert len(recon.offsets) == 1
        assert recon.residual == []
        assert recon.offsets[0].payer_plan == "ChoicePlus; SelectEPO"


class TestTheResidualCarriesTheDistribution:
    def test_min_max_count_and_inside(self):
        recon = Reconciliation(hospital="NYU", system="NYU Langone", hospital_slug="nyu")
        mart, rows = stream_shard(
            [hospital(rate=800.0)], [payer(300.0), payer(500.0, plan="SelectEPO")]
        )
        recon.add_shard("7", mart, rows=rows)
        recon.close()

        (row,) = recon.residual
        assert (row.payer_min, row.payer_rate, row.payer_max, row.payer_count) == (
            300.0,
            400.0,
            500.0,
            2,
        )
        assert row.inside_payer_range is False
        assert row.payer_plan == "ChoicePlus; SelectEPO"

    def test_the_spread_of_one_rate(self):
        spread = PayerSpread.of([payer(250.0)])

        assert (spread.minimum, spread.median, spread.maximum, spread.count) == (
            250.0,
            250.0,
            250.0,
            1,
        )


class TestThePlanQuestionAcrossNetworks:
    """Asked of every network in the distribution, not skipped.

    Written after the first rebuild on this grain: skipping the plan check for
    multi-network distributions sent Mount Sinai's residual from 1.8% to 55% of
    what formed. Its first exemplar is the case below, verbatim from the lake:
    an administrator plan, six times below every Aetna network's rate.
    """

    AETNA = ("Epo", "NY", "OpenAccessElectChoice", "OpenAccessManagedChoice", "Ppo")

    def aetna(self, rate: float, network: str) -> ComparableRate:
        return payer(rate, plan=network, payer="Aetna")

    def test_a_plan_matching_no_network_is_unresolved_not_a_finding(self):
        left = hospital(
            rate=2_503.98, payer="Aetna", plan="Aetna Signature Administrators/Tpa - Msq"
        )
        right = [self.aetna(15_437.4 + i * 600, n) for i, n in enumerate(self.AETNA)]

        rows, _ = run([left], right)

        assert rows[0].explanation == Explanation.PLAN_UNRESOLVED
        assert "any of the carrier's 5 networks" in rows[0].notes[-1]

    def test_a_plan_matching_one_network_is_a_finding_outside_the_whole_range(self):
        left = hospital(rate=2_503.98, payer="Aetna", plan="Aetna PPO - Msq")
        right = [self.aetna(15_437.4 + i * 600, n) for i, n in enumerate(self.AETNA)]

        rows, _ = run([left], right)

        assert rows[0].explanation == Explanation.UNEXPLAINED

    def test_a_plan_naming_several_networks_is_granularity(self):
        left = hospital(rate=2_503.98, payer="Aetna", plan="Aetna Hmo/Pos/Epo - Msq")
        right = [self.aetna(15_437.4 + i * 600, n) for i, n in enumerate(self.AETNA)]

        rows, _ = run([left], right)

        assert rows[0].explanation == Explanation.GRANULARITY_MISMATCH

    def test_inside_the_range_is_still_decided_first(self):
        left = hospital(rate=16_000.0, payer="Aetna", plan="Aetna Signature Administrators/Tpa")
        right = [self.aetna(15_437.4 + i * 600, n) for i, n in enumerate(self.AETNA)]

        rows, _ = run([left], right)

        assert rows[0].explanation == Explanation.WITHIN_PAYER_RANGE
