import pytest

from model.loaders import RateTable, WeightTable
from model.nys_medicaid import Basis, DrgWeight, RateSchedule
from model.scenarios import (
    CaseMix,
    CaseMixEntry,
    Scenario,
    compare,
    model_across_hospitals,
    model_case_mix,
)

WEIGHTS = WeightTable(
    [
        DrgWeight("194", 1, siw=0.5000, alos=3.0, cost_outlier_threshold=57181.0),
        DrgWeight("194", 2, siw=0.7912, alos=5.0, cost_outlier_threshold=57181.0),
        DrgWeight("194", 3, siw=1.2000, alos=7.0, cost_outlier_threshold=57181.0),
        DrgWeight("194", 4, siw=2.0000, alos=11.0, cost_outlier_threshold=57181.0),
        DrgWeight("139", 2, siw=0.6000, alos=4.0, cost_outlier_threshold=48000.0),
    ]
)

MAIMONIDES = RateSchedule(
    opcert="7001020",
    hospital="MAIMONIDES MEDICAL CENTER",
    discharge_rate=8870.0,
    isaf=1.1890,
    high_cost_charge_converter=0.25,
    capital_per_discharge=1468.0,
)
UNITY = RateSchedule(
    opcert="2754001",
    hospital="THE UNITY HOSPITAL OF ROCHESTER",
    discharge_rate=5488.0,
    isaf=0.7356,
    high_cost_charge_converter=0.25,
    capital_per_discharge=499.0,
)
RATES = RateTable([MAIMONIDES, UNITY])

BOOK = CaseMix(
    "heart failure book",
    [
        CaseMixEntry("194", 1, cases=100, average_days=3),
        CaseMixEntry("194", 2, cases=200, average_days=5),
        CaseMixEntry("194", 3, cases=50, average_days=7),
    ],
)


class TestModelCaseMix:
    def test_totals_and_case_mix_index(self):
        result = model_case_mix(BOOK, MAIMONIDES, WEIGHTS, Basis.MMC)

        assert result.cases == 350
        expected_cmi = (0.5 * 100 + 0.7912 * 200 + 1.2 * 50) / 350
        assert result.case_mix_index == pytest.approx(expected_cmi)
        assert result.total_payment > 0
        assert result.payment_per_case == pytest.approx(result.total_payment / 350)

    def test_unpriced_drgs_are_counted_not_dropped(self):
        book = CaseMix("with a gap", [*BOOK.entries, CaseMixEntry("999", 1, cases=25)])

        result = model_case_mix(book, MAIMONIDES, WEIGHTS, Basis.MMC)

        assert result.unpriced_cases == 25
        assert result.cases == 350, "unpriced cases must not inflate the denominator"

    def test_transfer_share_lowers_the_total(self):
        with_transfers = CaseMix(
            "book", [CaseMixEntry("194", 2, cases=100, average_days=2, transfer_share=1.0)]
        )
        without = CaseMix("book", [CaseMixEntry("194", 2, cases=100, average_days=2)])

        transfer_result = model_case_mix(with_transfers, MAIMONIDES, WEIGHTS, Basis.MMC)
        inlier_result = model_case_mix(without, MAIMONIDES, WEIGHTS, Basis.MMC)

        assert transfer_result.total_payment < inlier_result.total_payment


class TestScenarios:
    def test_rate_increase_flows_through(self):
        baseline = model_case_mix(BOOK, MAIMONIDES, WEIGHTS, Basis.MMC)
        variant = model_case_mix(
            BOOK, MAIMONIDES, WEIGHTS, Basis.MMC, Scenario("+3% base", rate_multiplier=1.03)
        )

        result = compare(baseline, variant)
        assert result.delta > 0
        # Capital is a flat per-discharge add-on, so the lift is under 3%.
        assert 0 < result.delta_pct < 0.03
        assert result.driver == "rate"

    def test_rate_change_does_not_move_case_mix_index(self):
        baseline = model_case_mix(BOOK, MAIMONIDES, WEIGHTS, Basis.MMC)
        variant = model_case_mix(
            BOOK, MAIMONIDES, WEIGHTS, Basis.MMC, Scenario("+3%", rate_multiplier=1.03)
        )

        assert compare(baseline, variant).cmi_change == pytest.approx(0.0)

    def test_severity_shift_is_reported_as_case_mix_not_rate(self):
        """A payment rise from severity drift is not a rate win."""
        baseline = model_case_mix(BOOK, MAIMONIDES, WEIGHTS, Basis.MMC)
        drifted = model_case_mix(BOOK.shift_severity(0.10), MAIMONIDES, WEIGHTS, Basis.MMC)

        result = compare(baseline, drifted)
        assert result.delta > 0
        assert result.cmi_change > 0
        assert result.driver == "case mix"

    def test_severity_shift_preserves_volume(self):
        shifted = BOOK.shift_severity(0.10)

        assert shifted.total_cases == BOOK.total_cases

    def test_isaf_override_models_a_wage_index_change(self):
        baseline = model_case_mix(BOOK, UNITY, WEIGHTS, Basis.MMC)
        variant = model_case_mix(
            BOOK, UNITY, WEIGHTS, Basis.MMC, Scenario("NYC wage index", isaf_override=1.1890)
        )

        # ISAF alone moves outlier thresholds, not the inlier payment.
        assert variant.total_payment == pytest.approx(baseline.total_payment)


class TestAcrossHospitals:
    def test_same_book_priced_at_each_hospital(self):
        results = model_across_hospitals(BOOK, RATES, WEIGHTS, ["7001020H", "2754001"], Basis.MMC)

        assert next(r.hospital for r in results) == "MAIMONIDES MEDICAL CENTER"
        assert results[0].total_payment > results[1].total_payment
        assert results[0].case_mix_index == pytest.approx(results[1].case_mix_index), (
            "identical book must give identical CMI; only rates differ"
        )

    def test_unknown_licence_is_skipped(self):
        results = model_across_hospitals(BOOK, RATES, WEIGHTS, ["9999999H"], Basis.MMC)

        assert results == []
