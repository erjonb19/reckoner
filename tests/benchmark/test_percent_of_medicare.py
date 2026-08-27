import pytest

from benchmark.models import (
    BenchmarkMatch,
    BenchmarkMiss,
    BenchmarkRate,
    HospitalGeography,
    IppsParameters,
    MatchFailure,
)
from benchmark.percent_of_medicare import (
    BenchmarkIndex,
    RateToBenchmark,
    to_percent_of_medicare,
)

YEAR = 2026

# Illustrative parameters. Real values come from the IPPS final rule; the type
# has no defaults precisely so a placeholder cannot leak into a reported figure.
IPPS = IppsParameters(
    fiscal_year=YEAR,
    labor_share=0.677,
    nonlabor_share=0.323,
    standardized_amount=6500.0,
    source="test fixture, not a published value",
)

INDEX = BenchmarkIndex(
    [
        BenchmarkRate(schedule="OPPS", code="70450", code_type="HCPCS", year=YEAR, amount=200.0),
        BenchmarkRate(schedule="IPPS", code="470", code_type="MS-DRG", year=YEAR, weight=2.0),
        BenchmarkRate(schedule="OPPS", code="99999", code_type="HCPCS", year=YEAR, amount=0.0),
    ]
)

NYC = HospitalGeography(hospital="Example", cbsa="35614", wage_index=1.0)


def rate(**overrides: object) -> RateToBenchmark:
    base = {
        "hospital": "Example",
        "code": "70450",
        "code_type": "HCPCS",
        "setting": "outpatient",
        "rate_kind": "dollar",
        "rate_dollar": 300.0,
    }
    return RateToBenchmark(**{**base, **overrides})


class TestMatches:
    def test_opps_percent_of_medicare(self):
        result = to_percent_of_medicare(rate(), INDEX, NYC, YEAR, IPPS)

        assert isinstance(result, BenchmarkMatch)
        # wage index 1.0 leaves the national rate unchanged: 300 / 200
        assert result.benchmark_amount == pytest.approx(200.0)
        assert result.percent_of_medicare == pytest.approx(1.5)
        assert result.schedule == "OPPS"

    def test_opps_rate_is_wage_adjusted(self):
        rich = HospitalGeography(hospital="Example", cbsa="35614", wage_index=1.3)

        result = to_percent_of_medicare(rate(), INDEX, rich, YEAR, IPPS)

        assert isinstance(result, BenchmarkMatch)
        # 200 * (0.6*1.3 + 0.4) = 236.0
        assert result.benchmark_amount == pytest.approx(236.0)
        assert result.percent_of_medicare == pytest.approx(300 / 236.0, rel=1e-4)

    def test_inpatient_weight_is_priced_through_the_wage_index(self):
        drg = rate(code="470", code_type="MS-DRG", setting="inpatient", rate_dollar=20000.0)

        result = to_percent_of_medicare(drg, INDEX, NYC, YEAR, IPPS)

        assert isinstance(result, BenchmarkMatch)
        # 2.0 * 6500 * (0.677*1.0 + 0.323) = 13000
        assert result.benchmark_amount == pytest.approx(13000.0)
        assert result.percent_of_medicare == pytest.approx(20000 / 13000, rel=1e-4)

    def test_wage_index_changes_the_inpatient_benchmark(self):
        drg = rate(code="470", code_type="MS-DRG", setting="inpatient", rate_dollar=20000.0)
        low = HospitalGeography(hospital="Example", cbsa="45060", wage_index=0.85)

        rich_result = to_percent_of_medicare(drg, INDEX, NYC, YEAR, IPPS)
        low_result = to_percent_of_medicare(drg, INDEX, low, YEAR, IPPS)

        assert isinstance(rich_result, BenchmarkMatch)
        assert isinstance(low_result, BenchmarkMatch)
        assert low_result.benchmark_amount < rich_result.benchmark_amount
        assert low_result.percent_of_medicare > rich_result.percent_of_medicare

    def test_zero_padded_drg_codes_still_match(self):
        drg = rate(code="0470", code_type="MS-DRG", setting="inpatient", rate_dollar=20000.0)

        assert isinstance(to_percent_of_medicare(drg, INDEX, NYC, YEAR, IPPS), BenchmarkMatch)


class TestMisses:
    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"rate_kind": "algorithm", "rate_dollar": None}, MatchFailure.NOT_DOLLAR_DENOMINATED),
            ({"rate_kind": "percentage", "rate_dollar": None}, MatchFailure.NOT_DOLLAR_DENOMINATED),
            ({"code": None}, MatchFailure.NO_BENCHMARK_FOR_CODE),
            ({"code_type": "APR-DRG"}, MatchFailure.UNSUPPORTED_CODE_TYPE),
            ({"code_type": "REVENUE"}, MatchFailure.UNSUPPORTED_CODE_TYPE),
            ({"code": "00000"}, MatchFailure.NO_BENCHMARK_FOR_CODE),
            ({"code": "99999"}, MatchFailure.ZERO_BENCHMARK),
        ],
    )
    def test_reason_codes(self, overrides, reason):
        result = to_percent_of_medicare(rate(**overrides), INDEX, NYC, YEAR, IPPS)

        assert isinstance(result, BenchmarkMiss)
        assert result.reason == str(reason)

    def test_inpatient_without_geography_is_a_reason_not_a_null(self):
        drg = rate(code="470", code_type="MS-DRG", setting="inpatient", rate_dollar=20000.0)

        result = to_percent_of_medicare(drg, INDEX, None, YEAR, IPPS)

        assert isinstance(result, BenchmarkMiss)
        assert result.reason == str(MatchFailure.NO_GEOGRAPHY_FOR_HOSPITAL)

    def test_inpatient_without_a_wage_index_is_reported(self):
        drg = rate(code="470", code_type="MS-DRG", setting="inpatient", rate_dollar=20000.0)
        unknown = HospitalGeography(hospital="Example", cbsa="99999", wage_index=None)

        result = to_percent_of_medicare(drg, INDEX, unknown, YEAR, IPPS)

        assert isinstance(result, BenchmarkMiss)
        assert result.reason == str(MatchFailure.NO_WAGE_INDEX_FOR_AREA)

    def test_wrong_benchmark_year_does_not_silently_fall_back(self):
        """A 2026 rate must not be priced against a 2024 schedule by accident."""
        result = to_percent_of_medicare(rate(), INDEX, NYC, 2024, IPPS)

        assert isinstance(result, BenchmarkMiss)
        assert result.reason == str(MatchFailure.NO_BENCHMARK_FOR_CODE)


class TestIppsParameters:
    def test_payment_formula(self):
        assert IPPS.payment(weight=1.0, wage_index=1.0) == pytest.approx(6500.0)
        assert IPPS.payment(weight=0.5, wage_index=1.0) == pytest.approx(3250.0)

    def test_parameters_require_an_explicit_source(self):
        """No defaults: a stale standardized amount rescales every comparison."""
        with pytest.raises(TypeError):
            IppsParameters(fiscal_year=2026)  # type: ignore[call-arg]
