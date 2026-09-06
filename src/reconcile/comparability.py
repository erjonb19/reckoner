"""Whether two published rates may be compared at all.

CLAUDE.md names methodology heterogeneity as the fastest way to discredit this
work, and the reason is that the files invite the mistake: a negotiated dollar,
a percentage of charges, a per diem and a case rate all sit in the same column
family and all look like numbers. Subtracting one from another produces a
variance that is arithmetically correct and means nothing.

So comparability is a decision made explicitly, before any subtraction, and
every refusal carries a reason code. The refusals are not failures to be
minimised -- the share of rate pairs that *cannot* be compared, and why, is one
of the findings this project exists to produce.

Three classes of obstacle, in the order they are cheapest to test:

1. **Structural.** Different code, setting or billing class. Not the same
   service, so not a comparison.
2. **Methodological.** Different rate kind or contracting methodology. The same
   service, priced in units that do not convert.
3. **Temporal.** Vintages far enough apart that a difference is at least partly
   a timing artifact. This one can never be engineered away, only surfaced.

Two further obstacles apply only across sources, because they come from the two
rules disclosing different things:

4. **Scope.** Medicare Advantage and Medicaid rates appear on the hospital side
   and are exempt from Transparency in Coverage, so they have no payer-side
   counterpart to disagree with. Reporting them as an unexplained variance would
   be a bug, not a finding.
5. **Unstated billing class.** A payer file always says whether a rate is
   professional or institutional; a hospital file often does not. A missing
   value is compatible with everything, so across sources it silently pairs one
   hospital rate with both of the payer's -- the facility charge for a scan
   against the fee for reading it. That is refused rather than assumed away.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from hospital.profile import HOSPITAL_ONLY_CLASSES
from reconcile.provenance import COMPARABLE_VINTAGE_DAYS, parse_vintage


class NotComparable(StrEnum):
    """Why a pair of rates was excluded from the variance mart."""

    DIFFERENT_CODE = "different_code"
    DIFFERENT_CODE_TYPE = "different_code_type"
    DIFFERENT_SETTING = "different_setting"
    DIFFERENT_BILLING_CLASS = "different_billing_class"
    BILLING_CLASS_UNSTATED = "billing_class_unstated"
    NOT_DOLLAR_DENOMINATED = "not_dollar_denominated"
    MIXED_RATE_KIND = "mixed_rate_kind"
    INCOMPATIBLE_METHODOLOGY = "incompatible_methodology"
    VINTAGE_TOO_FAR_APART = "vintage_too_far_apart"
    TIC_EXEMPT_PRODUCT = "tic_exempt_product"
    MISSING_RATE = "missing_rate"
    ZERO_RATE = "zero_rate"


#: Contracting methodologies, normalised from the free text hospitals publish.
#: Two rates are comparable only within a family: a case rate and a per diem for
#: the same DRG are both "what the hospital is paid" but one is per stay and the
#: other per day, and the ratio between them is length of stay, not price.
#: ``_S`` stands in for the separator between words, because hospitals write
#: "per diem", "per-diem" and "perdiem" for the same methodology and a pattern
#: that only tolerates whitespace silently classifies two of the three as
#: unknown -- which then makes them comparable with a case rate.
_S = r"[\s_-]*"

METHODOLOGY_FAMILIES: tuple[tuple[str, str], ...] = (
    ("per_diem", rf"per{_S}diem|\bday{_S}rate\b"),
    ("case_rate", rf"case{_S}rate|per{_S}case|\bdrg\b|bundled|global"),
    ("percent_of_charges", rf"percent.*charge|%{_S}of{_S}charge|\bpoc\b|discount.*charge"),
    ("fee_schedule", rf"fee{_S}schedule|fee{_S}for{_S}service|\bffs\b|contract.*rate"),
    ("percent_of_medicare", rf"percent.*medicare|%{_S}of{_S}medicare|medicare{_S}(rate|based)"),
    ("capitation", r"capitat|\bpmpm\b"),
)

_COMPILED_METHODOLOGY = tuple(
    (label, re.compile(pattern, re.I)) for label, pattern in METHODOLOGY_FAMILIES
)

#: Methodologies that produce a per-stay or per-service dollar figure, and so
#: can be compared with each other. Per diem and capitation are excluded: they
#: are denominated in days and members, not services.
DOLLAR_COMPARABLE_METHODOLOGIES = frozenset(
    {"case_rate", "fee_schedule", "percent_of_medicare", "unknown"}
)


def classify_methodology(value: str | None) -> str:
    """Normalise a free-text methodology field to a family.

    Returns ``unknown`` rather than guessing. An unknown methodology is treated
    as comparable -- most files leave the field blank for ordinary fee-schedule
    rates -- but the pair carries the fact so a reader can exclude it.
    """
    text = (value or "").strip()
    if not text:
        return "unknown"
    for label, pattern in _COMPILED_METHODOLOGY:
        if pattern.search(text):
            return label
    return "unknown"


@dataclass(frozen=True)
class ComparableRate:
    """The fields a comparison depends on, from either side of the disclosure."""

    source: str
    hospital: str
    code: str
    code_type: str | None = None
    setting: str | None = None
    billing_class: str | None = None
    payer: str = ""
    plan: str | None = None
    product_class: str = ""
    rate_kind: str = "dollar"
    rate_dollar: float | None = None
    methodology: str | None = None
    vintage: str | None = None
    location: str | None = None

    @property
    def methodology_family(self) -> str:
        return classify_methodology(self.methodology)


@dataclass(frozen=True)
class Comparability:
    """The verdict on one pair, with the reason when the answer is no."""

    ok: bool
    reason: str = ""
    detail: str = ""

    def __bool__(self) -> bool:
        return self.ok


COMPARABLE = Comparability(True)


def _no(reason: NotComparable, detail: str = "") -> Comparability:
    return Comparability(False, str(reason), detail[:160])


def can_compare(
    left: ComparableRate,
    right: ComparableRate,
    *,
    cross_source: bool = False,
    max_vintage_days: int = COMPARABLE_VINTAGE_DAYS,
    require_same_setting: bool = True,
) -> Comparability:
    """Decide whether two rates may be differenced.

    ``cross_source`` switches on the checks that only apply when comparing a
    hospital disclosure against a payer one -- principally that Medicare
    Advantage and Medicaid products have no payer-side counterpart by rule.
    """
    structural = _structural(left, right, require_same_setting=require_same_setting)
    if not structural:
        return structural

    if cross_source:
        exempt = _tic_exempt(left) or _tic_exempt(right)
        if exempt:
            return _no(
                NotComparable.TIC_EXEMPT_PRODUCT,
                f"{exempt} is exempt from Transparency in Coverage; "
                "it exists only in hospital-side files",
            )

        unstated = _billing_class_unstated(left, right)
        if unstated:
            return _no(NotComparable.BILLING_CLASS_UNSTATED, unstated)

    methodological = _methodological(left, right)
    if not methodological:
        return methodological

    return _temporal(left, right, max_vintage_days=max_vintage_days)


def _structural(
    left: ComparableRate, right: ComparableRate, *, require_same_setting: bool
) -> Comparability:
    if _normalise_code(left.code) != _normalise_code(right.code):
        return _no(NotComparable.DIFFERENT_CODE, f"{left.code} vs {right.code}")

    left_family, right_family = _code_family(left.code_type), _code_family(right.code_type)
    if left_family and right_family and left_family != right_family:
        # Revenue code 0470 and MS-DRG 470 are the same digits and different
        # things; without this the join would silently merge them.
        return _no(NotComparable.DIFFERENT_CODE_TYPE, f"{left.code_type} vs {right.code_type}")

    if require_same_setting and _differs(left.setting, right.setting):
        return _no(NotComparable.DIFFERENT_SETTING, f"{left.setting} vs {right.setting}")

    if _differs(left.billing_class, right.billing_class):
        return _no(
            NotComparable.DIFFERENT_BILLING_CLASS,
            f"{left.billing_class} vs {right.billing_class}",
        )
    return COMPARABLE


def _methodological(left: ComparableRate, right: ComparableRate) -> Comparability:
    if left.rate_kind != "dollar" or right.rate_kind != "dollar":
        if left.rate_kind != right.rate_kind:
            return _no(NotComparable.MIXED_RATE_KIND, f"{left.rate_kind} vs {right.rate_kind}")
        return _no(NotComparable.NOT_DOLLAR_DENOMINATED, left.rate_kind)

    if left.rate_dollar is None or right.rate_dollar is None:
        return _no(NotComparable.MISSING_RATE, "a side has no dollar amount")
    if left.rate_dollar <= 0 or right.rate_dollar <= 0:
        # A zero rate is a placeholder, not a price. Dividing by it produces an
        # infinite variance that dominates every summary it lands in.
        return _no(NotComparable.ZERO_RATE, f"{left.rate_dollar} vs {right.rate_dollar}")

    families = {left.methodology_family, right.methodology_family}
    if not families <= DOLLAR_COMPARABLE_METHODOLOGIES:
        return _no(
            NotComparable.INCOMPATIBLE_METHODOLOGY,
            f"{left.methodology_family} vs {right.methodology_family}",
        )
    return COMPARABLE


def _temporal(
    left: ComparableRate, right: ComparableRate, *, max_vintage_days: int
) -> Comparability:
    left_date, right_date = parse_vintage(left.vintage), parse_vintage(right.vintage)
    if left_date is None or right_date is None:
        # An unknown vintage is not a refusal -- it is a caveat the provenance
        # carries -- because refusing would drop every file with a blank field.
        return COMPARABLE
    span = abs((left_date - right_date).days)
    if span > max_vintage_days:
        return _no(
            NotComparable.VINTAGE_TOO_FAR_APART,
            f"{span} days apart ({left.vintage} vs {right.vintage})",
        )
    return COMPARABLE


def _tic_exempt(rate: ComparableRate) -> str:
    return rate.product_class if rate.product_class in HOSPITAL_ONLY_CLASSES else ""


def _billing_class_unstated(left: ComparableRate, right: ComparableRate) -> str:
    """Refuse a cross-source pair where either side omits its billing class.

    A payer file always says ``professional`` or ``institutional``. A hospital
    file often says nothing -- NYU Langone states it on none of its rows -- and
    :func:`_differs` treats a missing value as compatible with anything. Across
    sources that is not a harmless default, it is a silent cross-join: one
    unstated hospital rate meets both the payer's professional and its
    institutional rate for the same code.

    Measured on one NYU facility, 96.4% of pairs formed that way put an unstated
    hospital rate against a payer *professional* rate -- the hospital's charge
    for a scan against the radiologist's fee for reading it -- and those were six
    times more likely to land ten-fold apart than the facility-to-facility pairs
    (22.7% against 3.8%). They were reaching the mart as
    ``entity_resolution_suspect``, which reads as a finding and is arithmetic on
    two different things.

    Refusing is deliberately expensive: it removes most of the comparable volume
    for any hospital that omits the field. That is the correct trade. The share
    of a disclosure that cannot be compared *because the hospital did not say
    what kind of charge it published* is a result this project exists to report,
    and it only counts as one if it is counted rather than papered over with an
    assumption about what the hospital probably meant.

    Same-source comparisons are unaffected: two hospitals that both omit the
    field are omitting it the same way.
    """
    missing = [rate.source or "a side" for rate in (left, right) if not rate.billing_class]
    if not missing:
        return ""
    return (
        f"{' and '.join(missing)} did not state a billing class; "
        "professional and facility rates for one code are different services"
    )


def _differs(left: str | None, right: str | None) -> bool:
    """True only when both sides state a value and the values disagree."""
    if not left or not right:
        return False
    return left.strip().casefold() != right.strip().casefold()


_REVENUE_TYPES = frozenset({"RC", "REV", "REVENUE", "REVCODE"})
_PROCEDURE_TYPES = frozenset({"CPT", "HCPCS", "APC", "EAPG"})
_DRG_TYPES = frozenset({"MS-DRG", "MSDRG", "DRG", "APR-DRG", "APRDRG", "TRIS-DRG"})


def _code_family(code_type: str | None) -> str | None:
    normalised = (code_type or "").strip().upper().replace("_", "-")
    if normalised in _REVENUE_TYPES:
        return "revenue"
    if normalised in _PROCEDURE_TYPES:
        return "procedure"
    if normalised in _DRG_TYPES:
        return "drg"
    return None


def _normalise_code(code: str | None) -> str:
    """Codes compare case-insensitively, with numeric codes unpadded."""
    stripped = (code or "").strip().upper()
    return (stripped.lstrip("0") or "0") if stripped.isdigit() else stripped
