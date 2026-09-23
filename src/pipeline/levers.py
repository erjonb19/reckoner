"""Why candidates never became pairs, and which fixes would move the share.

Reads the published summary -- refusals, coverage, outcomes -- and computes
nothing gold did not already count. It changes no result. What it adds is
attribution: each refusal reason is assigned the lever that could turn it into
a pair, a feasibility for pulling that lever, and the comparable-share lift if
it were pulled all the way.

Three things about the arithmetic, because each would mislead if left unsaid:

* **Lift is an upper bound.** It assumes every refused candidate of a reason
  becomes a pair. Nothing resolves fully, and feasibility is where that is
  discounted -- as a stated judgement, not a measurement.
* **Every count is a hospital rate** (ADR 0006). Each hospital rate is exactly
  one outcome -- compared against the carrier's distribution, or refused once
  for one reason -- so the reasons, the pairs and the denominator share a unit.
  Before ADR 0006 candidates were counted after a carrier-level fan-out, and
  ``no payer-side counterpart`` was the only reason counted per hospital rate.
* **Billing-class refusals are a definition, not a lever** (ADR 0005). They
  are reported, and the like-class share leaves them out of its denominator.

Levers that cannot move the share are reported with a lift of zero rather than
left out. Plan matching is the important one: an unresolved plan is an
*explanation* attached to a pair that formed, never a refusal, so under the
current join it moves how much of the share is explained, not the share.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents.entity_resolution import CANONICAL_PAYERS

SUMMARY = Path("summary")
DOC = Path("docs/refusal-decomposition.md")

#: Feasibility as a weight. A judgement, stated as one: ``high`` means the fix
#: is in this codebase's reach with data already on hand; ``none`` means the
#: refusal is correct and should stay a refusal.
FEASIBILITY: dict[str, float] = {"high": 0.8, "medium": 0.5, "low": 0.2, "none": 0.0}

NO_COUNTERPART = "no payer-side counterpart"


@dataclass(frozen=True)
class Lever:
    name: str
    feasibility: str
    why: str


#: Each refusal reason the comparability layer can emit, and what would fix it.
BY_REASON: dict[str, Lever] = {
    "different_billing_class": Lever(
        "none (ADR 0005)",
        "none",
        "the hospital rate's only counterparts are the other billing class -- a facility "
        "charge against a professional fee. The join keys on billing class, so these are "
        "never compared, and the like-class share leaves them out of its denominator",
    ),
    "billing_class_unstated": Lever(
        "billing class",
        "medium",
        "the hospital left it blank and the facility-only assumption did not apply",
    ),
    "tic_exempt_product": Lever(
        "none",
        "none",
        "Medicare Advantage and Medicaid rates exist only in hospital files; Transparency "
        "in Coverage exempts them by rule. A correct refusal",
    ),
    "zero_rate": Lever(
        "none",
        "none",
        "a $0 negotiated rate on one side is a placeholder in the filing, not a price",
    ),
    "missing_rate": Lever("none", "none", "one side published no amount"),
    "incompatible_methodology": Lever(
        "methodology normalization",
        "low",
        "a per diem against a case rate needs a length of stay neither file publishes",
    ),
    "mixed_rate_kind": Lever(
        "methodology normalization",
        "medium",
        "a percentage against a dollar amount; percent-of-charges converts with the "
        "gross charge the hospital publishes in the same file",
    ),
    "not_dollar_denominated": Lever(
        "methodology normalization", "medium", "both sides are percentages of something"
    ),
    "vintage_too_far_apart": Lever(
        "vintage tolerance",
        "high",
        "a constant in the mart; widening it is one line and a judgement about drift",
    ),
    "different_setting": Lever(
        "modifier/setting normalization", "medium", "inpatient against outpatient"
    ),
    "different_code": Lever("none", "none", "structurally different services"),
    "different_code_type": Lever("none", "none", "structurally different code systems"),
}

#: ``no payer-side counterpart`` splits three ways on carrier, because who the
#: hospital named decides whether a counterpart could exist at all.
NO_COUNTERPART_IN_CORPUS = Lever(
    "modifier/setting normalization",
    "low",
    "the carrier is in the payer corpus but published no rate at this facility, code "
    "and setting. Some share is a setting or facility bridge the join missed; most is "
    "the payer genuinely not listing the code there, and gold cannot split the two",
)
NO_COUNTERPART_OUTSIDE_CORPUS = Lever(
    "none",
    "none",
    "a real carrier with no payer file here (Healthfirst, MetroPlus, Fidelis, ...). "
    "Adding carriers is coverage, not a fix to the reconciliation",
)
NO_COUNTERPART_UNRESOLVED = Lever(
    "payer/plan name matching",
    "medium",
    "the hospital's payer string did not resolve to a canonical carrier (for example "
    "'local', 'connecticare', 'northwell direct'). Some would resolve to a corpus "
    "carrier and pair; many name carriers the corpus does not hold",
)
NO_COUNTERPART_UNATTRIBUTED = Lever(
    "unattributed",
    "none",
    "written by an image that predates carrier-grain refusals (#63); rerunning the "
    "system attributes it",
)
UNKNOWN = Lever("unclassified", "none", "a reason this table does not know yet")


def lever_for(reason: str, carrier: str | None, corpus: frozenset[str]) -> Lever:
    if reason != NO_COUNTERPART:
        return BY_REASON.get(reason, UNKNOWN)
    if not carrier:
        return NO_COUNTERPART_UNATTRIBUTED
    if carrier in corpus:
        return NO_COUNTERPART_IN_CORPUS
    if carrier in CANONICAL_PAYERS:
        return NO_COUNTERPART_OUTSIDE_CORPUS
    return NO_COUNTERPART_UNRESOLVED


def _read(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _int(value: object) -> int:
    try:
        return int(float(str(value)))
    except ValueError:
        return 0


@dataclass
class Decomposition:
    coverage: list[dict[str, Any]]
    refusals: list[dict[str, Any]]
    outcomes: list[dict[str, Any]]

    @classmethod
    def load(cls, root: Path = SUMMARY) -> Decomposition:
        return cls(
            _read(root / "coverage.csv"),
            _read(root / "refusals.csv"),
            _read(root / "outcomes.csv"),
        )

    @property
    def corpus(self) -> frozenset[str]:
        """Carriers the payer side holds: every carrier that formed a pair anywhere."""
        return frozenset(r["carrier"] for r in self.outcomes if r.get("carrier"))

    @property
    def candidates(self) -> int:
        return sum(_int(r["candidates"]) for r in self.coverage)

    @property
    def pairs(self) -> int:
        return sum(_int(r["pairs_formed"]) for r in self.coverage)

    def arithmetic_holds(self) -> dict[str, tuple[int, int]]:
        """Per system: candidates, and pairs plus refusals. They must agree."""
        refused: dict[str, int] = defaultdict(int)
        for row in self.refusals:
            refused[row["system"]] += _int(row["candidates"])
        return {
            r["system"]: (_int(r["candidates"]), _int(r["pairs_formed"]) + refused[r["system"]])
            for r in self.coverage
        }

    def by_reason(self) -> list[dict[str, Any]]:
        """One row per reason and lever, pooled over systems, largest first."""
        corpus = self.corpus
        totals: dict[tuple[str, str], int] = defaultdict(int)
        levers: dict[tuple[str, str], Lever] = {}
        for row in self.refusals:
            lever = lever_for(row["reason"], row.get("carrier") or None, corpus)
            key = (row["reason"], lever.name)
            totals[key] += _int(row["candidates"])
            levers[key] = lever
        total = self.candidates
        out = []
        for (reason, _), count in totals.items():
            lever = levers[(reason, _)]
            lift = count / total if total else 0.0
            out.append(
                {
                    "reason": reason,
                    "lever": lever.name,
                    "feasibility": lever.feasibility,
                    "candidates": count,
                    "share_of_candidates": lift,
                    "lift_if_resolved": lift,
                    "weighted_lift": lift * FEASIBILITY[lever.feasibility],
                    "why": lever.why,
                }
            )
        return sorted(out, key=lambda r: -r["candidates"])

    def by_lever(self) -> list[dict[str, Any]]:
        """Levers ranked by lift x feasibility, with the ones that cannot move it."""
        grouped: dict[str, dict[str, Any]] = {}
        for row in self.by_reason():
            entry = grouped.setdefault(
                row["lever"],
                {
                    "lever": row["lever"],
                    "feasibility": row["feasibility"],
                    "lift_if_resolved": 0.0,
                    "weighted_lift": 0.0,
                    "reasons": [],
                },
            )
            entry["lift_if_resolved"] += row["lift_if_resolved"]
            entry["weighted_lift"] += row["weighted_lift"]
            entry["reasons"].append(row["reason"])
        # Levers with no refusals to act on, stated at zero rather than omitted:
        # their absence from the table would read as "not considered".
        for name in ("plan matching", "vintage tolerance", "modifier/setting normalization"):
            grouped.setdefault(
                name,
                {
                    "lever": name,
                    "feasibility": "n/a",
                    "lift_if_resolved": 0.0,
                    "weighted_lift": 0.0,
                    "reasons": [],
                },
            )
        return sorted(grouped.values(), key=lambda r: (-r["weighted_lift"], -r["lift_if_resolved"]))

    def by_system_and_carrier(self, reason: str, top: int = 8) -> list[dict[str, Any]]:
        cells: dict[tuple[str, str], int] = defaultdict(int)
        for row in self.refusals:
            if row["reason"] == reason:
                cells[(row["system"], row.get("carrier") or "")] += _int(row["candidates"])
        total = sum(cells.values())
        ranked = sorted(cells.items(), key=lambda kv: -kv[1])[:top]
        return [
            {
                "system": system,
                "carrier": carrier or "(not attributed)",
                "candidates": count,
                "share_of_reason": count / total if total else 0.0,
            }
            for (system, carrier), count in ranked
        ]

    def per_system(self) -> list[dict[str, Any]]:
        """Each system's share today, expected with feasible levers, and redefined.

        ``expected`` weights each refused candidate by its lever's feasibility.
        An earlier draft reported a ceiling instead -- every feasible lever
        pulled all the way -- and it put Mount Sinai at 95.8%, because it
        counted every professional-versus-institutional refusal as recoverable.
        Most are correct refusals, so the ceiling was a number nobody could
        reach and a reader would quote.

        ``billing_class_keyed`` is not a lever. It is the share if the join keyed
        on billing class, so professional and institutional rates never became
        candidates of each other: a change of denominator, like #70's, reported
        beside the share and never in place of it.
        """
        corpus = self.corpus
        weighted: dict[str, float] = defaultdict(float)
        billing: dict[str, int] = defaultdict(int)
        for row in self.refusals:
            lever = lever_for(row["reason"], row.get("carrier") or None, corpus)
            weighted[row["system"]] += _int(row["candidates"]) * FEASIBILITY[lever.feasibility]
            if row["reason"] == "different_billing_class":
                billing[row["system"]] += _int(row["candidates"])
        out = []
        for row in self.coverage:
            candidates = _int(row["candidates"])
            pairs = _int(row["pairs_formed"])
            keyed = candidates - billing[row["system"]]
            out.append(
                {
                    "system": row["system"],
                    "candidates": candidates,
                    "share_now": pairs / candidates if candidates else 0.0,
                    "expected": (pairs + weighted[row["system"]]) / candidates
                    if candidates
                    else 0.0,
                    "billing_class_share": billing[row["system"]] / candidates
                    if candidates
                    else 0.0,
                    "billing_class_keyed": pairs / keyed if keyed else 0.0,
                }
            )
        return sorted(out, key=lambda r: -r["candidates"])

    def plan_unresolved_share(self) -> float:
        """How much of what did pair is explained only as an unresolved plan."""
        pairs = sum(_int(r["pairs"]) for r in self.outcomes)
        plan = sum(_int(r["pairs"]) for r in self.outcomes if r["explanation"] == "plan_unresolved")
        return plan / pairs if pairs else 0.0


def _pct(value: float) -> str:
    return f"{value:.2%}"


def _setting_and_vintage(d: Decomposition) -> list[str]:
    """What the data says about the two levers that were once always zero.

    Computed, not asserted: the first version of this document stated neither
    had refused anything, and the next rebuild refused 0.23% for vintage.
    """
    counts = {
        reason: sum(_int(r["candidates"]) for r in d.refusals if r["reason"] == reason)
        for reason in ("vintage_too_far_apart", "different_setting")
    }
    lines = [
        f"**Vintage tolerance** covers {counts['vintage_too_far_apart']:,} hospital rates "
        f"refused as `vintage_too_far_apart` "
        f"({_pct(counts['vintage_too_far_apart'] / d.candidates if d.candidates else 0.0)}).",
    ]
    if counts["different_setting"]:
        lines.append(f"**Setting** refused {counts['different_setting']:,} as `different_setting`.")
    else:
        lines += [
            "**Setting** refused nothing as `different_setting`: setting is part of the",
            "join key, so a setting mismatch surfaces as `no payer-side counterpart`",
            "instead, inside the in-corpus row below.",
        ]
    return lines


def markdown(d: Decomposition, *, generated_from: str = "summary/") -> str:
    share = d.pairs / d.candidates if d.candidates else 0.0
    unattributed = sum(_int(r["candidates"]) for r in d.refusals if not r.get("carrier"))
    stale = sorted({r["system"] for r in d.refusals if not r.get("carrier")}) or ["none"]
    lines = [
        "# Refusal decomposition",
        "",
        f"Generated by `python -m pipeline.levers` from `{generated_from}`. Changes no result;",
        "every count is one gold already published.",
        "",
        f"Across {len(d.coverage)} reconciled systems: **{d.candidates:,} candidates, "
        f"{d.pairs:,} pairs, a pooled comparable share of {_pct(share)}.** The other "
        f"{d.candidates - d.pairs:,} were refused, and each refusal carries a reason.",
        "",
        "## Read this first",
        "",
        "- **Lift is an upper bound**: every refused candidate of a reason becoming a pair.",
        "  Feasibility discounts it, and feasibility is a stated judgement, not a measurement.",
        "- **Every count is a hospital rate** (ADR 0006): each is compared once against",
        "  the carrier's distribution, or refused once for one reason.",
        "- **Billing-class refusals are a definition** (ADR 0005), shown as `none` below.",
        "  The like-class share in the per-system table leaves them out.",
    ]
    if unattributed:
        lines += [
            f"- {unattributed:,} candidates carry no carrier ({', '.join(stale)}): gold written",
            "  before #63 and not yet rebuilt. They are shown as `(not attributed)`, never",
            "  guessed.",
        ]
    lines += [
        "",
        "## Levers, ranked by lift x feasibility",
        "",
        "| lever | feasibility | lift if fully resolved | lift x feasibility | reasons |",
        "|---|---|---:|---:|---|",
    ]
    for row in d.by_lever():
        lines.append(
            f"| {row['lever']} | {row['feasibility']} | {_pct(row['lift_if_resolved'])} "
            f"| {_pct(row['weighted_lift'])} | {', '.join(row['reasons']) or '—'} |"
        )
    lines += [
        "",
        "Weights: high 0.8, medium 0.5, low 0.2, none 0.",
        "",
        "**Plan matching lifts comparable share by zero, by construction.** An unresolved",
        "plan is an explanation on a comparison that formed, never a refusal. What it",
        f"moves is the {_pct(d.plan_unresolved_share())} of comparisons whose only",
        "explanation is `plan_unresolved`: the hospital's plan matched none of the",
        "networks in the carrier's distribution (ADR 0006).",
        "",
        *_setting_and_vintage(d),
        "",
        "## Every reason",
        "",
        "| reason | lever | feasibility | candidates | share of candidates |",
        "|---|---|---|---:|---:|",
    ]
    for row in d.by_reason():
        lines.append(
            f"| {row['reason']} | {row['lever']} | {row['feasibility']} "
            f"| {row['candidates']:,} | {_pct(row['share_of_candidates'])} |"
        )
    lines += ["", "What each lever assignment rests on:", ""]
    seen: set[tuple[str, str]] = set()
    for row in d.by_reason():
        key = (row["reason"], row["lever"])
        if key not in seen:
            seen.add(key)
            lines.append(f"- **{row['reason']} → {row['lever']}**: {row['why']}.")
    lines += [
        "",
        "## Per system",
        "",
        "| system | candidates | raw share | expected with levers | billing-class refusals "
        "| like-class share |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in d.per_system():
        lines.append(
            f"| {row['system']} | {row['candidates']:,} | {_pct(row['share_now'])} "
            f"| {_pct(row['expected'])} | {_pct(row['billing_class_share'])} "
            f"| {_pct(row['billing_class_keyed'])} |"
        )
    billing_total = sum(
        _int(r["candidates"]) for r in d.refusals if r["reason"] == "different_billing_class"
    )
    keyed = d.pairs / (d.candidates - billing_total) if d.candidates > billing_total else 0.0
    lines += [
        "",
        "*Expected* weights each refused candidate by its lever's feasibility. It is a",
        "judgement dressed as a number, and is here only so the levers can be ranked.",
        "",
        f"**Billing class.** {_pct(billing_total / d.candidates)} of hospital rates found",
        "counterparts only in the other billing class. ADR 0005 keys the join on billing",
        "class, so they are refused once and never compared. The pooled like-class share",
        f"is {_pct(keyed)}; the raw share is {_pct(d.pairs / d.candidates)}. Both are reported,",
        "side by side, for this release.",
        "",
        "## By system and carrier, for the reasons that matter",
        "",
    ]
    for reason in [r["reason"] for r in d.by_reason()][:5]:
        lines += [
            f"### {reason}",
            "",
            "| system | carrier | candidates | share of this reason |",
            "|---|---|---:|---:|",
        ]
        for cell in d.by_system_and_carrier(reason):
            lines.append(
                f"| {cell['system']} | {cell['carrier']} | {cell['candidates']:,} "
                f"| {_pct(cell['share_of_reason'])} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--summary", type=Path, default=SUMMARY)
    parser.add_argument("--out", type=Path, default=DOC)
    args = parser.parse_args(argv)
    decomposition = Decomposition.load(args.summary)
    mismatched = {s: v for s, v in decomposition.arithmetic_holds().items() if v[0] != v[1]}
    if mismatched:
        # Pairs plus refusals must be the candidates, or the shares below are
        # shares of something else. Refuse to write a document that would lie.
        print(f"candidates != pairs + refusals for {mismatched}")
        return 1
    args.out.write_text(markdown(decomposition), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BY_REASON",
    "FEASIBILITY",
    "Decomposition",
    "Lever",
    "lever_for",
    "main",
    "markdown",
]
