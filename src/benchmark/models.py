"""Medicare benchmark rates -- the denominator for percent-of-Medicare.

Percent-of-Medicare is the unit contracting teams actually use, and getting it
right is not a lookup. A benchmark must match on fee schedule, setting, code
type, geographic locality and year. A miss on any of those is a *reason*, not a
null: an unexplained blank is how a comparison quietly becomes wrong.

Three schedules, and they behave differently:

* **IPPS** (inpatient, MS-DRG) publishes a relative weight, not a price. The
  payment is derived from the weight, the hospital's wage index and the national
  standardized amounts, so it varies by hospital.
* **OPPS** (outpatient, APC) publishes a national payment rate adjusted by the
  same wage index.
* **PFS** (professional, CPT/HCPCS) publishes RVUs priced by locality-specific
  GPCIs and a national conversion factor.

Licensing note: MS-DRG weights and HCPCS Level II are CMS-owned and freely
downloadable. The PFS RVU files are keyed on CPT, which is AMA-licensed, and CMS
puts them behind a licence click-through -- see loaders.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Schedule(StrEnum):
    IPPS = "IPPS"
    OPPS = "OPPS"
    PFS = "PFS"


class Setting(StrEnum):
    INPATIENT = "inpatient"
    OUTPATIENT = "outpatient"
    PROFESSIONAL = "professional"


class MatchFailure(StrEnum):
    """Why a rate could not be expressed as a percent of Medicare."""

    NOT_DOLLAR_DENOMINATED = "not_dollar_denominated"
    UNSUPPORTED_CODE_TYPE = "unsupported_code_type"
    NO_BENCHMARK_FOR_CODE = "no_benchmark_for_code"
    NO_GEOGRAPHY_FOR_HOSPITAL = "no_geography_for_hospital"
    NO_WAGE_INDEX_FOR_AREA = "no_wage_index_for_area"
    SETTING_UNKNOWN = "setting_unknown"
    VINTAGE_MISMATCH = "vintage_mismatch"
    ZERO_BENCHMARK = "zero_benchmark"


#: Code types we can benchmark, mapped to the schedule that prices them.
CODE_TYPE_SCHEDULE = {
    "MS-DRG": Schedule.IPPS,
    "MSDRG": Schedule.IPPS,
    "DRG": Schedule.IPPS,
    "APR-DRG": None,  # state-specific grouper; Medicare does not price it
    "CPT": Schedule.PFS,
    "HCPCS": Schedule.OPPS,
    "APC": Schedule.OPPS,
}


@dataclass(frozen=True)
class BenchmarkRate:
    """One priced or weighted benchmark row."""

    schedule: str
    code: str
    code_type: str
    year: int
    #: Dollars, where the schedule publishes a directly payable national rate.
    amount: float | None = None
    #: Relative weight, where payment is derived rather than published.
    weight: float | None = None
    description: str | None = None
    source: str | None = None

    @property
    def is_weighted(self) -> bool:
        return self.amount is None and self.weight is not None


@dataclass(frozen=True)
class HospitalGeography:
    """What a hospital needs to price a national benchmark locally."""

    hospital: str
    cbsa: str | None = None
    wage_index: float | None = None
    pfs_locality: str | None = None


@dataclass(frozen=True)
class IppsParameters:
    """National IPPS parameters for a fiscal year.

    Deliberately has no defaults. These are published in the IPPS final rule and
    change every year; a wrong standardized amount silently rescales every
    inpatient comparison, so the caller must supply them from a cited source
    rather than inherit a stale constant.
    """

    fiscal_year: int
    labor_share: float
    nonlabor_share: float
    standardized_amount: float
    source: str

    def payment(self, weight: float, wage_index: float) -> float:
        """Operating payment for one discharge, before add-ons.

        Excludes IME, DSH, outlier and capital payments -- those are
        hospital-specific and not part of a rate comparison.
        """
        adjusted = self.labor_share * wage_index + self.nonlabor_share
        return weight * self.standardized_amount * adjusted


@dataclass(frozen=True)
class BenchmarkMatch:
    """A rate successfully expressed as a percent of Medicare."""

    code: str
    code_type: str
    schedule: str
    setting: str | None
    benchmark_year: int
    benchmark_amount: float
    negotiated_dollar: float
    percent_of_medicare: float
    wage_index: float | None
    cbsa: str | None


@dataclass(frozen=True)
class BenchmarkMiss:
    """A rate that could not be benchmarked, and why."""

    code: str | None
    code_type: str | None
    reason: str
    detail: str = ""
