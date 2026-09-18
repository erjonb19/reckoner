"""The gold layer: the reconciliation mart, built one shard at a time.

Silver holds conformed rates. Gold holds what comparing them produced -- the
residual disagreements that survived every deterministic explanation, and the
accounting that gives that residual its denominators.

**Gold is not every pair.** One system alone formed 3,370,446 pairs, of which
3,011,270 were "material" and 60,182 were material *and* unexplained. Material is
not selective; unexplained is. Storing all 3.37 million would be storing an
intermediate: the pairs that agree are counted, not kept, and the counts are what
a reader needs to know the residual is 1.8% rather than the whole story.

**Why this is not simply the existing mart run in a loop.** ``mart_cli`` refuses
``--all-shards`` outside range mode, and the refusal is well founded: a variance
mart carries cross-row state. :func:`find_systematic_offsets` groups pairs by
*contract* -- (facility, carrier, hospital plan, payer plan) -- and asks whether
one constant ratio covers enough distinct codes to be a fact about two base rates
rather than hundreds of separate findings. Shards split on the code's first
character, so every contract is scattered across 36 of them. Detecting offsets
per shard would see a thirty-sixth of each contract and fail the ``min_codes``
threshold on contracts that genuinely have an offset.

That matters more than it sounds: collapsing offsets is what turned 2,943
unexplained Mount Sinai pairs into 27, and chasing the four constants found a
real join defect. A sharded run that lost it would report hundreds of findings
that are not findings.

So the shard loop accumulates a **projection** of each contract rather than its
rows: one float per priced pair and a pointer to an interned code string, which
is 16 bytes against the several hundred a :class:`Variance` occupies. Offsets are
then computed once, at the end, over exactly the population
:func:`find_systematic_offsets` would have seen unsharded -- and
``test_sharding_changes_nothing`` holds that equality down.
"""

from __future__ import annotations

import sys
from array import array
from collections.abc import Iterable
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from reconcile.comparability import ComparableRate
from reconcile.variance import (
    OFFSET_MIN_CODES,
    OFFSET_MIN_SHARE,
    OFFSET_TOLERANCE,
    Explanation,
    SystematicOffset,
    Variance,
    VarianceMart,
    cross_source_variance,
)

#: The contract a pair belongs to. Same key :func:`find_systematic_offsets`
#: groups on, because the whole point is to reproduce its answer exactly.
ContractKey = tuple[str, str, str | None, str | None]

UNEXPLAINED = str(Explanation.UNEXPLAINED)
SYSTEMATIC_OFFSET = str(Explanation.SYSTEMATIC_OFFSET)


@dataclass(frozen=True)
class ResidualRow:
    """One disagreement no deterministic rule accounts for.

    Flat and self-describing: this is the row a BI tool charts, a reviewer reads,
    and A1 triages, so it carries its own context rather than referring out to a
    dimension table that would have to be joined to mean anything.
    """

    hospital_slug: str
    system: str
    facility: str
    carrier: str
    code: str
    code_type: str
    setting: str
    billing_class: str
    hospital_plan: str
    payer_plan: str
    hospital_rate: float
    payer_rate: float
    difference: float
    ratio: float
    relative_difference: float
    is_implausible: bool
    hospital_vintage: str
    payer_vintage: str
    notes: str

    @property
    def contract(self) -> ContractKey:
        return (
            self.facility,
            self.carrier,
            self.hospital_plan or None,
            self.payer_plan or None,
        )


@dataclass
class _Contract:
    """The projection of one contract's pairs that offset detection needs.

    Ratios in an ``array('d')`` and codes as interned strings: 16 bytes a pair
    against the several hundred a :class:`Variance` holds, which is the whole
    reason a 3.4-million-pair system fits in a 4 GiB container.
    """

    ratios: array[float] = field(default_factory=lambda: array("d"))
    codes: list[str] = field(default_factory=list)
    #: Pairs that are unexplained but *not* material. They never reach the
    #: residual, but an offset still reclassifies them, and the counts have to
    #: move with it or the breakdown stops summing to the total. The code type
    #: rides along because the outcome counts are keyed by it and this is the
    #: only place these pairs are still remembered.
    immaterial_ratios: array[float] = field(default_factory=lambda: array("d"))
    immaterial_code_types: list[str] = field(default_factory=list)


@dataclass
class Reconciliation:
    """One system's reconciliation, accumulated across shards and then closed."""

    hospital: str
    system: str
    hospital_slug: str

    #: Whether an absent hospital billing class was read as ``facility`` for this
    #: system. It changes the numbers materially -- without it NYU Langone
    #: refuses 136,259,228 candidates and produces nothing -- so it is recorded
    #: beside them rather than inferred from their size.
    assumed_facility_when_unstated: bool = False
    hospital_rates: int = 0
    payer_rates: int = 0
    pairs_formed: int = 0
    excluded: dict[str, int] = field(default_factory=dict)
    #: The same refusals keyed (reason, carrier, code_type), so a report can
    #: filter them. Accumulated beside ``excluded``, never instead of it.
    excluded_detail: dict[tuple[str, str, str], int] = field(default_factory=dict)
    explanation: dict[str, int] = field(default_factory=dict)
    facilities: set[str] = field(default_factory=set)
    caveats: list[str] = field(default_factory=list)
    shards: list[str] = field(default_factory=list)

    residual: list[ResidualRow] = field(default_factory=list)
    offsets: list[SystematicOffset] = field(default_factory=list)
    #: Pairs and material pairs keyed (carrier, code_type, explanation). This is
    #: the grain every filter in the report acts on -- a page that filters by
    #: carrier against a table counted per system would answer with the system's
    #: numbers and look like it had filtered.
    outcomes: dict[tuple[str, str, str], list[int]] = field(default_factory=dict)
    _contracts: dict[ContractKey, _Contract] = field(default_factory=dict)
    _closed: bool = False

    @property
    def material(self) -> int:
        return self.explanation.get("_material", 0)

    @property
    def comparable_share(self) -> float:
        total = self.pairs_formed + sum(self.excluded.values())
        return self.pairs_formed / total if total else 0.0

    def add_shard(
        self,
        shard: str,
        mart: VarianceMart,
        *,
        hospital_rates: int = 0,
        payer_rates: int = 0,
    ) -> None:
        """Fold one shard's mart in, keeping only what survives the whole run.

        Deliberately called with a mart whose offsets have **not** been applied:
        doing that per shard is the thing this class exists to avoid.
        """
        if self._closed:
            raise RuntimeError("cannot add a shard after close(); the offsets are already fixed")
        self.shards.append(shard)
        self.hospital_rates += hospital_rates
        self.payer_rates += payer_rates
        self.pairs_formed += len(mart.rows)
        for reason, count in mart.excluded.items():
            self.excluded[reason] = self.excluded.get(reason, 0) + count
        for detail_key, detail_count in mart.excluded_detail.items():
            self.excluded_detail[detail_key] = (
                self.excluded_detail.get(detail_key, 0) + detail_count
            )
        for note in mart.provenance.caveats:
            if note not in self.caveats:
                self.caveats.append(note)

        for row in mart.rows:
            self.explanation[row.explanation] = self.explanation.get(row.explanation, 0) + 1
            if row.is_material:
                self.explanation["_material"] = self.explanation.get("_material", 0) + 1
            self.facilities.add(row.left.hospital)

            bucket = self.outcomes.setdefault(
                (row.payer, row.code_type or "", row.explanation), [0, 0]
            )
            bucket[0] += 1
            bucket[1] += 1 if row.is_material else 0

            key: ContractKey = (row.left.hospital, row.payer, row.left.plan, row.right.plan)
            contract = self._contracts.setdefault(key, _Contract())
            if row.ratio > 0:
                contract.ratios.append(row.ratio)
                # Interned so the list holds pointers to one copy per distinct
                # code rather than a string per pair.
                contract.codes.append(sys.intern(row.code))

            if row.explanation != UNEXPLAINED:
                continue
            if row.is_material:
                self.residual.append(
                    _as_residual(row, self.hospital, self.system, self.hospital_slug)
                )
            elif row.ratio > 0:
                contract.immaterial_ratios.append(row.ratio)
                contract.immaterial_code_types.append(sys.intern(row.code_type or ""))

    def close(
        self,
        *,
        tolerance: float = OFFSET_TOLERANCE,
        min_codes: int = OFFSET_MIN_CODES,
        min_share: float = OFFSET_MIN_SHARE,
    ) -> None:
        """Detect offsets over the whole run and reclassify what they explain.

        Reproduces :func:`apply_systematic_offsets` from the accumulated
        projection: same grouping, same median, same band, same thresholds --
        and therefore the same answer as an unsharded run.
        """
        if self._closed:
            return
        self.offsets = _offsets_from(
            self._contracts, tolerance=tolerance, min_codes=min_codes, min_share=min_share
        )
        by_key = {(o.hospital, o.payer, o.hospital_plan, o.payer_plan): o for o in self.offsets}

        kept: list[ResidualRow] = []
        moved = 0
        for row in self.residual:
            offset = by_key.get(row.contract)
            if offset is not None and _in_band(row.ratio, offset.ratio, tolerance):
                moved += 1
                self._reclassify(row.carrier, row.code_type, material=True)
                continue
            kept.append(row)
        self.residual = kept

        # The immaterial unexplained pairs move too. They never reach the
        # residual, but leaving them counted as unexplained would make the
        # breakdown disagree with a run that was not sharded.
        for key, contract in self._contracts.items():
            offset = by_key.get(key)
            if offset is None:
                continue
            for ratio, code_type in zip(
                contract.immaterial_ratios, contract.immaterial_code_types, strict=True
            ):
                if _in_band(ratio, offset.ratio, tolerance):
                    moved += 1
                    self._reclassify(key[1], code_type, material=False)

        if moved:
            self.explanation[UNEXPLAINED] = self.explanation.get(UNEXPLAINED, 0) - moved
            self.explanation[SYSTEMATIC_OFFSET] = self.explanation.get(SYSTEMATIC_OFFSET, 0) + moved
        self._contracts.clear()
        self._closed = True

    def _reclassify(self, carrier: str, code_type: str, *, material: bool) -> None:
        """Move one pair from unexplained to systematic_offset at outcome grain."""
        source = self.outcomes.get((carrier, code_type, UNEXPLAINED))
        if source is not None:
            source[0] -= 1
            source[1] -= 1 if material else 0
        target = self.outcomes.setdefault((carrier, code_type, SYSTEMATIC_OFFSET), [0, 0])
        target[0] += 1
        target[1] += 1 if material else 0

    def outcome_rows(self) -> list[dict[str, Any]]:
        """Pairs by carrier, code type and explanation -- what the filters act on."""
        self._require_closed("outcome_rows")
        return [
            {
                "hospital_slug": self.hospital_slug,
                "system": self.system,
                "carrier": carrier,
                "code_type": code_type,
                "explanation": explanation,
                "pairs": pairs,
                "material_pairs": material,
            }
            for (carrier, code_type, explanation), (pairs, material) in sorted(
                self.outcomes.items()
            )
            if pairs
        ]

    def magnitude_rows(self) -> list[dict[str, Any]]:
        """How big the surviving disagreements are, per carrier and code type.

        Computed from the residual rather than accumulated, which costs nothing:
        the residual is already in memory and is small. A page that counts pairs
        without sizing them cannot answer the question the project is about.
        """
        self._require_closed("magnitude_rows")
        grouped: dict[tuple[str, str], list[ResidualRow]] = {}
        for row in self.residual:
            grouped.setdefault((row.carrier, row.code_type), []).append(row)
        rows = []
        for (carrier, code_type), members in sorted(grouped.items()):
            relative = sorted(r.relative_difference for r in members)
            absolute = sorted(abs(r.difference) for r in members)
            rows.append(
                {
                    "hospital_slug": self.hospital_slug,
                    "system": self.system,
                    "carrier": carrier,
                    "code_type": code_type,
                    "residual_pairs": len(members),
                    "median_relative_difference": round(median(relative), 6),
                    "p90_relative_difference": round(_percentile(relative, 0.9), 6),
                    "median_abs_difference_usd": round(median(absolute), 2),
                    "implausible_pairs": sum(1 for r in members if r.is_implausible),
                }
            )
        return rows

    def exemplar_rows(self, per_carrier: int = 25) -> list[dict[str, Any]]:
        """The widest residual disagreements, capped per carrier.

        Row-level and therefore bounded on purpose: without a few real examples
        a report can show that disagreements exist and never show one.
        """
        self._require_closed("exemplar_rows")
        by_carrier: dict[str, list[ResidualRow]] = {}
        for row in self.residual:
            by_carrier.setdefault(row.carrier, []).append(row)
        chosen: list[ResidualRow] = []
        for carrier in sorted(by_carrier):
            ranked = sorted(by_carrier[carrier], key=lambda r: (-r.relative_difference, r.code))
            chosen.extend(ranked[:per_carrier])
        return as_records(chosen)

    def coverage_row(self) -> dict[str, Any]:
        """One row: the funnel from rows read to findings that survived."""
        self._require_closed("coverage_row")
        return {
            "hospital_slug": self.hospital_slug,
            "system": self.system,
            "hospital_rates": self.hospital_rates,
            "payer_rates": self.payer_rates,
            "candidates": self.pairs_formed + sum(self.excluded.values()),
            "pairs_formed": self.pairs_formed,
            "comparable_share": round(self.comparable_share, 6),
            "material": self.material,
            "unexplained_and_material": len(self.residual),
            "facilities": len(self.facilities),
            "carriers": len({carrier for carrier, _, _ in self.outcomes}),
            "systematic_offsets": len(self.offsets),
            "assumed_facility_when_unstated": self.assumed_facility_when_unstated,
        }

    def refusal_rows(self) -> list[dict[str, Any]]:
        """Why candidates never became pairs, by carrier and code type.

        Emitted from ``excluded_detail`` so all three of the report's filters
        apply. A refusal the comparability layer could not attribute carries an
        empty carrier or code type rather than a guess -- attributing one to the
        wrong carrier would be worse than attributing it to none, and the blank
        is visible in the table.

        The totals are the ones ``excluded`` has always held:
        ``test_refusal_rows_sum_to_the_old_totals`` holds that down, because a
        finer breakdown that quietly stopped adding up would be a regression
        wearing a feature's clothes.
        """
        self._require_closed("refusal_rows")
        rows = [
            {
                "hospital_slug": self.hospital_slug,
                "system": self.system,
                "reason": reason,
                "carrier": carrier,
                "code_type": code_type,
                "candidates": count,
            }
            for (reason, carrier, code_type), count in sorted(
                self.excluded_detail.items(), key=lambda kv: -kv[1]
            )
        ]
        if rows:
            return rows
        # A mart built before the finer counting existed, or one whose refusals
        # all came from call sites without the dimensions. Fall back rather than
        # report nothing, and keep the columns so the shape is stable.
        return [
            {
                "hospital_slug": self.hospital_slug,
                "system": self.system,
                "reason": reason,
                "carrier": "",
                "code_type": "",
                "candidates": count,
            }
            for reason, count in sorted(self.excluded.items(), key=lambda kv: -kv[1])
        ]

    def _require_closed(self, what: str) -> None:
        if not self._closed:
            raise RuntimeError(f"call close() before {what}; the offsets are not yet applied")

    def measures(self) -> list[dict[str, Any]]:
        """The funnel, long-format: one row per measured quantity.

        Long rather than wide because the breakdowns (by exclusion reason, by
        explanation) have no fixed column set, and a table that grows a column
        whenever a new reason appears is one a report has to be edited to read.
        """
        self._require_closed("measures")
        rows = [
            ("headline", "hospital_rates", float(self.hospital_rates)),
            ("headline", "payer_rates", float(self.payer_rates)),
            ("headline", "pairs_formed", float(self.pairs_formed)),
            ("headline", "comparable_share", round(self.comparable_share, 6)),
            ("headline", "material", float(self.material)),
            ("headline", "unexplained_and_material", float(len(self.residual))),
            ("headline", "facilities", float(len(self.facilities))),
            ("headline", "systematic_offsets", float(len(self.offsets))),
        ]
        rows += [("excluded", reason, float(n)) for reason, n in sorted(self.excluded.items())]
        rows += [
            ("explanation", name, float(n))
            for name, n in sorted(self.explanation.items())
            if not name.startswith("_")
        ]
        rows += [
            (
                "systematic_offset",
                f"{o.hospital} / {o.payer}",
                round(o.ratio, 6),
            )
            for o in self.offsets
        ]
        return [
            {
                "hospital_slug": self.hospital_slug,
                "system": self.system,
                "measure": measure,
                "key": key,
                "value": value,
            }
            for measure, key, value in rows
        ]


def _percentile(ordered: list[float], q: float) -> float:
    """Nearest-rank percentile over an already sorted list."""
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round(q * len(ordered)) - 1))
    return ordered[index]


def _in_band(ratio: float, centre: float, tolerance: float) -> bool:
    return bool(centre) and abs(ratio - centre) / centre <= tolerance


def _offsets_from(
    contracts: dict[ContractKey, _Contract],
    *,
    tolerance: float,
    min_codes: int,
    min_share: float,
) -> list[SystematicOffset]:
    """The same test :func:`find_systematic_offsets` applies, on the projection."""
    offsets: list[SystematicOffset] = []
    for (hospital, payer, left_plan, right_plan), contract in contracts.items():
        if len(contract.ratios) < min_codes:
            continue
        centre = median(contract.ratios)
        if not centre:
            continue
        inside = [
            (ratio, code)
            for ratio, code in zip(contract.ratios, contract.codes, strict=True)
            if _in_band(ratio, centre, tolerance)
        ]
        codes = {code for _, code in inside}
        if len(codes) < min_codes or len(inside) / len(contract.ratios) < min_share:
            continue
        offsets.append(
            SystematicOffset(
                hospital=hospital,
                payer=payer,
                hospital_plan=left_plan,
                payer_plan=right_plan,
                ratio=median(ratio for ratio, _ in inside),
                codes=len(codes),
                rows=len(inside),
            )
        )
    return sorted(offsets, key=lambda o: -o.rows)


def _as_residual(row: Variance, hospital: str, system: str, slug: str) -> ResidualRow:
    return ResidualRow(
        hospital_slug=slug,
        system=system,
        facility=row.left.hospital,
        carrier=row.payer,
        code=row.code,
        code_type=row.code_type or "",
        setting=row.setting or "",
        billing_class=row.left.billing_class or "",
        hospital_plan=row.left.plan or "",
        payer_plan=row.right.plan or "",
        hospital_rate=row.left_rate,
        payer_rate=row.right_rate,
        difference=row.difference,
        ratio=row.ratio,
        relative_difference=row.relative_difference,
        is_implausible=row.is_implausible,
        hospital_vintage=row.left.vintage or "",
        payer_vintage=row.right.vintage or "",
        notes=" | ".join(row.notes),
    )


def reconcile_shard(
    hospital_side: list[ComparableRate],
    payer_side: list[ComparableRate],
    *,
    max_vintage_days: int = 400,
    assume_facility_when_unstated: frozenset[str] = frozenset(),
) -> VarianceMart:
    """One shard's mart, with offsets deliberately **not** applied.

    Separated so the shard loop cannot accidentally apply them: doing it here
    would compute each offset from a thirty-sixth of its contract.
    """
    return cross_source_variance(
        hospital_side,
        payer_side,
        max_vintage_days=max_vintage_days,
        assume_facility_when_unstated=assume_facility_when_unstated,
    )


def residual_columns() -> tuple[str, ...]:
    return tuple(ResidualRow.__dataclass_fields__)


def as_records(rows: Iterable[ResidualRow]) -> list[dict[str, Any]]:
    return [{name: getattr(row, name) for name in residual_columns()} for row in rows]


__all__ = [
    "SYSTEMATIC_OFFSET",
    "UNEXPLAINED",
    "ContractKey",
    "Reconciliation",
    "ResidualRow",
    "as_records",
    "reconcile_shard",
    "residual_columns",
]
