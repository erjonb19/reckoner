"""Read the curated lake into rates that can be compared.

Two things happen here, and both are joins the raw curated rows cannot do for
themselves:

* **Aggregation.** The lake holds one row per published rate line -- 13 million
  of them across four health systems. Materialising that as Python objects to
  compare them would be gratuitous, and comparing individual lines would let a
  hospital that publishes forty plan rows outweigh one that publishes two. Rows
  are collapsed in Arrow to one representative rate per facility, service, payer
  and setting before anything leaves the columnar world.

* **Entity resolution.** The join key is the contracting party, and the files
  give free text. The A2 rule-based matcher supplies the canonical name, so the
  same matcher that is scored against a labelled set is the one the pipeline
  actually uses -- an eval that scores something the pipeline does not run is
  measuring nothing.

The facility, not the health system, is the unit. ``Mount Sinai Health System``
is five hospitals with five wage indexes; collapsing them would compare a
Manhattan academic centre with a Long Island community hospital and call the
difference a price.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.dataset as ds

from agents.entity_resolution import RuleBasedMatcher, canonical_key
from hospital.facility import resolve_facility
from reconcile.comparability import ComparableRate

#: Columns the comparison needs. Reading only these keeps a 13 million row scan
#: to a few hundred MB rather than several GB.
NEEDED_COLUMNS = (
    "hospital",
    "location_name",
    "file_vintage",
    "code",
    "code_type",
    "setting",
    "billing_class",
    "payer_name_raw",
    "plan_name_raw",
    "product_class",
    "rate_kind",
    "rate_dollar",
    "methodology",
)


@dataclass(frozen=True)
class CuratedFilter:
    """Narrowing applied in Arrow, before rows are materialised."""

    hospitals: tuple[str, ...] = ()
    codes: tuple[str, ...] = ()
    product_classes: tuple[str, ...] = ()
    dollar_only: bool = True

    def expression(self) -> ds.Expression | None:
        terms: list[ds.Expression] = []
        if self.dollar_only:
            terms.append(ds.field("rate_kind") == "dollar")
            terms.append(ds.field("rate_dollar") > 0)
        if self.hospitals:
            terms.append(ds.field("hospital").isin(list(self.hospitals)))
        if self.codes:
            terms.append(ds.field("code").isin(list(self.codes)))
        if self.product_classes:
            terms.append(ds.field("product_class").isin(list(self.product_classes)))
        if not terms:
            return None
        combined = terms[0]
        for term in terms[1:]:
            combined = combined & term
        return combined


def open_curated(root: Path) -> ds.Dataset:
    """Open the curated hospital_rates dataset."""
    path = root / "curated" / "hospital_rates" if (root / "curated").exists() else root
    if not path.exists():
        raise FileNotFoundError(f"no curated dataset at {path}")
    return ds.dataset(path, partitioning="hive")


def aggregate_rates(
    dataset: ds.Dataset,
    where: CuratedFilter | None = None,
) -> pa.Table:
    """Collapse rate lines to one representative rate per facility and key.

    The median is used rather than the mean: a single mis-scaled row -- a rate
    published in cents, or a placeholder of 999999 -- moves a mean enough to
    dominate every spread it lands in, and these files contain both.
    """
    where = where or CuratedFilter()
    scanned = dataset.to_table(columns=list(NEEDED_COLUMNS), filter=where.expression())
    if scanned.num_rows == 0:
        return scanned

    keys = [
        "hospital",
        "location_name",
        "file_vintage",
        "code",
        "code_type",
        "setting",
        "billing_class",
        "payer_name_raw",
        "plan_name_raw",
        "product_class",
        "rate_kind",
    ]
    return scanned.group_by(keys).aggregate(
        [
            ("rate_dollar", "approximate_median"),
            ("rate_dollar", "count"),
            ("methodology", "min"),
        ]
    )


def to_comparable_rates(
    table: pa.Table,
    *,
    source: str = "hospital",
    canonicalise_payers: bool = True,
) -> list[ComparableRate]:
    """Turn aggregated rows into ComparableRate, keyed on the canonical payer.

    A payer the matcher cannot resolve keeps its raw string, normalised. That
    is deliberately not a drop: an unresolved payer still compares correctly
    against itself at another hospital, and dropping it would quietly shrink the
    mart to the payers that happen to be in the alias table.
    """
    if table.num_rows == 0:
        return []
    rows = table.to_pylist()
    payer_names = {str(row.get("payer_name_raw") or "") for row in rows}
    canonical = _canonical_payers(payer_names) if canonicalise_payers else {}

    rates: list[ComparableRate] = []
    for row in rows:
        raw_payer = str(row.get("payer_name_raw") or "")
        rate = row.get("rate_dollar_approximate_median")
        if rate is None:
            continue
        rates.append(
            ComparableRate(
                source=source,
                # The facility is the unit of comparison. It comes from the plan
                # suffix where a system is known to publish several hospitals in
                # one file, and from the file-level location otherwise -- see
                # hospital.facility for why trusting the file alone merged two
                # Mount Sinai hospitals under one name.
                hospital=resolve_facility(
                    _optional(row.get("hospital")),
                    _optional(row.get("location_name")),
                    _optional(row.get("plan_name_raw")),
                ),
                code=str(row.get("code") or ""),
                code_type=_optional(row.get("code_type")),
                setting=_optional(row.get("setting")),
                billing_class=_optional(row.get("billing_class")),
                payer=canonical.get(raw_payer) or canonical_key(raw_payer) or raw_payer,
                plan=_optional(row.get("plan_name_raw")),
                product_class=str(row.get("product_class") or ""),
                rate_kind=str(row.get("rate_kind") or "dollar"),
                rate_dollar=float(rate),
                methodology=_optional(row.get("methodology_min")),
                vintage=_optional(row.get("file_vintage")),
                location=_optional(row.get("location_name")),
            )
        )
    return rates


def _canonical_payers(names: set[str]) -> dict[str, str]:
    """Resolve raw payer strings to canonical parties with the A2 baseline."""
    from agents.entity_resolution import PayerCandidate

    candidates = [PayerCandidate(payer_raw=name, plan_raw="") for name in sorted(names)]
    proposals = RuleBasedMatcher().propose(candidates)
    return {
        candidate.payer_raw: proposal.canonical_payer
        for candidate, proposal in zip(candidates, proposals, strict=True)
        if proposal.canonical_payer
    }


def _optional(value: Any) -> str | None:  # noqa: ANN401 - an Arrow cell, any scalar type
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def load_comparable_rates(
    root: Path,
    where: CuratedFilter | None = None,
    *,
    canonicalise_payers: bool = True,
) -> list[ComparableRate]:
    """Read the curated lake straight to comparable rates."""
    table = aggregate_rates(open_curated(root), where)
    return to_comparable_rates(table, canonicalise_payers=canonicalise_payers)


def distinct_locations(root: Path) -> list[str]:
    """Every facility name in the lake, for the CCN crosswalk to resolve."""
    dataset = open_curated(root)
    table = dataset.to_table(columns=["location_name", "hospital", "plan_name_raw"])
    names = set()
    for location, hospital, plan in zip(
        table.column("location_name").to_pylist(),
        table.column("hospital").to_pylist(),
        table.column("plan_name_raw").to_pylist(),
        strict=True,
    ):
        name = resolve_facility(hospital, location, plan)
        if name:
            names.add(name)
    return sorted(names)


def iter_batches(
    dataset: ds.Dataset, where: CuratedFilter | None = None, batch_size: int = 100_000
) -> Iterator[pa.RecordBatch]:
    """Stream the curated dataset, for passes that must not hold it in memory."""
    where = where or CuratedFilter()
    yield from dataset.to_batches(
        columns=list(NEEDED_COLUMNS),
        filter=where.expression(),
        batch_size=batch_size,
    )


def reject_summary(root: Path) -> dict[str, int]:
    """Counts by reason from the quarantine tree, if any rows were rejected."""
    path = root / "curated" / "hospital_rejects"
    if not path.exists():
        return {}
    table = ds.dataset(path, partitioning="hive").to_table(columns=["reason"])
    grouped = table.group_by(["reason"]).aggregate([("reason", "count")])
    return dict(
        zip(
            grouped.column("reason").to_pylist(),
            [int(v) for v in grouped.column("reason_count").to_pylist()],
            strict=True,
        )
    )


def code_coverage(root: Path) -> dict[str, int]:
    """Distinct codes by code type, so a benchmark run can say what it can price."""
    table = open_curated(root).to_table(columns=["code", "code_type"])
    grouped = table.group_by(["code_type"]).aggregate([("code", "count_distinct")])
    return dict(
        zip(
            [str(v) for v in grouped.column("code_type").to_pylist()],
            [int(v) for v in grouped.column("code_count_distinct").to_pylist()],
            strict=True,
        )
    )


__all__ = [
    "NEEDED_COLUMNS",
    "CuratedFilter",
    "aggregate_rates",
    "code_coverage",
    "distinct_locations",
    "iter_batches",
    "load_comparable_rates",
    "open_curated",
    "reject_summary",
    "to_comparable_rates",
]
