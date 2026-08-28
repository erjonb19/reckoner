import pytest

from model.nys_medicaid import (
    MEDICAID_SURCHARGE,
    Basis,
    Claim,
    DrgWeight,
    PaymentType,
    RateSchedule,
    calculate,
    high_cost_outlier_payment,
    inlier_payment,
    transfer_payment,
)

# Maimonides, from the published 04/01/2025 managed care schedule.
MAIMONIDES = RateSchedule(
    opcert="7001020",
    hospital="MAIMONIDES MEDICAL CENTER",
    discharge_rate=8870.0,
    isaf=1.1890,
    high_cost_charge_converter=0.25,
    capital_per_discharge=1468.0,
    capital_per_diem=180.0,
    alc_rate=550.0,
    dme_rate=900.0,
    statewide_price=7460.0,
)

# APR-DRG 194-2 shape: a common medical DRG with a multi-day stay.
WEIGHT = DrgWeight(apr_drg="194", severity=2, siw=0.8500, alos=4.0, cost_outlier_threshold=120000.0)
SHORT_STAY = DrgWeight(
    apr_drg="860", severity=1, siw=0.3000, alos=1.0, cost_outlier_threshold=90000.0
)


class TestInlier:
    def test_managed_care_lines(self):
        payment = inlier_payment(Claim("194", 2, total_days=4), MAIMONIDES, WEIGHT, Basis.MMC)

        assert payment.lines["3_case_mix_adjusted"] == pytest.approx(8870 * 0.85)
        assert payment.lines["4_dme"] == 0.0, "DME is FFS-only"
        assert payment.lines["5_capital"] == pytest.approx(1468.0)
        assert payment.total == pytest.approx(8870 * 0.85 + 1468.0)
        assert payment.payment_type == PaymentType.INLIER

    def test_fee_for_service_adds_dme(self):
        mmc = inlier_payment(Claim("194", 2), MAIMONIDES, WEIGHT, Basis.MMC)
        ffs = inlier_payment(Claim("194", 2), MAIMONIDES, WEIGHT, Basis.FFS)

        assert ffs.total - mmc.total == pytest.approx(MAIMONIDES.dme_rate)

    def test_alc_days_are_paid_per_day(self):
        payment = inlier_payment(
            Claim("194", 2, total_days=10, alc_days=3), MAIMONIDES, WEIGHT, Basis.MMC
        )

        assert payment.lines["7c_alc"] == pytest.approx(3 * 550.0)
        assert payment.total == pytest.approx(payment.lines["6_inlier_drg"] + 1650.0)

    def test_severity_drives_the_payment(self):
        low = DrgWeight("001", 1, siw=6.1633, alos=22, cost_outlier_threshold=626969)
        high = DrgWeight("001", 4, siw=17.5054, alos=22, cost_outlier_threshold=626969)

        low_pay = inlier_payment(Claim("001", 1), MAIMONIDES, low, Basis.MMC)
        high_pay = inlier_payment(Claim("001", 4), MAIMONIDES, high, Basis.MMC)

        assert high_pay.total > low_pay.total * 2.5


class TestTransfer:
    def test_per_diem_build_up(self):
        claim = Claim("194", 2, total_days=2, is_transfer=True)

        payment = transfer_payment(claim, MAIMONIDES, WEIGHT, Basis.MMC)

        case_mix = 8870 * 0.85
        per_day = case_mix / 4.0 * 1.2 + 180.0
        assert payment.lines["8_transfer_adjustment_factor"] == 1.2
        assert payment.lines["11_total_per_diem"] == pytest.approx(per_day)
        assert payment.lines["12_transfer_before_addons"] == pytest.approx(per_day * 2)

    def test_short_stay_drg_uses_a_100_percent_factor(self):
        """DOH line 8: ALOS of 1 day means no 120% uplift."""
        claim = Claim("860", 1, total_days=1, is_transfer=True)

        payment = transfer_payment(claim, MAIMONIDES, SHORT_STAY, Basis.MMC)

        assert payment.lines["8_transfer_adjustment_factor"] == 1.0

    def test_transfer_never_pays_more_than_the_full_drg(self):
        """A long transfer stay must cap at the inlier amount."""
        claim = Claim("194", 2, total_days=30, is_transfer=True)

        payment = transfer_payment(claim, MAIMONIDES, WEIGHT, Basis.MMC)
        inlier = inlier_payment(claim, MAIMONIDES, WEIGHT, Basis.MMC)

        assert payment.lines["15_transfer_before_cap"] > payment.lines["16a_inlier_drg"]
        assert payment.lines["17_capped"] == pytest.approx(inlier.lines["6_inlier_drg"])
        assert "capped at inlier" in payment.notes

    def test_alc_days_are_excluded_from_the_per_diem_count(self):
        claim = Claim("194", 2, total_days=6, alc_days=2, is_transfer=True)

        payment = transfer_payment(claim, MAIMONIDES, WEIGHT, Basis.MMC)

        assert payment.lines["1c_days_excluding_alc"] == 4.0

    def test_zero_alos_is_rejected_rather_than_dividing_by_zero(self):
        broken = DrgWeight("999", 1, siw=1.0, alos=0.0, cost_outlier_threshold=1.0)

        with pytest.raises(ValueError, match="ALOS"):
            transfer_payment(Claim("999", 1, is_transfer=True), MAIMONIDES, broken, Basis.MMC)


class TestHighCostOutlier:
    def test_qualifying_case_pays_inlier_plus_excess(self):
        # 0.25 converter on 800k charges = 200k costs, threshold 120k x 1.189.
        claim = Claim("194", 2, gross_charges=800_000.0)

        payment = high_cost_outlier_payment(claim, MAIMONIDES, WEIGHT, Basis.MMC)

        assert payment is not None
        assert payment.lines["5_converted_costs"] == pytest.approx(200_000.0)
        assert payment.lines["6c_adjusted_threshold"] == pytest.approx(120_000 * 1.1890)
        assert payment.total == pytest.approx(
            payment.lines["8_outlier_before_inlier"] + payment.lines["9_inlier_with_alc"]
        )

    def test_below_threshold_does_not_qualify(self):
        claim = Claim("194", 2, gross_charges=100_000.0)

        assert high_cost_outlier_payment(claim, MAIMONIDES, WEIGHT, Basis.MMC) is None

    def test_non_covered_charges_are_removed_first(self):
        gross = Claim("194", 2, gross_charges=800_000.0)
        adjusted = Claim("194", 2, gross_charges=800_000.0, non_covered_charges=300_000.0)

        full = high_cost_outlier_payment(gross, MAIMONIDES, WEIGHT, Basis.MMC)
        assert full is not None
        assert high_cost_outlier_payment(adjusted, MAIMONIDES, WEIGHT, Basis.MMC) is None

    def test_outliers_do_not_apply_to_transfers(self):
        claim = Claim("194", 2, gross_charges=5_000_000.0, is_transfer=True)

        assert high_cost_outlier_payment(claim, MAIMONIDES, WEIGHT, Basis.MMC) is None

    def test_isaf_moves_the_threshold(self):
        cheap_area = RateSchedule(
            opcert="2754001",
            hospital="THE UNITY HOSPITAL OF ROCHESTER",
            discharge_rate=5488.0,
            isaf=0.7356,
            high_cost_charge_converter=0.25,
        )
        claim = Claim("194", 2, gross_charges=400_000.0)

        rich = high_cost_outlier_payment(claim, MAIMONIDES, WEIGHT, Basis.MMC)
        cheap = high_cost_outlier_payment(claim, cheap_area, WEIGHT, Basis.MMC)

        assert cheap is not None
        assert rich is None, "the higher ISAF raises the bar for an outlier"


class TestRouting:
    def test_transfer_routes_to_transfer(self):
        claim = Claim("194", 2, total_days=2, is_transfer=True)

        assert calculate(claim, MAIMONIDES, WEIGHT, Basis.MMC).payment_type == PaymentType.TRANSFER

    def test_high_charges_route_to_outlier(self):
        claim = Claim("194", 2, gross_charges=800_000.0)

        result = calculate(claim, MAIMONIDES, WEIGHT, Basis.MMC)
        assert result.payment_type == PaymentType.HIGH_COST_OUTLIER

    def test_ordinary_case_routes_to_inlier(self):
        assert calculate(Claim("194", 2), MAIMONIDES, WEIGHT, Basis.MMC).payment_type == (
            PaymentType.INLIER
        )


class TestSurcharge:
    def test_signed_election_pays_the_base_amount(self):
        claim = Claim("194", 2, provider_signed_surcharge_election=True)

        payment = inlier_payment(claim, MAIMONIDES, WEIGHT, Basis.MMC)

        assert payment.paid_to_hospital == pytest.approx(payment.total)

    def test_unsigned_election_adds_the_surcharge(self):
        claim = Claim("194", 2, provider_signed_surcharge_election=False)

        payment = inlier_payment(claim, MAIMONIDES, WEIGHT, Basis.MMC)

        assert payment.surcharge == pytest.approx(payment.total * MEDICAID_SURCHARGE)
        assert payment.paid_to_hospital == pytest.approx(payment.total * (1 + MEDICAID_SURCHARGE))


class TestGeography:
    def test_identical_case_pays_differently_by_hospital(self):
        """ISAF is the whole point: same DRG, same severity, different money."""
        unity = RateSchedule(
            opcert="2754001",
            hospital="THE UNITY HOSPITAL OF ROCHESTER",
            discharge_rate=5488.0,
            isaf=0.7356,
            high_cost_charge_converter=0.25,
            capital_per_discharge=499.0,
        )
        claim = Claim("194", 2)

        rich = inlier_payment(claim, MAIMONIDES, WEIGHT, Basis.MMC)
        cheap = inlier_payment(claim, unity, WEIGHT, Basis.MMC)

        assert rich.total > cheap.total
        assert rich.total / cheap.total > 1.5


class TestTeachingAddons:
    """The MMC published rate says "EXCLUDING IME"; margin work must add it back."""

    TEACHING = RateSchedule(
        opcert="7002053",
        hospital="NYU LANGONE HOSPITALS",
        discharge_rate=7899.0,
        isaf=1.0589,
        high_cost_charge_converter=0.25,
        capital_per_discharge=1742.0,
        ime_pct=0.297,
        dme_rate=500.0,
    )
    COMMUNITY = RateSchedule(
        opcert="4601001",
        hospital="ELLIS HOSPITAL",
        discharge_rate=6020.0,
        isaf=0.8070,
        high_cost_charge_converter=0.25,
        capital_per_discharge=511.0,
        ime_pct=0.052,
        dme_rate=100.0,
    )

    def test_off_by_default_so_the_doh_worksheet_reproduces(self):
        payment = inlier_payment(Claim("194", 2), self.TEACHING, WEIGHT, Basis.MMC)

        assert payment.lines["6b_teaching_addons"] == 0.0
        assert payment.total == pytest.approx(payment.lines["6_inlier_drg"])

    def test_adding_them_raises_payment_by_ime_and_dme(self):
        payment = inlier_payment(
            Claim("194", 2), self.TEACHING, WEIGHT, Basis.MMC, include_teaching_addons=True
        )
        base = payment.lines["6_inlier_drg"]

        assert payment.lines["6b_teaching_addons"] == pytest.approx(base * 0.297 + 500.0)
        assert payment.total > base

    def test_the_effect_is_concentrated_on_teaching_hospitals(self):
        """5.2% IME at a community hospital against 29.7% at an academic centre."""
        academic = inlier_payment(
            Claim("194", 2), self.TEACHING, WEIGHT, Basis.MMC, include_teaching_addons=True
        )
        community = inlier_payment(
            Claim("194", 2), self.COMMUNITY, WEIGHT, Basis.MMC, include_teaching_addons=True
        )

        academic_lift = academic.lines["6b_teaching_addons"] / academic.lines["6_inlier_drg"]
        community_lift = community.lines["6b_teaching_addons"] / community.lines["6_inlier_drg"]
        assert academic_lift > community_lift * 3

    def test_ffs_does_not_double_count_dme(self):
        """FFS already pays DME on worksheet line 4."""
        payment = inlier_payment(
            Claim("194", 2), self.TEACHING, WEIGHT, Basis.FFS, include_teaching_addons=True
        )

        assert payment.lines["4_dme"] == pytest.approx(500.0)
        assert payment.lines["6b_teaching_addons"] == pytest.approx(
            payment.lines["6_inlier_drg"] * 0.297
        ), "IME only; DME must not be added twice"

    def test_the_flag_reaches_the_router(self):
        with_addons = calculate(
            Claim("194", 2), self.TEACHING, WEIGHT, Basis.MMC, include_teaching_addons=True
        )
        without = calculate(Claim("194", 2), self.TEACHING, WEIGHT, Basis.MMC)

        assert with_addons.total > without.total
