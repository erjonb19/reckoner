"""Express a negotiated rate as a percent of Medicare.

Every failure path returns a BenchmarkMiss with a reason code rather than a
null, so the share of rates that *cannot* be benchmarked is itself a reported
number. That share is part of the finding: if a third of inpatient rates cannot
be priced against Medicare, a chart of the other two thirds is misleading.
"""

from __future__ import annotations

from dataclasses import dataclass

from benchmark.models import (
    CODE_TYPE_SCHEDULE,
    BenchmarkMatch,
    BenchmarkMiss,
    BenchmarkRate,
    HospitalGeography,
    IppsParameters,
    MatchFailure,
    Schedule,
)


@dataclass(frozen=True)
class RateToBenchmark:
    """The subset of a curated rate that benchmarking needs."""

    hospital: str
    code: str | None
    code_type: str | None
    setting: str | None
    rate_kind: str
    rate_dollar: float | None
    file_vintage: str | None = None


class BenchmarkIndex:
    """Lookup of benchmark rates by (schedule, code_type, code, year)."""

    def __init__(self, rates: list[BenchmarkRate]) -> None:
        self._by_key: dict[tuple[str, str, int], BenchmarkRate] = {}
        for rate in rates:
            self._by_key[(rate.schedule, _normalise_code(rate.code), rate.year)] = rate
        self.years = sorted({rate.year for rate in rates})

    def get(self, schedule: str, code: str, year: int) -> BenchmarkRate | None:
        return self._by_key.get((schedule, _normalise_code(code), year))

    def latest_year(self) -> int | None:
        return self.years[-1] if self.years else None

    def __len__(self) -> int:
        return len(self._by_key)


def to_percent_of_medicare(
    rate: RateToBenchmark,
    index: BenchmarkIndex,
    geography: HospitalGeography | None,
    year: int,
    ipps: IppsParameters | None = None,
) -> BenchmarkMatch | BenchmarkMiss:
    """Price one negotiated rate against the Medicare benchmark."""
    if rate.rate_kind != "dollar" or rate.rate_dollar is None:
        # A percentage-of-charges or algorithm rate has no dollar to compare.
        return _miss(rate, MatchFailure.NOT_DOLLAR_DENOMINATED, rate.rate_kind)
    if not rate.code or not rate.code_type:
        return _miss(rate, MatchFailure.NO_BENCHMARK_FOR_CODE, "row has no code")

    schedule = CODE_TYPE_SCHEDULE.get(rate.code_type.strip().upper())
    if schedule is None:
        return _miss(rate, MatchFailure.UNSUPPORTED_CODE_TYPE, rate.code_type)

    benchmark = index.get(str(schedule), rate.code, year)
    if benchmark is None:
        return _miss(rate, MatchFailure.NO_BENCHMARK_FOR_CODE, f"{schedule} {rate.code} for {year}")

    amount = benchmark.amount
    wage_index = geography.wage_index if geography else None

    if benchmark.is_weighted:
        # IPPS publishes a weight; the price is hospital-specific.
        if geography is None:
            return _miss(rate, MatchFailure.NO_GEOGRAPHY_FOR_HOSPITAL, rate.hospital)
        if wage_index is None:
            return _miss(rate, MatchFailure.NO_WAGE_INDEX_FOR_AREA, geography.cbsa or "?")
        if ipps is None:
            return _miss(rate, MatchFailure.NO_BENCHMARK_FOR_CODE, "no IPPS parameters supplied")
        assert benchmark.weight is not None
        amount = ipps.payment(benchmark.weight, wage_index)
    elif amount is not None and wage_index is not None and schedule is Schedule.OPPS:
        # OPPS national rate, wage-adjusted on its labor share.
        amount = amount * (0.6 * wage_index + 0.4)

    if amount is None:
        return _miss(rate, MatchFailure.NO_BENCHMARK_FOR_CODE, "benchmark has no price or weight")
    if amount <= 0:
        return _miss(rate, MatchFailure.ZERO_BENCHMARK, f"benchmark={amount}")

    return BenchmarkMatch(
        code=rate.code,
        code_type=rate.code_type,
        schedule=str(schedule),
        setting=rate.setting,
        benchmark_year=year,
        benchmark_amount=round(amount, 2),
        negotiated_dollar=rate.rate_dollar,
        percent_of_medicare=round(rate.rate_dollar / amount, 4),
        wage_index=wage_index,
        cbsa=geography.cbsa if geography else None,
    )


def _miss(rate: RateToBenchmark, reason: MatchFailure, detail: str = "") -> BenchmarkMiss:
    return BenchmarkMiss(
        code=rate.code, code_type=rate.code_type, reason=str(reason), detail=detail[:120]
    )


def _normalise_code(code: str) -> str:
    """MS-DRGs are zero-padded to three digits in some files and not others."""
    stripped = code.strip().upper()
    return stripped.lstrip("0") or "0" if stripped.isdigit() else stripped
