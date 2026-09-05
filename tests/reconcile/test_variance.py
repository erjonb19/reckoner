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
