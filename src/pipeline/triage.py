"""Deterministic triage of the residual: the A1 fallback, and its baseline.

The mart has already removed every finding a deterministic rule could account
for -- a vintage gap wide enough to explain the difference, a plan that
aggregates several, a constant ratio across a contract. What reaches here
survived all of that. So these rules cannot be the same rules again; if they
were, they would fire on nothing and the module would be theatre.

What they are is **near-miss detection**. Each rule uses a threshold
deliberately looser than the mart's, so it catches findings that fell just the
wrong side of a line: a vintage gap that is large but not large enough to
explain the size, a ratio shared by fewer codes than an offset needs. A near
miss is the most triage-worthy thing in the queue, because it is where a
judgement call was made and might have gone the other way.

**This is also the baseline A1 has to beat.** An agent that cannot outperform
five arithmetic rules on a labelled set is not worth its per-call cost, and
without this there would be nothing to compare it against.

Rules are ordered and the first match wins, so a finding gets the most specific
account of itself rather than a list. ``unexplained`` is last and always
matches, because a queue where some rows have no verdict is a queue someone has
to re-derive.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

#: A ratio at or beyond this is more likely a unit or methodology error than a
#: negotiated difference. Same constant the mart flags with, reused rather than
#: re-chosen so the two agree about what implausible means.
IMPLAUSIBLE_RATIO = 10.0

#: The mart explains a variance by vintage only when drift over the gap could
#: actually produce the difference. This fires on a wide gap regardless, because
#: a year apart is worth a human's attention even when the arithmetic did not
#: clear the mart's bar.
WIDE_VINTAGE_GAP_DAYS = 180

#: Just above the 5% materiality line. A finding at 5.2% is material by rule and
#: uninteresting in fact, and it should not sit in a queue next to a 350% gap.
MARGINAL_RELATIVE_DIFFERENCE = 0.08

#: A ratio shared by at least this many codes within one carrier and code type,
#: but by fewer than the 20 distinct codes an offset needs. Between the two is
#: where a real base-rate difference hides from the mart.
NEAR_OFFSET_MIN_CODES = 5
NEAR_OFFSET_TOLERANCE = 0.03


@dataclass(frozen=True)
class Rule:
    """One deterministic account of a residual finding."""

    name: str
    priority: int
    why: str
    fires: Callable[[dict[str, Any], dict[str, Any]], bool]


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _as_date(value: object) -> date | None:
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def vintage_gap_days(row: dict[str, Any]) -> int | None:
    """Days between the two sides' vintages, or ``None`` if either is missing.

    Missing is not zero. A pair with no vintage on one side has an unknown gap,
    and scoring it as same-day would let it look like the best-aligned row in
    the queue.
    """
    left = _as_date(row.get("hospital_vintage"))
    right = _as_date(row.get("payer_vintage"))
    if left is None or right is None:
        return None
    return abs((right - left).days)


def _implausible(row: dict[str, Any], _: dict[str, Any]) -> bool:
    hospital = _as_float(row.get("hospital_rate"))
    payer = _as_float(row.get("payer_rate"))
    low, high = sorted((hospital, payer))
    return bool(row.get("is_implausible")) or (bool(low) and high / low >= IMPLAUSIBLE_RATIO)


def _wide_vintage(row: dict[str, Any], _: dict[str, Any]) -> bool:
    gap = vintage_gap_days(row)
    return gap is not None and gap >= WIDE_VINTAGE_GAP_DAYS


def _granularity(row: dict[str, Any], _: dict[str, Any]) -> bool:
    """One side names a plan and the other does not, or one aggregates many.

    The mart's rule needs evidence that a plan covers several networks. This
    fires on the weaker signal of an absent plan name on one side only, which is
    the shape that produces the same error without proving it.
    """
    hospital_plan = str(row.get("hospital_plan") or "").strip()
    payer_plan = str(row.get("payer_plan") or "").strip()
    return bool(hospital_plan) != bool(payer_plan)


def _near_offset(row: dict[str, Any], context: dict[str, Any]) -> bool:
    """This row's ratio is shared by several codes in the same contract.

    Below the 20 distinct codes an offset needs, and above coincidence. The
    cluster is computed once per run and handed in, because a rule that
    re-scanned the queue per row would be quadratic in the thing it triages.
    """
    clusters: dict[tuple[str, str], list[float]] = context.get("ratio_clusters", {})
    key = (str(row.get("carrier") or ""), str(row.get("code_type") or ""))
    ratios = clusters.get(key, [])
    if len(ratios) < NEAR_OFFSET_MIN_CODES:
        return False
    ratio = _as_float(row.get("ratio"))
    if not ratio:
        return False
    close = [r for r in ratios if abs(r - ratio) / ratio <= NEAR_OFFSET_TOLERANCE]
    return len(close) >= NEAR_OFFSET_MIN_CODES


def _marginal(row: dict[str, Any], _: dict[str, Any]) -> bool:
    relative = _as_float(row.get("relative_difference"))
    return 0 < relative < MARGINAL_RELATIVE_DIFFERENCE


#: Ordered. The first match wins, so a finding gets the most specific account of
#: itself rather than every account that happens to apply. Implausible leads
#: because a tenfold gap is a data question and answering the others first would
#: bury it.
RULES: tuple[Rule, ...] = (
    Rule(
        "implausible",
        1,
        "ten times apart or more for the same service and payer, which is a unit "
        "or methodology mismatch more often than a negotiated rate",
        _implausible,
    ),
    Rule(
        "vintage_artifact",
        2,
        f"the two sides are at least {WIDE_VINTAGE_GAP_DAYS} days apart, so the "
        "difference may be time rather than contract",
        _wide_vintage,
    ),
    Rule(
        "systematic_offset",
        3,
        f"the same ratio appears on at least {NEAR_OFFSET_MIN_CODES} codes in this "
        "contract, below the threshold the mart collapses on",
        _near_offset,
    ),
    Rule(
        "granularity_mismatch",
        4,
        "one side names a plan and the other does not, so the two may not be the same contract",
        _granularity,
    ),
    Rule(
        "marginal",
        5,
        f"below {MARGINAL_RELATIVE_DIFFERENCE:.0%} relative difference: material by "
        "rule, and not worth a human's time next to the rest of this queue",
        _marginal,
    ),
    Rule(
        "unexplained",
        6,
        "no deterministic rule accounts for this one. This is the queue A1 exists "
        "for, and the only rows worth an LLM call",
        lambda row, context: True,
    ),
)


def ratio_clusters(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[float]]:
    """Ratios per (carrier, code type), computed once for the near-offset rule."""
    clusters: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        ratio = _as_float(row.get("ratio"))
        if ratio <= 0:
            continue
        key = (str(row.get("carrier") or ""), str(row.get("code_type") or ""))
        clusters.setdefault(key, []).append(ratio)
    return clusters


def classify(row: dict[str, Any], context: dict[str, Any] | None = None) -> Rule:
    """The first rule that fires. Never ``None`` -- ``unexplained`` always matches."""
    ctx = context or {}
    for rule in RULES:
        if rule.fires(row, ctx):
            return rule
    return RULES[-1]


def queue(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The triage queue: every residual finding with the rule that accounts for it.

    Sorted by priority then by size, so the top of the queue is the most
    specific verdict about the largest gap. ``unexplained`` sinks to the bottom
    not because it matters least but because everything above it has an answer
    already.
    """
    context = {"ratio_clusters": ratio_clusters(rows)}
    out = []
    for row in rows:
        rule = classify(row, context)
        gap = vintage_gap_days(row)
        out.append(
            {
                **{k: v for k, v in row.items()},
                "triage_rule": rule.name,
                "triage_priority": rule.priority,
                "triage_why": rule.why,
                "vintage_gap_days": "" if gap is None else gap,
            }
        )
    return sorted(
        out,
        key=lambda r: (r["triage_priority"], -_as_float(r.get("relative_difference"))),
    )


def summarise(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """How many findings each rule accounted for, which is the headline.

    The number that matters is ``unexplained``: it is what a human or an agent
    still has to look at, and it is the baseline A1 has to beat.
    """
    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row["triage_rule"])] = counts.get(str(row["triage_rule"]), 0) + 1
    by_name = {rule.name: rule for rule in RULES}
    return [
        {
            "triage_rule": name,
            "findings": count,
            "share": round(count / len(rows), 4) if rows else 0.0,
            "priority": by_name[name].priority if name in by_name else 99,
            "why": by_name[name].why if name in by_name else "",
        }
        for name, count in sorted(
            counts.items(), key=lambda kv: by_name[kv[0]].priority if kv[0] in by_name else 99
        )
    ]


__all__ = [
    "IMPLAUSIBLE_RATIO",
    "MARGINAL_RELATIVE_DIFFERENCE",
    "NEAR_OFFSET_MIN_CODES",
    "RULES",
    "WIDE_VINTAGE_GAP_DAYS",
    "Rule",
    "classify",
    "queue",
    "ratio_clusters",
    "summarise",
    "vintage_gap_days",
]
