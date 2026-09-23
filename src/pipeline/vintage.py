"""How far apart in time the two sides of each comparison are.

CLAUDE.md calls vintage mismatch structural: hospital files update at least
annually, payer files monthly, so a variance may be a timing artifact rather
than a disagreement. The mart already refuses a pair beyond ``max_vintage_days``
and explains one as a vintage artifact when drift over the gap could account for
the size.

What was missing is the distribution. "Some pairs are refused for vintage" is
not an answer to "how well aligned are these two sources": a median gap of nine
days and a median gap of nine months are the same sentence about very different
projects.

**The gap is computed from the pairs that formed.** It therefore describes the
comparison rather than the lake -- a file whose rates were all refused for some
other reason contributes nothing here, which is right, because its alignment did
not affect any result.
"""

from __future__ import annotations

from datetime import date
from statistics import median
from typing import Any

#: The mart's own limit. A pair beyond this is refused rather than compared, so
#: it lands in the refusal counts and not in this report -- which is why the
#: report carries the limit rather than assuming a reader knows it.
MAX_VINTAGE_DAYS = 400


def _as_date(value: object) -> date | None:
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def gap_days(row: dict[str, Any]) -> int | None:
    """Days between the hospital and payer vintages, or ``None`` if unknown.

    Unknown rather than zero. A missing vintage on one side is the *least*
    aligned a pair can be, and scoring it as same-day would put it at the top
    of any table sorted by alignment.
    """
    left = _as_date(row.get("hospital_vintage"))
    right = _as_date(row.get("payer_vintage"))
    if left is None or right is None:
        return None
    return abs((right - left).days)


def _percentile(ordered: list[int], q: float) -> int | str:
    if not ordered:
        return ""
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def alignment(
    rows: list[dict[str, Any]], *, max_vintage_days: int = MAX_VINTAGE_DAYS
) -> list[dict[str, Any]]:
    """One row per hospital and carrier: how far apart, and how many are beyond.

    ``beyond_limit`` will normally be zero, and that is the point rather than a
    redundancy: the mart refuses those pairs before they reach gold. A non-zero
    count means something got through that the comparability layer should have
    stopped, which is worth seeing the moment it happens.
    """
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            str(row.get("hospital_slug") or ""),
            str(row.get("system") or ""),
            str(row.get("carrier") or ""),
        )
        grouped.setdefault(key, []).append(row)

    out: list[dict[str, Any]] = []
    for (slug, system, carrier), members in sorted(grouped.items()):
        gaps = [g for g in (gap_days(r) for r in members) if g is not None]
        ordered = sorted(gaps)
        out.append(
            {
                "hospital_slug": slug,
                "system": system,
                "carrier": carrier,
                "pairs": len(members),
                "median_gap_days": int(median(ordered)) if ordered else "",
                "min_gap_days": ordered[0] if ordered else "",
                "p90_gap_days": _percentile(ordered, 0.9),
                "max_gap_days": ordered[-1] if ordered else "",
                # Neither aligned nor misaligned: unmeasured. Counted rather
                # than folded into a median that would then be a lie.
                "unknown_vintage_pairs": len(members) - len(gaps),
                "beyond_limit": sum(1 for g in ordered if g > max_vintage_days),
                "max_vintage_days": max_vintage_days,
            }
        )
    return out


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """One line for the report: the headline alignment across everything."""
    gaps = [g for g in (gap_days(r) for r in rows) if g is not None]
    ordered = sorted(gaps)
    return {
        "pairs": len(rows),
        "median_gap_days": int(median(ordered)) if ordered else None,
        "p90_gap_days": _percentile(ordered, 0.9) if ordered else None,
        "max_gap_days": ordered[-1] if ordered else None,
        "unknown_vintage_pairs": len(rows) - len(gaps),
        "beyond_limit": sum(1 for g in ordered if g > MAX_VINTAGE_DAYS),
    }


__all__ = ["MAX_VINTAGE_DAYS", "alignment", "gap_days", "summarise"]
