"""Validate raw rates into curated rows, quarantining anything that fails.

Rule 4 of the architecture: quarantine, never hard-fail. Every rejected row
carries a reason code so the reject rate can be monitored per code and per
hospital, and so a spike is diagnosable rather than just alarming.

Nothing here raises. A row is either curated or rejected.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from hospital.parser import FileMeta, RawRate
from hospital.profile import classify_product, normalise_name


class Reason(StrEnum):
    """Reject reason codes. Stable strings -- they are grouped and alerted on."""

    MISSING_CODE = "missing_code"
    MISSING_PAYER = "missing_payer"
    NO_RATE_VALUE = "no_rate_value"
    NON_NUMERIC_RATE = "non_numeric_rate"
    NEGATIVE_RATE = "negative_rate"
    IMPLAUSIBLE_RATE = "implausible_rate"
    PERCENTAGE_OUT_OF_RANGE = "percentage_out_of_range"
    NON_PAYER_ROW = "non_payer_row"
    MISSING_VINTAGE = "missing_vintage"


#: A negotiated rate above this is treated as a data error rather than a price.
#: Chosen well above any plausible DRG case rate so it only catches unit errors.
MAX_PLAUSIBLE_DOLLAR = Decimal("10000000")

_CURRENCY = re.compile(r"[$,\s]")


@dataclass(frozen=True)
class CuratedRate:
    batch_id: str
    source_url: str
    hospital: str
    location_name: str | None
    file_vintage: str | None
    code: str
    code_type: str | None
    description: str | None
    setting: str | None
    billing_class: str | None
    payer_name_raw: str
    plan_name_raw: str | None
    payer_key: str
    plan_key: str | None
    product_class: str
    rate_kind: str
    rate_dollar: float | None
    rate_percentage: float | None
    rate_algorithm: str | None
    methodology: str | None
    gross_charge: float | None
    discounted_cash: float | None
    row_hash: str


@dataclass(frozen=True)
class Reject:
    batch_id: str
    source_url: str
    ordinal: int
    reason: str
    detail: str
    payer_name: str | None
    code: str | None


@dataclass(frozen=True)
class CurateContext:
    batch_id: str
    source_url: str
    hospital: str
    meta: FileMeta


def curate(raw: RawRate, context: CurateContext) -> CuratedRate | Reject:
    """Turn one raw row into a curated rate or a quarantined reject."""
    reject = _reject_for(raw, context)
    if reject is not None:
        return reject

    dollar = _to_decimal(raw.rate_dollar)
    percentage = _to_decimal(raw.rate_percentage)
    kind = (
        "dollar" if dollar is not None else "percentage" if percentage is not None else "algorithm"
    )

    payer_key = normalise_name(raw.payer_name).casefold()
    plan_key = normalise_name(raw.plan_name).casefold() or None
    assert raw.code is not None and raw.payer_name is not None  # guaranteed by _reject_for

    return CuratedRate(
        batch_id=context.batch_id,
        source_url=context.source_url,
        hospital=context.hospital,
        location_name=context.meta.location_name,
        file_vintage=context.meta.last_updated_on,
        code=raw.code,
        code_type=raw.code_type,
        description=raw.description,
        setting=raw.setting,
        billing_class=raw.billing_class,
        payer_name_raw=raw.payer_name,
        plan_name_raw=raw.plan_name,
        payer_key=payer_key,
        plan_key=plan_key,
        product_class=classify_product(raw.payer_name, raw.plan_name),
        rate_kind=kind,
        rate_dollar=float(dollar) if dollar is not None else None,
        rate_percentage=float(percentage) if percentage is not None else None,
        rate_algorithm=raw.rate_algorithm,
        methodology=raw.methodology,
        gross_charge=_as_float(raw.gross_charge),
        discounted_cash=_as_float(raw.discounted_cash),
        row_hash=row_hash(context.source_url, raw),
    )


def _reject_for(raw: RawRate, context: CurateContext) -> Reject | None:
    def rejected(reason: Reason, detail: str) -> Reject:
        return Reject(
            batch_id=context.batch_id,
            source_url=context.source_url,
            ordinal=raw.ordinal,
            reason=str(reason),
            detail=detail[:200],
            payer_name=raw.payer_name,
            code=raw.code,
        )

    if not raw.payer_name:
        return rejected(Reason.MISSING_PAYER, "no payer_name on the row")
    if classify_product(raw.payer_name, raw.plan_name) == "non_payer":
        # Self-pay and uninsured columns are standard charges, not negotiated
        # rates; keeping them would inflate every payer comparison.
        return rejected(Reason.NON_PAYER_ROW, f"{raw.payer_name} / {raw.plan_name}")
    if not raw.code:
        return rejected(Reason.MISSING_CODE, "no billing code on the row")
    if not context.meta.has_vintage:
        return rejected(Reason.MISSING_VINTAGE, "file has no last_updated_on")

    has_any = any((raw.rate_dollar, raw.rate_percentage, raw.rate_algorithm))
    if not has_any:
        return rejected(Reason.NO_RATE_VALUE, "no dollar, percentage or algorithm")

    if raw.rate_dollar:
        value = _to_decimal(raw.rate_dollar)
        if value is None:
            return rejected(Reason.NON_NUMERIC_RATE, f"dollar={raw.rate_dollar!r}")
        if value < 0:
            return rejected(Reason.NEGATIVE_RATE, f"dollar={raw.rate_dollar!r}")
        if value > MAX_PLAUSIBLE_DOLLAR:
            return rejected(Reason.IMPLAUSIBLE_RATE, f"dollar={raw.rate_dollar!r}")

    if raw.rate_percentage:
        value = _to_decimal(raw.rate_percentage)
        if value is None:
            return rejected(Reason.NON_NUMERIC_RATE, f"percentage={raw.rate_percentage!r}")
        if not 0 <= value <= 1000:
            return rejected(Reason.PERCENTAGE_OUT_OF_RANGE, f"percentage={raw.rate_percentage!r}")

    return None


def row_hash(source_url: str, raw: RawRate) -> str:
    """Stable identity for a rate line, so reloads are idempotent."""
    parts = [
        source_url,
        raw.code or "",
        raw.code_type or "",
        raw.setting or "",
        raw.billing_class or "",
        raw.payer_name or "",
        raw.plan_name or "",
        raw.rate_dollar or "",
        raw.rate_percentage or "",
        raw.rate_algorithm or "",
    ]
    return hashlib.sha256("␟".join(parts).encode("utf-8")).hexdigest()[:32]


def _to_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    cleaned = _CURRENCY.sub("", value).replace("%", "")
    if not cleaned:
        return None
    try:
        parsed = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    # Decimal happily parses "NaN" and "Infinity", then raises on comparison.
    # Treat both as non-numeric so they quarantine instead of exploding.
    return parsed if parsed.is_finite() else None


def _as_float(value: str | None) -> float | None:
    parsed = _to_decimal(value)
    return float(parsed) if parsed is not None else None
