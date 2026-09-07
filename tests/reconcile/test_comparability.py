"""Comparability tests.

Each refusal here is a way to produce a variance that is arithmetically correct
and meaningless, so the tests are written as "this pair must NOT be compared"
rather than as coverage of the branches.
"""

import pytest

from reconcile.comparability import (
    DOLLAR_COMPARABLE_METHODOLOGIES,
    ComparableRate,
    NotComparable,
    can_compare,
    classify_methodology,
    setting_key,
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


class TestMethodologyClassification:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Per Diem", "per_diem"),
            ("per-diem rate", "per_diem"),
            ("Case Rate", "case_rate"),
            ("DRG based", "case_rate"),
            ("percent of charges", "percent_of_charges"),
            ("% of Medicare", "percent_of_medicare"),
            ("fee schedule", "fee_schedule"),
            ("Capitation", "capitation"),
            ("", "unknown"),
            (None, "unknown"),
            ("something nobody has seen", "unknown"),
        ],
    )
    def test_families(self, text, expected):
        assert classify_methodology(text) == expected

    def test_per_diem_is_not_dollar_comparable(self):
        # A per diem and a case rate differ by length of stay, not by price.
        assert "per_diem" not in DOLLAR_COMPARABLE_METHODOLOGIES
        assert "capitation" not in DOLLAR_COMPARABLE_METHODOLOGIES


class TestStructural:
    def test_same_thing_compares(self):
        assert can_compare(rate(), rate(hospital="Other"))

    def test_different_codes_do_not(self):
        verdict = can_compare(rate(), rate(code="871"))

        assert not verdict
        assert verdict.reason == NotComparable.DIFFERENT_CODE

    def test_zero_padding_is_not_a_difference(self):
        # Hospitals publish MS-DRG 064 and 64 for the same DRG.
        assert can_compare(rate(code="064"), rate(code="64"))

    def test_same_digits_different_family_do_not(self):
        # Revenue code 0470 and MS-DRG 470 are the same digits, different things.
        verdict = can_compare(
            rate(code="470", code_type="MS-DRG"), rate(code="470", code_type="RC")
        )

        assert not verdict
        assert verdict.reason == NotComparable.DIFFERENT_CODE_TYPE

    def test_inpatient_against_outpatient_does_not(self):
        verdict = can_compare(rate(), rate(setting="outpatient"))

        assert not verdict
        assert verdict.reason == NotComparable.DIFFERENT_SETTING

    def test_a_missing_setting_is_not_a_disagreement(self):
        # Refusing on a blank field would drop every file that omits it.
        assert can_compare(rate(), rate(setting=None))

    def test_billing_class_must_agree_when_stated(self):
        verdict = can_compare(
            rate(billing_class="institutional"), rate(billing_class="professional")
        )

        assert not verdict
        assert verdict.reason == NotComparable.DIFFERENT_BILLING_CLASS


class TestMethodological:
    def test_percentage_rates_are_not_dollar_comparable(self):
        verdict = can_compare(
            rate(rate_kind="percentage", rate_dollar=None),
            rate(rate_kind="percentage", rate_dollar=None),
        )

        assert not verdict
        assert verdict.reason == NotComparable.NOT_DOLLAR_DENOMINATED

    def test_dollar_against_percentage_is_a_mixed_kind(self):
        verdict = can_compare(rate(), rate(rate_kind="percentage", rate_dollar=None))

        assert not verdict
        assert verdict.reason == NotComparable.MIXED_RATE_KIND

    def test_per_diem_against_case_rate_does_not_compare(self):
        verdict = can_compare(rate(methodology="per diem"), rate(methodology="case rate"))

        assert not verdict
        assert verdict.reason == NotComparable.INCOMPATIBLE_METHODOLOGY

    def test_blank_methodology_is_treated_as_comparable(self):
        # Most files leave it blank for ordinary fee-schedule rates; refusing
        # would empty the mart.
        assert can_compare(rate(methodology=None), rate(methodology=""))

    def test_zero_rate_is_refused(self):
        verdict = can_compare(rate(), rate(rate_dollar=0.0))

        assert not verdict
        assert verdict.reason == NotComparable.ZERO_RATE

    def test_missing_rate_is_refused(self):
        verdict = can_compare(rate(), rate(rate_dollar=None))

        assert not verdict
        assert verdict.reason == NotComparable.MISSING_RATE


class TestTemporal:
    def test_close_vintages_compare(self):
        assert can_compare(rate(vintage="2026-01-01"), rate(vintage="2026-06-01"))

    def test_distant_vintages_do_not(self):
        verdict = can_compare(rate(vintage="2024-01-01"), rate(vintage="2026-06-01"))

        assert not verdict
        assert verdict.reason == NotComparable.VINTAGE_TOO_FAR_APART

    def test_unknown_vintage_is_a_caveat_not_a_refusal(self):
        assert can_compare(rate(vintage=None), rate(vintage="2026-06-01"))

    def test_threshold_is_configurable(self):
        pair = (rate(vintage="2026-01-01"), rate(vintage="2026-08-01"))

        assert can_compare(*pair, max_vintage_days=400)
        assert not can_compare(*pair, max_vintage_days=30)


class TestCrossSourceExemptions:
    @pytest.mark.parametrize(
        "product",
        ["medicare_advantage", "medicaid_managed", "dual_or_ltc", "essential_plan"],
    )
    def test_tic_exempt_products_cannot_be_reconciled(self, product):
        # These exist only in hospital-side files. A payer-side "counterpart"
        # is impossible by rule, so any variance would be an artefact.
        verdict = can_compare(rate(product_class=product), rate(source="payer"), cross_source=True)

        assert not verdict
        assert verdict.reason == NotComparable.TIC_EXEMPT_PRODUCT

    def test_commercial_products_are_reconcilable(self):
        assert can_compare(
            rate(product_class="commercial"),
            rate(source="payer", product_class="commercial"),
            cross_source=True,
        )


class TestUnrestrictedSetting:
    """``both`` is not a third setting; it is a rate that applies in either.

    Comparing it as an ordinary string made it disagree with everything. On
    Mount Sinai 97% of payer rates carry ``both`` or no place of service while
    the hospital always names inpatient or outpatient, so the literal comparison
    refused nearly every pair -- including all 507,839 UnitedHealthcare rates,
    which never met a hospital rate at all.
    """

    def test_both_is_comparable_with_a_specific_setting(self):
        assert can_compare(rate(setting="inpatient"), rate(source="payer", setting="both"))
        assert can_compare(rate(setting="outpatient"), rate(source="payer", setting="both"))

    def test_two_specific_and_different_settings_still_refuse(self):
        verdict = can_compare(rate(setting="inpatient"), rate(source="payer", setting="outpatient"))

        assert not verdict
        assert verdict.reason == NotComparable.DIFFERENT_SETTING

    def test_an_absent_setting_stays_compatible(self):
        assert can_compare(rate(setting=None), rate(source="payer", setting="inpatient"))

    def test_both_against_both_is_comparable(self):
        assert can_compare(rate(setting="both"), rate(source="payer", setting="both"))

    def test_the_bucket_collapses_only_the_wildcards(self):
        assert setting_key("both") == ""
        assert setting_key(None) == ""
        assert setting_key("  ") == ""
        assert setting_key("Inpatient") == "inpatient"
        assert setting_key("inpatient") != setting_key("outpatient")


class TestUnstatedBillingClass:
    """A payer always states professional or institutional; a hospital often does not.

    Treating the omission as compatible-with-anything is a silent cross-join
    across sources: one unstated hospital rate meets both of the payer's rates
    for the same code. On one NYU facility that put 96.4% of pairs against the
    payer's *professional* rate -- the hospital's charge for a scan against the
    radiologist's fee for reading it -- and those were six times likelier to land
    ten-fold apart than the facility-to-facility pairs.
    """

    def test_an_unstated_hospital_billing_class_refuses_the_pair(self):
        verdict = can_compare(
            rate(billing_class=None),
            rate(source="payer", billing_class="professional"),
            cross_source=True,
        )

        assert not verdict
        assert verdict.reason == NotComparable.BILLING_CLASS_UNSTATED

    def test_both_sides_unstated_still_refuses(self):
        verdict = can_compare(
            rate(billing_class=None),
            rate(source="payer", billing_class=None),
            cross_source=True,
        )

        assert not verdict
        assert verdict.reason == NotComparable.BILLING_CLASS_UNSTATED

    def test_both_sides_stated_and_matching_is_comparable(self):
        assert can_compare(
            rate(billing_class="facility"),
            rate(source="payer", billing_class="facility"),
            cross_source=True,
        )

    def test_a_stated_disagreement_keeps_its_own_reason(self):
        """Stated-and-different is a different fact from not-stated-at-all."""
        verdict = can_compare(
            rate(billing_class="facility"),
            rate(source="payer", billing_class="professional"),
            cross_source=True,
        )

        assert not verdict
        assert verdict.reason == NotComparable.DIFFERENT_BILLING_CLASS

    def test_same_source_comparisons_are_unaffected(self):
        """Two hospitals that both omit the field omit it the same way."""
        assert can_compare(rate(billing_class=None), rate(billing_class=None))

    def test_scope_exemption_still_outranks_it(self):
        """A Medicare Advantage rate can never be reconciled at all."""
        verdict = can_compare(
            rate(billing_class=None, product_class="medicare_advantage"),
            rate(source="payer", billing_class=None),
            cross_source=True,
        )

        assert verdict.reason == NotComparable.TIC_EXEMPT_PRODUCT

    def test_exemption_only_applies_across_sources(self):
        # Two hospital files may be compared on a Medicare Advantage rate; it is
        # only the payer-side comparison that is impossible.
        assert can_compare(
            rate(product_class="medicare_advantage"),
            rate(product_class="medicare_advantage"),
        )
