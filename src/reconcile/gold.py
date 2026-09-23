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
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from statistics import median
from typing import Any

import pyarrow as pa

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
    iter_cross_source_variance,
    iter_distribution_variance,
)

#: How a shard's hospital rates meet its payer rates. ``distribution`` is the
#: published grain (ADR 0006): one outcome per hospital rate, against the
#: carrier's distribution. ``pair`` is the old one, one pair per payer rate,
#: kept for the tests that pin the offset machinery down on pairs.
GRAINS = ("distribution", "pair")
DIFFERENT_BILLING_CLASS = "different_billing_class"

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
    #: The carrier's distribution this rate was compared against (ADR 0006).
    #: ``payer_rate`` is its median. Defaults describe a pair-grain row.
    payer_min: float = 0.0
    payer_max: float = 0.0
    payer_count: int = 1
    inside_payer_range: bool = False

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
class _RateState:
    """One facility x carrier x code within one slice, while it is being read."""

    hospital: list[float] = field(default_factory=list)
    compared: int = 0
    inside: int = 0
    payer_count: int = 0
    mins: list[float] = field(default_factory=list)
    medians: list[float] = field(default_factory=list)
    maxs: list[float] = field(default_factory=list)
    explanations: dict[str, int] = field(default_factory=dict)
    refusals: dict[str, int] = field(default_factory=dict)


#: Columns of the ``rates`` table, in order. One row per facility, carrier,
#: code and code type: what code lookup reads.
RATE_COLUMNS: tuple[tuple[str, pa.DataType], ...] = (
    ("hospital_slug", pa.string()),
    ("system", pa.string()),
    ("facility", pa.string()),
    ("carrier", pa.string()),
    ("code", pa.string()),
    ("code_type", pa.string()),
    ("hospital_rates", pa.int32()),
    ("hospital_rate", pa.float64()),
    ("compared", pa.int32()),
    ("payer_min", pa.float64()),
    ("payer_median", pa.float64()),
    ("payer_max", pa.float64()),
    ("payer_count", pa.int32()),
    ("inside_share", pa.float64()),
    ("explanation_before_offsets", pa.string()),
    ("refusal", pa.string()),
)
RATE_SCHEMA = pa.schema(list(RATE_COLUMNS))


def _mode(counts: dict[str, int]) -> str:
    return max(sorted(counts), key=lambda k: counts[k]) if counts else ""


class RateCollector:
    """Every hospital rate's outcome, rolled up to facility x carrier x code.

    Collected one slice at a time and flushed at the slice's end: a slice is
    one facility and one code shard, and the key holds both, so no key spans
    two slices and each flush is final. Memory is one slice's keys, never the
    system's -- which is what lets code lookup be built inside the same 8 GiB
    container that NYU Langone's mart runs in.

    ``explanation_before_offsets`` is named for what it is: systematic offsets
    are detected over the whole system at ``close()``, after these rows are
    final, and a row here cannot know which of its rates an offset later
    reclassifies.
    """

    def __init__(self) -> None:
        self._keys: dict[tuple[str, str, str, str], _RateState] = {}

    def _state(self, facility: str, carrier: str, code: str, code_type: str) -> _RateState:
        return self._keys.setdefault((facility, carrier, code, code_type), _RateState())

    def compared(self, row: Variance) -> None:
        state = self._state(row.left.hospital, row.payer, row.code, row.code_type or "")
        state.hospital.append(row.left_rate)
        state.compared += 1
        state.explanations[row.explanation] = state.explanations.get(row.explanation, 0) + 1
        spread = row.spread
        if spread is not None:
            state.mins.append(spread.minimum)
            state.medians.append(spread.median)
            state.maxs.append(spread.maximum)
            state.payer_count = max(state.payer_count, spread.count)
            state.inside += 1 if spread.contains(row.left_rate) else 0

    def refused(self, left: ComparableRate, reason: str) -> None:
        state = self._state(left.hospital, left.payer, left.code, left.code_type or "")
        if left.rate_dollar:
            state.hospital.append(left.rate_dollar)
        state.refusals[reason] = state.refusals.get(reason, 0) + 1

    def flush(self, carriers: frozenset[str], slug: str, system: str) -> pa.RecordBatch | None:
        """This slice's rows, for carriers the payer side holds; then forget them.

        Only carriers in the payer corpus: a hospital rate for a carrier with no
        payer file (Healthfirst, Medicare) has no insurer rate to look up, and
        code lookup is a comparison, not a listing of the hospital file.
        """
        wanted = {c.casefold() for c in carriers}
        columns: dict[str, list[Any]] = {name: [] for name, _ in RATE_COLUMNS}
        for (facility, carrier, code, code_type), state in sorted(self._keys.items()):
            if carrier.casefold() not in wanted:
                continue
            values = {
                "hospital_slug": slug,
                "system": system,
                "facility": facility,
                "carrier": carrier,
                "code": code,
                "code_type": code_type,
                "hospital_rates": len(state.hospital) or sum(state.refusals.values()),
                "hospital_rate": median(state.hospital) if state.hospital else None,
                "compared": state.compared,
                "payer_min": min(state.mins) if state.mins else None,
                "payer_median": median(state.medians) if state.medians else None,
                "payer_max": max(state.maxs) if state.maxs else None,
                "payer_count": state.payer_count,
                "inside_share": state.inside / state.compared if state.compared else None,
                "explanation_before_offsets": _mode(state.explanations),
                "refusal": "" if state.compared else _mode(state.refusals),
            }
            for name, value in values.items():
                columns[name].append(value)
        self._keys.clear()
        if not columns["facility"]:
            return None
        return pa.RecordBatch.from_pydict(columns, schema=RATE_SCHEMA)


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
    excluded_detail: dict[tuple[str, str, str, str], int] = field(default_factory=dict)
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
    outcomes: dict[tuple[str, str, str, str], list[int]] = field(default_factory=dict)
    _contracts: dict[ContractKey, _Contract] = field(default_factory=dict)
    #: Per facility x carrier: every compared rate's signed gap (payer median over
    #: hospital, minus one) and how many sat inside the payer's range. One float a
    #: comparison, for the rankings' median gap and inside share.
    _pair_gaps: dict[tuple[str, str], array[float]] = field(default_factory=dict)
    _pair_inside: dict[tuple[str, str], int] = field(default_factory=dict)
    _rates: RateCollector = field(default_factory=RateCollector)
    rate_batches: list[pa.RecordBatch] = field(default_factory=list)
    #: (code_type, code) -> description -> rows, capped at the top three per code.
    descriptions: dict[tuple[str, str], dict[str, int]] = field(default_factory=dict)
    _closed: bool = False

    @property
    def material(self) -> int:
        return self.explanation.get("_material", 0)

    @property
    def candidates(self) -> int:
        return self.pairs_formed + sum(self.excluded.values())

    @property
    def comparable_share(self) -> float:
        """Compared, over every candidate: the raw share."""
        return self.pairs_formed / self.candidates if self.candidates else 0.0

    @property
    def like_class_candidates(self) -> int:
        """Candidates less those whose counterparts were all the other billing class."""
        return self.candidates - self.excluded.get(DIFFERENT_BILLING_CLASS, 0)

    @property
    def like_class_share(self) -> float:
        """Compared, over like-class candidates. Reported beside the raw share (ADR 0005)."""
        total = self.like_class_candidates
        return self.pairs_formed / total if total else 0.0

    def add_shard(
        self,
        shard: str,
        mart: VarianceMart,
        *,
        hospital_rates: int = 0,
        payer_rates: int = 0,
        rows: Iterable[Variance] | None = None,
        payer_carriers: frozenset[str] | None = None,
    ) -> int:
        """Fold one shard's pairs in, keeping only what survives the whole run.

        Deliberately called with a mart whose offsets have **not** been applied:
        doing that per shard is the thing this class exists to avoid.

        ``rows`` is the pair stream. Given one, the pairs are never all in
        memory at once -- which is the difference between fitting a container
        and not, because one NYU Langone slice forms three million of them
        against a quarter-million inputs. Omitted, ``mart.rows`` is used, which
        is what a caller holding the list already wants.

        **The exclusion counts are read after the stream, not before.** A
        generator fills them as it runs, so reading them first would report
        plausible, smaller numbers and nothing would look wrong.
        """
        if self._closed:
            raise RuntimeError("cannot add a shard after close(); the offsets are already fixed")
        self.shards.append(shard)
        self.hospital_rates += hospital_rates
        self.payer_rates += payer_rates

        pairs = 0
        for row in mart.rows if rows is None else rows:
            pairs += 1
            self.explanation[row.explanation] = self.explanation.get(row.explanation, 0) + 1
            if row.is_material:
                self.explanation["_material"] = self.explanation.get("_material", 0) + 1
            self.facilities.add(row.left.hospital)

            bucket = self.outcomes.setdefault(
                (row.left.hospital, row.payer, row.code_type or "", row.explanation), [0, 0]
            )
            bucket[0] += 1
            bucket[1] += 1 if row.is_material else 0

            pair = (row.left.hospital, row.payer)
            if row.ratio > 0:
                self._pair_gaps.setdefault(pair, array("d")).append(row.ratio - 1.0)
            if row.spread is not None and row.spread.contains(row.left_rate):
                self._pair_inside[pair] = self._pair_inside.get(pair, 0) + 1
            if payer_carriers is not None:
                self._rates.compared(row)

            # The payer side of the contract is the row's payer label -- one
            # network, or every network a distribution spans -- so it is the
            # same string the residual row carries, and close() matches them.
            key: ContractKey = (
                row.left.hospital,
                row.payer,
                row.left.plan,
                row.payer_label or None,
            )
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

        # After the loop: a streamed mart is only fully counted once exhausted.
        if payer_carriers is not None:
            batch = self._rates.flush(payer_carriers, self.hospital_slug, self.system)
            if batch is not None:
                self.rate_batches.append(batch)
        self.pairs_formed += pairs
        for reason, count in mart.excluded.items():
            self.excluded[reason] = self.excluded.get(reason, 0) + count
        for detail_key, detail_count in mart.excluded_detail.items():
            self.excluded_detail[detail_key] = (
                self.excluded_detail.get(detail_key, 0) + detail_count
            )
        for note in mart.provenance.caveats:
            if note not in self.caveats:
                self.caveats.append(note)
        return pairs

    def record_refusal(self, left: ComparableRate, reason: str) -> None:
        """Hand a refused hospital rate to code lookup's collector."""
        self._rates.refused(left, reason)

    def add_descriptions(self, table: pa.Table) -> None:
        """Fold in (code_type, code, description, rows) counts from a shard scan.

        Kept to the three most common descriptions per code, so the store is
        bounded by codes rather than by how many ways a hospital spells one.
        """
        for code_type, code, text, rows in zip(
            table.column("code_type").to_pylist(),
            table.column("code").to_pylist(),
            table.column("description").to_pylist(),
            table.column("rows").to_pylist(),
            strict=True,
        ):
            if not text:
                continue
            counts = self.descriptions.setdefault((code_type or "", code or ""), {})
            counts[text] = counts.get(text, 0) + int(rows)
            if len(counts) > 3:
                for weakest in sorted(counts, key=counts.__getitem__)[:-3]:
                    del counts[weakest]

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
                self._reclassify(row.facility, row.carrier, row.code_type, material=True)
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
                    self._reclassify(key[0], key[1], code_type, material=False)

        if moved:
            self.explanation[UNEXPLAINED] = self.explanation.get(UNEXPLAINED, 0) - moved
            self.explanation[SYSTEMATIC_OFFSET] = self.explanation.get(SYSTEMATIC_OFFSET, 0) + moved
        self._contracts.clear()
        self._closed = True

    def _reclassify(self, facility: str, carrier: str, code_type: str, *, material: bool) -> None:
        """Move one pair from unexplained to systematic_offset at outcome grain."""
        source = self.outcomes.get((facility, carrier, code_type, UNEXPLAINED))
        if source is not None:
            source[0] -= 1
            source[1] -= 1 if material else 0
        target = self.outcomes.setdefault((facility, carrier, code_type, SYSTEMATIC_OFFSET), [0, 0])
        target[0] += 1
        target[1] += 1 if material else 0

    def outcome_rows(self) -> list[dict[str, Any]]:
        """Pairs by carrier, code type and explanation -- what the filters act on."""
        self._require_closed("outcome_rows")
        return [
            {
                "hospital_slug": self.hospital_slug,
                "system": self.system,
                "facility": facility,
                "carrier": carrier,
                "code_type": code_type,
                "explanation": explanation,
                "pairs": pairs,
                "material_pairs": material,
            }
            for (facility, carrier, code_type, explanation), (pairs, material) in sorted(
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
            "candidates": self.candidates,
            "pairs_formed": self.pairs_formed,
            "comparable_share": round(self.comparable_share, 6),
            "like_class_candidates": self.like_class_candidates,
            "like_class_share": round(self.like_class_share, 6),
            "material": self.material,
            "unexplained_and_material": len(self.residual),
            "facilities": len(self.facilities),
            "carriers": len({carrier for _, carrier, _, _ in self.outcomes}),
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
                "facility": facility,
                "carrier": carrier,
                "code_type": code_type,
                "candidates": count,
            }
            for (reason, carrier, code_type, facility), count in sorted(
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

    def pair_rows(self) -> list[dict[str, Any]]:
        """One row per facility x carrier: what the rankings page ranks.

        ``summed_abs_gap_usd`` adds price differences across codes, and the files
        carry no volumes, so it is not money at stake. The page says so beside it.
        """
        self._require_closed("pair_rows")
        compared: dict[tuple[str, str], int] = {}
        material: dict[tuple[str, str], int] = {}
        for (facility, carrier, _, _), (pairs, mat) in self.outcomes.items():
            compared[(facility, carrier)] = compared.get((facility, carrier), 0) + pairs
            material[(facility, carrier)] = material.get((facility, carrier), 0) + mat
        refused: dict[tuple[str, str], int] = {}
        other_class: dict[tuple[str, str], int] = {}
        for (reason, carrier, _, facility), count in self.excluded_detail.items():
            if not facility:
                continue
            refused[(facility, carrier)] = refused.get((facility, carrier), 0) + count
            if reason == DIFFERENT_BILLING_CLASS:
                other_class[(facility, carrier)] = other_class.get((facility, carrier), 0) + count
        unexplained: dict[tuple[str, str], list[ResidualRow]] = {}
        for row in self.residual:
            unexplained.setdefault((row.facility, row.carrier), []).append(row)

        out = []
        for pair in sorted(set(compared) | set(refused)):
            facility, carrier = pair
            done = compared.get(pair, 0)
            total = done + refused.get(pair, 0)
            like = total - other_class.get(pair, 0)
            gaps = self._pair_gaps.get(pair)
            residual = unexplained.get(pair, [])
            out.append(
                {
                    "hospital_slug": self.hospital_slug,
                    "system": self.system,
                    "facility": facility,
                    "carrier": carrier,
                    "hospital_rates": total,
                    "compared": done,
                    "material": material.get(pair, 0),
                    "unexplained_material": len(residual),
                    "median_signed_gap": round(median(gaps), 6) if gaps else None,
                    "summed_abs_gap_usd": round(sum(abs(r.difference) for r in residual), 2),
                    "inside_range_share": round(self._pair_inside.get(pair, 0) / done, 6)
                    if done
                    else None,
                    "like_class_share": round(done / like, 6) if like else None,
                }
            )
        return out

    def residual_rows(self, per_pair: int = 100) -> list[dict[str, Any]]:
        """The widest unexplained gaps in dollars, capped per facility x carrier.

        Beside ``exemplar_rows``, not instead of it: the exemplars feed A1's
        triage queue, which is being labelled, and must not change under the
        labeller.
        """
        self._require_closed("residual_rows")
        by_pair: dict[tuple[str, str], list[ResidualRow]] = {}
        for row in self.residual:
            by_pair.setdefault((row.facility, row.carrier), []).append(row)
        chosen: list[ResidualRow] = []
        for pair in sorted(by_pair):
            ranked = sorted(by_pair[pair], key=lambda r: (-abs(r.difference), r.code))
            chosen.extend(ranked[:per_pair])
        return as_records(chosen)

    def rate_table(self) -> pa.Table:
        """Code lookup's rows, as Arrow: millions of dicts would not fit."""
        self._require_closed("rate_table")
        return pa.Table.from_batches(self.rate_batches, schema=RATE_SCHEMA)

    def code_rows(self) -> list[dict[str, Any]]:
        """Each code's most common description in this system's hospital files."""
        self._require_closed("code_rows")
        return [
            {
                "hospital_slug": self.hospital_slug,
                "code_type": code_type,
                "code": code,
                "description": max(sorted(counts), key=counts.__getitem__),
                "rows": max(counts.values()),
            }
            for (code_type, code), counts in sorted(self.descriptions.items())
            if counts
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
            ("headline", "like_class_share", round(self.like_class_share, 6)),
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
        payer_plan=row.payer_label,
        hospital_rate=row.left_rate,
        payer_rate=row.right_rate,
        difference=row.difference,
        ratio=row.ratio,
        relative_difference=row.relative_difference,
        is_implausible=row.is_implausible,
        hospital_vintage=row.left.vintage or "",
        payer_vintage=row.right.vintage or "",
        notes=" | ".join(row.notes),
        payer_min=row.spread.minimum if row.spread else row.right_rate,
        payer_max=row.spread.maximum if row.spread else row.right_rate,
        payer_count=row.spread.count if row.spread else 1,
        inside_payer_range=bool(row.spread and row.spread.contains(row.left_rate)),
    )


def stream_shard(
    hospital_side: list[ComparableRate],
    payer_side: list[ComparableRate],
    *,
    max_vintage_days: int = 400,
    assume_facility_when_unstated: frozenset[str] = frozenset(),
    grain: str = "distribution",
    on_refusal: Callable[[ComparableRate, str], None] | None = None,
) -> tuple[VarianceMart, Iterator[Variance]]:
    """An empty mart and the pair stream that fills it.

    The mart is returned first so the caller can hand both to
    :meth:`Reconciliation.add_shard`, which must consume the stream before it
    reads the mart's counts. Offsets are deliberately not applied here, for the
    same reason :func:`reconcile_shard` does not apply them.
    """
    if grain not in GRAINS:
        raise ValueError(f"unknown grain {grain!r}; expected one of {GRAINS}")
    mart = VarianceMart()
    rows: Iterator[Variance]
    if grain == "distribution":
        rows = iter_distribution_variance(
            hospital_side,
            payer_side,
            mart=mart,
            max_vintage_days=max_vintage_days,
            assume_facility_when_unstated=assume_facility_when_unstated,
            on_refusal=on_refusal,
        )
    else:
        rows = iter_cross_source_variance(
            hospital_side,
            payer_side,
            mart=mart,
            max_vintage_days=max_vintage_days,
            assume_facility_when_unstated=assume_facility_when_unstated,
        )
    return mart, rows


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
    "stream_shard",
]
