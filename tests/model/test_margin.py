import pytest

from model.loaders import WeightTable
from model.margin import margin_for_case_mix
from model.nys_medicaid import Basis, DrgWeight, RateSchedule
from model.scenarios import CaseMix, CaseMixEntry

WEIGHTS = WeightTable(
    [
        DrgWeight("194", 2, siw=0.7912, alos=5.0, cost_outlier_threshold=57181.0),
        DrgWeight("560", 1, siw=0.4000, alos=2.0, cost_outlier_threshold=40000.0),
    ]
)
RATE = RateSchedule(
    opcert="7001020",
    hospital="MAIMONIDES MEDICAL CENTER",
    discharge_rate=8870.0,
    isaf=1.1890,
    high_cost_charge_converter=0.25,
    capital_per_discharge=1468.0,
)
BOOK = CaseMix("book", [CaseMixEntry("194", 2, cases=1000, average_days=5)])
COSTS = {"194-2": 12000.0, "560-1": 8000.0}


class TestPayerShare:
    def test_unweighted_prices_every_discharge_and_says_so(self):
        """The error this exists to prevent: all-payer volume at one payer's rates."""
        result = margin_for_case_mix(BOOK, RATE, WEIGHTS, COSTS, Basis.MMC)

        assert result.cases == 1000
        assert any("EVERY discharge" in c for c in result.provenance.caveats)

    def test_payer_share_scales_volume_and_dollars(self):
        full = margin_for_case_mix(BOOK, RATE, WEIGHTS, COSTS, Basis.MMC)
        medicaid = margin_for_case_mix(BOOK, RATE, WEIGHTS, COSTS, Basis.MMC, payer_share=0.523)

        assert medicaid.cases == 523
        assert medicaid.total_payment == pytest.approx(full.total_payment * 0.523, rel=1e-3)
        assert medicaid.total_cost == pytest.approx(full.total_cost * 0.523, rel=1e-3)

    def test_margin_percentage_is_unchanged_by_scaling(self):
        """Scaling changes the dollars, not the economics per case."""
        full = margin_for_case_mix(BOOK, RATE, WEIGHTS, COSTS, Basis.MMC)
        part = margin_for_case_mix(BOOK, RATE, WEIGHTS, COSTS, Basis.MMC, payer_share=0.5)

        assert part.margin_pct == pytest.approx(full.margin_pct, rel=1e-3)

    def test_scaled_result_carries_the_share_as_a_caveat(self):
        result = margin_for_case_mix(BOOK, RATE, WEIGHTS, COSTS, Basis.MMC, payer_share=0.52)

        assert any("52% of discharges" in c for c in result.provenance.caveats)

    @pytest.mark.parametrize("bad", [0.0, -0.1, 1.5])
    def test_impossible_shares_are_rejected(self, bad):
        with pytest.raises(ValueError, match="payer_share"):
            margin_for_case_mix(BOOK, RATE, WEIGHTS, COSTS, Basis.MMC, payer_share=bad)


class TestOutlierCharges:
    def test_without_charges_no_outlier_can_fire(self):
        result = margin_for_case_mix(BOOK, RATE, WEIGHTS, COSTS, Basis.MMC)

        assert all(line.payment_type == "inlier" for line in result.lines)
        assert any("no high-cost outlier" in c for c in result.provenance.caveats)

    def test_high_charges_trigger_an_outlier_and_raise_payment(self):
        charges = {"194-2": 400_000.0}

        without = margin_for_case_mix(BOOK, RATE, WEIGHTS, COSTS, Basis.MMC)
        with_charges = margin_for_case_mix(BOOK, RATE, WEIGHTS, COSTS, Basis.MMC, charges=charges)

        assert with_charges.total_payment > without.total_payment
        assert with_charges.lines[0].payment_type == "high_cost_outlier"

    def test_ordinary_charges_stay_inlier(self):
        result = margin_for_case_mix(
            BOOK, RATE, WEIGHTS, COSTS, Basis.MMC, charges={"194-2": 30_000.0}
        )

        assert result.lines[0].payment_type == "inlier"


class TestUnknowns:
    def test_unpriced_and_uncosted_cases_are_counted_not_absorbed(self):
        book = CaseMix(
            "book",
            [
                CaseMixEntry("194", 2, cases=100),
                CaseMixEntry("999", 1, cases=50),  # no weight
                CaseMixEntry("560", 1, cases=25),  # no cost
            ],
        )

        result = margin_for_case_mix(book, RATE, WEIGHTS, {"194-2": 12000.0}, Basis.MMC)

        assert result.cases == 100
        assert result.unpriced_cases == 50
        assert result.uncosted_cases == 25
        assert result.provenance.excluded["no APR-DRG weight"] == 50
        assert result.provenance.excluded["no SPARCS cost for this DRG"] == 25


class TestUnderwater:
    def test_underwater_lines_are_ranked_worst_first(self):
        book = CaseMix(
            "book",
            [CaseMixEntry("194", 2, cases=1000), CaseMixEntry("560", 1, cases=1000)],
        )
        costs = {"194-2": 99_000.0, "560-1": 5_000.0}

        result = margin_for_case_mix(book, RATE, WEIGHTS, costs, Basis.MMC)

        assert result.underwater_lines[0].key == "194-2"
        assert result.underwater_share == pytest.approx(0.5)

    def test_a_profitable_book_has_none(self):
        result = margin_for_case_mix(BOOK, RATE, WEIGHTS, {"194-2": 100.0}, Basis.MMC)

        assert result.underwater_lines == []
        assert result.margin > 0
