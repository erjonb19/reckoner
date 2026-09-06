"""Variance mart tests.

The mart's job is not to maximise rows. It is to produce rows that survive the
cheap explanations, and to count what it dropped. Both halves are tested.
"""

import pytest

from reconcile.comparability import ComparableRate
from reconcile.variance import (
    Explanation,
    cross_hospital_variance,
    cross_source_variance,
    explain,
    spread_by_code,
)


def rate(**overrides: object) -> ComparableRate:
    base = {
        "source": "hospital",
        "hospital": "Example Hospital",
        "code": "470",
        "code_type": "MS-DRG",
        "setting": "inpatient",
        # Stated, because a cross-source pair is refused without it.
        "billing_class": "facility",
        "payer": "Aetna",
        "plan": "Commercial PPO",
        "product_class": "commercial",
        "rate_kind": "dollar",
        "rate_dollar": 25000.0,
        "vintage": "2026-01-01",
    }
    return ComparableRate(**{**base, **overrides})  # type: ignore[arg-type]


class TestExplanation:
    def test_far_apart_vintages_explain_the_difference(self):
        explanation, notes = explain(
            rate(vintage="2025-01-01"), rate(vintage="2026-06-01", rate_dollar=30000.0)
        )

        assert explanation == Explanation.VINTAGE_ARTIFACT
        assert notes

    def test_aggregate_plan_against_named_plan_is_granularity(self):
        explanation, _ = explain(
            rate(product_class="commercial_aggregate", plan="All Commercial Plans"),
            rate(product_class="commercial", plan="Choice Plus", rate_dollar=31000.0),
        )

        assert explanation == Explanation.GRANULARITY_MISMATCH

    def test_different_plan_names_are_granularity(self):
        explanation, _ = explain(
            rate(plan="Choice Plus"), rate(plan="Navigate", rate_dollar=28000.0)
        )

        assert explanation == Explanation.GRANULARITY_MISMATCH

    def test_wildly_different_rates_suspect_the_entity_match(self):
        # One service, one payer, ten times apart is not a negotiated
        # difference -- it is two contracts merged under one name.
        explanation, notes = explain(rate(rate_dollar=2000.0), rate(rate_dollar=40000.0))

        assert explanation == Explanation.ENTITY_RESOLUTION_SUSPECT
        assert notes

    def test_a_plain_difference_is_unexplained(self):
        explanation, _ = explain(rate(rate_dollar=25000.0), rate(rate_dollar=28000.0))

        assert explanation == Explanation.UNEXPLAINED


class TestVintageMustAlsoBePlausible:
    """A vintage gap is a precondition for a timing artifact, not evidence of one.

    Hospital files update at least annually and payer files monthly, so in
    cross-source mode every pair has a large gap. If the gap alone explained a
    row, the explanation column would sort rows by which file they came from --
    which is exactly what it did, labelling 72% of an Aetna run
    ``vintage_artifact`` and leaving ``unexplained`` empty.
    """

    def test_a_difference_drift_could_produce_is_a_vintage_artifact(self):
        # +8% over a year is an ordinary escalator.
        explanation, notes = explain(
            rate(vintage="2025-01-01", rate_dollar=25000.0),
            rate(vintage="2026-01-01", rate_dollar=27000.0),
        )

        assert explanation == Explanation.VINTAGE_ARTIFACT
        assert "within" in notes[0]

    def test_a_difference_too_large_for_drift_is_not_a_vintage_artifact(self):
        # The same gap, but the price nearly tripled. Seven months of drift did
        # not do that, so timing is ruled out rather than credited.
        explanation, _ = explain(
            rate(vintage="2025-01-01", rate_dollar=10000.0),
            rate(vintage="2026-01-01", rate_dollar=29000.0),
        )

        assert explanation != Explanation.VINTAGE_ARTIFACT
        assert explanation == Explanation.UNEXPLAINED

    def test_the_bound_scales_with_the_gap(self):
        """20% is drift over two years and a rebase over one month."""
        slow = explain(
            rate(vintage="2024-01-01", rate_dollar=10000.0),
            rate(vintage="2026-01-01", rate_dollar=12000.0),
        )[0]
        fast = explain(
            rate(vintage="2025-12-01", rate_dollar=10000.0),
            rate(vintage="2026-01-01", rate_dollar=12000.0),
        )[0]

        assert slow == Explanation.VINTAGE_ARTIFACT
        assert fast == Explanation.UNEXPLAINED

    def test_identical_vintages_are_never_a_timing_artifact(self):
        explanation, _ = explain(
            rate(vintage="2026-01-01", rate_dollar=25000.0),
            rate(vintage="2026-01-01", rate_dollar=26000.0),
        )

        assert explanation == Explanation.UNEXPLAINED

    def test_an_unknown_vintage_does_not_earn_the_explanation(self):
        explanation, _ = explain(
            rate(vintage=None, rate_dollar=25000.0),
            rate(vintage="2026-01-01", rate_dollar=26000.0),
        )

        assert explanation == Explanation.UNEXPLAINED

    def test_an_implausible_ratio_beats_a_stale_vintage(self):
        """Ten times apart is a bad match, not seven months of inflation.

        Ordering regression: the timing check used to run first and would claim
        any sufficiently stale pair, however impossible the price gap.
        """
        explanation, _ = explain(
            rate(vintage="2025-01-01", rate_dollar=2000.0),
            rate(vintage="2026-06-01", rate_dollar=40000.0),
        )

        assert explanation == Explanation.ENTITY_RESOLUTION_SUSPECT


class TestPlansAcrossSources:
    """A hospital plan name and a payer network label are different vocabularies."""

    def test_unmatched_plans_across_sources_are_unresolved_not_a_finding(self):
        explanation, notes = explain(
            rate(source="hospital", plan="Aetna Choice POS II", rate_dollar=25000.0),
            rate(source="payer", plan="OpenAccessManagedChoice", rate_dollar=28000.0),
        )

        assert explanation == Explanation.PLAN_UNRESOLVED
        assert "not matched across sources" in notes[0]

    def test_within_one_source_differing_plans_are_still_granularity(self):
        """Same vocabulary, so a difference does mean two different contracts."""
        explanation, _ = explain(
            rate(source="hospital", plan="Choice Plus", rate_dollar=25000.0),
            rate(source="hospital", plan="Navigate", rate_dollar=28000.0),
        )

        assert explanation == Explanation.GRANULARITY_MISMATCH

    def test_matching_plans_across_sources_reach_unexplained(self):
        explanation, _ = explain(
            rate(source="hospital", plan="Open Access", rate_dollar=25000.0),
            rate(source="payer", plan="open access", rate_dollar=28000.0),
        )

        assert explanation == Explanation.UNEXPLAINED

    def test_an_aggregate_plan_still_outranks_an_unresolved_one(self):
        explanation, _ = explain(
            rate(source="hospital", product_class="commercial_aggregate", plan="All Plans"),
            rate(source="payer", product_class="commercial", plan="PPO", rate_dollar=31000.0),
        )

        assert explanation == Explanation.GRANULARITY_MISMATCH


class TestCrossSource:
    def test_matching_pair_produces_a_variance(self):
        mart = cross_source_variance([rate()], [rate(source="payer", rate_dollar=27000.0)])

        assert len(mart.rows) == 1
        row = mart.rows[0]
        assert row.difference == pytest.approx(2000.0)
        assert row.relative_difference == pytest.approx(0.08)
        assert row.is_material

    def test_hospital_rate_with_no_counterpart_is_counted(self):
        mart = cross_source_variance([rate()], [])

        assert not mart.rows
        assert mart.excluded["no payer-side counterpart"] == 1

    def test_exempt_products_are_excluded_with_their_reason(self):
        mart = cross_source_variance(
            [rate(product_class="medicare_advantage")],
            [rate(source="payer", product_class="medicare_advantage")],
        )

        assert not mart.rows
        assert "tic_exempt_product" in mart.excluded

    def test_pairs_join_within_a_hospital(self):
        # A payer rate for another hospital must not satisfy this hospital.
        mart = cross_source_variance([rate(hospital="A")], [rate(source="payer", hospital="B")])

        assert not mart.rows

    def test_provenance_carries_the_vintage_caveat(self):
        mart = cross_source_variance([rate()], [rate(source="payer", rate_dollar=27000.0)])

        assert any("timing artifact" in note for note in mart.provenance.caveats)

    def test_comparable_share_is_reported(self):
        mart = cross_source_variance(
            [rate(), rate(code="871")], [rate(source="payer", rate_dollar=27000.0)]
        )

        assert mart.comparable_share == pytest.approx(0.5)


class TestCrossHospital:
    def test_spread_between_two_hospitals(self):
        mart = cross_hospital_variance(
            [
                rate(hospital="Cheap General", rate_dollar=20000.0),
                rate(hospital="Dear Memorial", rate_dollar=30000.0),
            ]
        )

        assert len(mart.rows) == 1
        row = mart.rows[0]
        assert row.left.hospital == "Cheap General"
        assert row.right.hospital == "Dear Memorial"
        assert row.relative_difference == pytest.approx(0.5)

    def test_single_hospital_key_is_excluded(self):
        mart = cross_hospital_variance([rate()])

        assert not mart.rows
        assert mart.excluded["only one hospital publishes this service and payer"] == 1

    def test_non_dollar_rows_are_excluded_by_reason(self):
        mart = cross_hospital_variance(
            [rate(rate_kind="percentage", rate_dollar=None), rate(hospital="B")]
        )

        assert "not_dollar_denominated" in mart.excluded

    def test_a_hospital_contributes_one_representative_rate(self):
        # Five plan rows at one hospital must not outvote a single row at
        # another; the median row stands for the hospital.
        mart = cross_hospital_variance(
            [
                *[rate(hospital="Many", rate_dollar=float(r)) for r in (10, 20, 30, 40, 50)],
                rate(hospital="One", rate_dollar=100.0),
            ]
        )

        assert len(mart.rows) == 1
        assert mart.rows[0].left.rate_dollar == pytest.approx(30.0)
        assert mart.rows[0].right.rate_dollar == pytest.approx(100.0)

    def test_keys_are_separated_by_payer_and_setting(self):
        mart = cross_hospital_variance(
            [
                rate(hospital="A", payer="Aetna"),
                rate(hospital="B", payer="Cigna"),
            ]
        )

        assert not mart.rows

    def test_hospital_count_reaches_provenance(self):
        mart = cross_hospital_variance(
            [rate(hospital="A", rate_dollar=1.0), rate(hospital="B", rate_dollar=2.0)]
        )

        assert mart.provenance.hospitals == 2


class TestMartSummaries:
    def test_unexplained_excludes_immaterial_and_explained_rows(self):
        mart = cross_hospital_variance(
            [
                # Material and unexplained.
                rate(code="470", hospital="A", rate_dollar=20000.0),
                rate(code="470", hospital="B", rate_dollar=24000.0),
                # Material but explained by vintage.
                rate(code="871", hospital="A", rate_dollar=10000.0, vintage="2025-01-01"),
                rate(code="871", hospital="B", rate_dollar=14000.0, vintage="2026-01-01"),
                # Immaterial.
                rate(code="291", hospital="A", rate_dollar=10000.0),
                rate(code="291", hospital="B", rate_dollar=10100.0),
            ]
        )

        assert {row.code for row in mart.unexplained} == {"470"}
        assert mart.by_explanation()[str(Explanation.VINTAGE_ARTIFACT)] == 1

    def test_spread_by_code_ranks_widest_first(self):
        mart = cross_hospital_variance(
            [
                rate(code="470", hospital="A", rate_dollar=10000.0),
                rate(code="470", hospital="B", rate_dollar=11000.0),
                rate(code="871", hospital="A", rate_dollar=10000.0),
                rate(code="871", hospital="B", rate_dollar=20000.0),
            ]
        )

        ranked = spread_by_code(mart)

        assert [code for code, _, _ in ranked] == ["871", "470"]

    def test_summary_mentions_what_was_dropped(self):
        mart = cross_hospital_variance([rate()])

        assert "0 comparable pairs" in mart.summary()
