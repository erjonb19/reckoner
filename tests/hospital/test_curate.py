import pytest

from hospital.curate import CurateContext, CuratedRate, Reason, Reject, curate, row_hash
from hospital.parser import FileMeta, RawRate

META = FileMeta(hospital_name="Example Hospital", last_updated_on="2026-04-01", version="3.0.0")
CONTEXT = CurateContext("batch-1", "https://example.org/mrf.json", "Example Hospital", META)


def raw(**overrides: object) -> RawRate:
    base = {
        "ordinal": 1,
        "code": "70450",
        "code_type": "CPT",
        "payer_name": "Aetna",
        "plan_name": "All Commercial Plans",
        "rate_dollar": "412.55",
        "methodology": "fee schedule",
    }
    return RawRate(**{**base, **overrides})


class TestAccepts:
    def test_dollar_rate_is_curated(self):
        result = curate(raw(), CONTEXT)

        assert isinstance(result, CuratedRate)
        assert result.rate_dollar == pytest.approx(412.55)
        assert result.rate_kind == "dollar"
        assert result.product_class == "commercial_aggregate"
        assert result.file_vintage == "2026-04-01"

    def test_currency_formatting_is_tolerated(self):
        result = curate(raw(rate_dollar="$1,234.50"), CONTEXT)

        assert isinstance(result, CuratedRate)
        assert result.rate_dollar == pytest.approx(1234.50)

    def test_percentage_and_algorithm_rows_are_kept_with_their_kind(self):
        percent = curate(raw(rate_dollar=None, rate_percentage="45"), CONTEXT)
        algo = curate(raw(rate_dollar=None, rate_algorithm="per contract"), CONTEXT)

        assert isinstance(percent, CuratedRate) and percent.rate_kind == "percentage"
        assert isinstance(algo, CuratedRate) and algo.rate_kind == "algorithm"

    def test_payer_and_plan_keys_are_normalised(self):
        result = curate(raw(payer_name="AETNA [2700]", plan_name="Essential 1&amp;2"), CONTEXT)

        assert isinstance(result, CuratedRate)
        assert result.payer_key == "aetna"
        assert result.plan_key == "essential 1 2"
        assert result.payer_name_raw == "AETNA [2700]"


class TestRejects:
    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"payer_name": None}, Reason.MISSING_PAYER),
            ({"code": None}, Reason.MISSING_CODE),
            ({"rate_dollar": None}, Reason.NO_RATE_VALUE),
            ({"rate_dollar": "n/a"}, Reason.NON_NUMERIC_RATE),
            ({"rate_dollar": "-5"}, Reason.NEGATIVE_RATE),
            ({"rate_dollar": "99999999999"}, Reason.IMPLAUSIBLE_RATE),
            ({"rate_dollar": None, "rate_percentage": "5000"}, Reason.PERCENTAGE_OUT_OF_RANGE),
            ({"payer_name": "Self-Pay", "plan_name": "Self-Pay"}, Reason.NON_PAYER_ROW),
        ],
    )
    def test_reason_codes(self, overrides, reason):
        result = curate(raw(**overrides), CONTEXT)

        assert isinstance(result, Reject)
        assert result.reason == str(reason)
        assert result.ordinal == 1

    def test_file_without_vintage_is_rejected(self):
        """Vintage mismatch is a structural hazard; an undated file is unusable."""
        context = CurateContext("b", "u", "H", FileMeta(hospital_name="X"))

        result = curate(raw(), context)

        assert isinstance(result, Reject)
        assert result.reason == str(Reason.MISSING_VINTAGE)

    def test_nothing_raises_on_hostile_input(self):
        for value in ("", "  ", "abc", "1e999", "NaN", "--5", "$"):
            result = curate(raw(rate_dollar=value), CONTEXT)
            assert isinstance(result, CuratedRate | Reject)


class TestRowHash:
    def test_identical_rows_hash_identically(self):
        assert row_hash("u", raw()) == row_hash("u", raw())

    def test_rate_change_changes_the_hash(self):
        assert row_hash("u", raw()) != row_hash("u", raw(rate_dollar="500.00"))

    def test_source_url_is_part_of_identity(self):
        assert row_hash("u1", raw()) != row_hash("u2", raw())
