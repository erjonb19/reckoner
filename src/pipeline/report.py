"""Stage 3: publish gold as a summary dataset and a written report.

Gold already holds the five tables the summary dataset needs, at the grains the
report filters on. So this stage is not a second calculation -- it is a format
change, a provenance record, and a narrative. Recomputing any of it here would
give a second set of numbers with nothing to say which was right.

**CSV, not Parquet.** The dataset is committed to a public repository and read by
a page that must make no network calls. At under a megabyte compression buys
nothing, and CSV diffs in review and renders in GitHub's own UI, so a reader can
check a number without cloning anything.

**``run.json`` is the part that matters most.** The committed copy is a snapshot
of an artifact that lives in ADLS, and nothing about a stale snapshot looks
stale. Without the source vintages and the build it came from rendered where a
reader sees them, a months-old page displays as current -- which is precisely
the failure this project keeps cataloguing.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.dataset as ds

from pipeline import vintage
from pipeline.mart import GOLD_ROOT, RECONCILABLE, TABLES
from storage import Location

#: Where the summary lands: beside the layers rather than inside them, because
#: it is a published extract and not a medallion layer. Regenerable from gold at
#: any time, so it is deliberately not part of the drift check -- a manifest for
#: something reproducible from the layer next to it would be ceremony.
SUMMARY_ROOT = ("summary",)

#: The report's own home in the repository.
REPORT_PATH = ("docs", "reconciliation-report.md")

#: Every row in every table here derives from hospital and payer files published
#: under two federal price transparency rules. Both are public by law. Nothing
#: in this dataset is a patient, an encounter, or a claim.
PROVENANCE_CAVEAT = (
    "All summary data derives from public hospital (45 CFR 180) and payer "
    "(Transparency in Coverage) price transparency files. No PHI."
)


@dataclass(frozen=True)
class Summary:
    """The published dataset: five tables plus the record of where they came from."""

    tables: dict[str, list[dict[str, Any]]]
    metadata: dict[str, Any]

    def rows(self) -> dict[str, int]:
        return {name: len(records) for name, records in self.tables.items()}


def read_gold(lake: Location) -> dict[str, list[dict[str, Any]]]:
    """Every gold table, as records.

    A missing table is an empty list rather than an error: gold is written per
    system, so a partially published tree is a state this can legitimately meet
    and should describe rather than refuse.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for name in TABLES:
        target = lake.child(*GOLD_ROOT, name)
        if not target.exists():
            out[name] = []
            continue
        out[name] = _read_unified(target).to_pylist()
    return out


def _read_unified(target: Location) -> pa.Table:
    """One gold table across every partition, with every partition's columns.

    ``ds.dataset`` takes its schema from the first file it opens and silently
    drops any column a later file adds. Gold is written a system at a time, by
    whichever image was current, so partitions legitimately differ: systems
    rebuilt after refusals gained a carrier had it, systems not yet rebuilt did
    not, and reading them together dropped the column for all of them. The
    union keeps it, with nulls where a partition predates it.
    """
    dataset = ds.dataset(
        target.root, filesystem=target.filesystem, format="parquet", partitioning="hive"
    )
    schemas = [fragment.physical_schema for fragment in dataset.get_fragments()]
    if not schemas:
        return dataset.to_table()
    unified = pa.unify_schemas([*schemas, dataset.partitioning.schema])
    return ds.dataset(
        target.root,
        schema=unified,
        filesystem=target.filesystem,
        format="parquet",
        partitioning=dataset.partitioning,
    ).to_table()


def _manifest_field(lake: Location, path: tuple[str, ...], field: str) -> str:
    """One field from one manifest, or empty. Never raises: this is provenance."""
    try:
        with lake.filesystem.open_input_stream(lake.child(*path).root) as handle:
            return str(json.loads(handle.readall().decode("utf-8-sig")).get(field, ""))
    except Exception:
        return ""


def metadata(
    lake: Location,
    tables: dict[str, list[dict[str, Any]]],
    *,
    build_sha: str = "",
    caveats: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Where this came from, and what a reader should not assume about it."""
    coverage = tables.get("coverage", [])
    assumed = sorted(
        row["hospital_slug"] for row in coverage if row.get("assumed_facility_when_unstated")
    )
    return {
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "build_sha": build_sha,
        "bronze_ingest_date": _manifest_field(
            lake, ("_meta", "ingest_date=2026-09-13", "upload_manifest.json"), "ingest_date"
        ),
        "silver_hospital_published_at": _manifest_field(
            lake, ("_meta", "silver", "hospital_rates", "upload_manifest.json"), "published_at"
        ),
        "silver_payer_published_at": _manifest_field(
            lake, ("_meta", "silver", "payer_rates", "upload_manifest.json"), "published_at"
        ),
        "gold_published_at": _manifest_field(
            lake, ("_meta", "gold", "upload_manifest.json"), "published_at"
        ),
        "systems": [row["system"] for row in coverage],
        "systems_expected": [spec.system for spec in RECONCILABLE],
        "code_types": ["CPT", "HCPCS", "MS-DRG"],
        "max_vintage_days": 400,
        "assume_facility_when_unstated": assumed,
        "caveats": [PROVENANCE_CAVEAT, *caveats],
        "rows_per_table": {name: len(records) for name, records in tables.items()},
    }


def build(lake: Location, *, build_sha: str = "", caveats: tuple[str, ...] = ()) -> Summary:
    tables = read_gold(lake)
    # Derived here rather than in the mart: it is a reading of the pairs that
    # formed, not a new measurement, so it costs one pass over a table already
    # in hand and cannot disagree with the rows it describes.
    tables["vintage_alignment"] = vintage.alignment(tables.get("exemplars", []))
    return Summary(
        tables=tables, metadata=metadata(lake, tables, build_sha=build_sha, caveats=caveats)
    )


def as_csv(records: list[dict[str, Any]]) -> str:
    """One table as CSV text, with a stable column order.

    Columns come from the first record rather than a hardcoded list, so a column
    added in gold reaches the page without an edit here -- and sorted rows keep
    the committed diff to what actually changed instead of to row ordering.
    """
    if not records:
        return ""
    columns = list(records[0])
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for record in sorted(records, key=lambda r: [str(r.get(c, "")) for c in columns]):
        writer.writerow({c: record.get(c, "") for c in columns})
    return buffer.getvalue()


def write_local(summary: Summary, directory: Path) -> list[Path]:
    """Write the dataset where the repository and the page can read it."""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, records in summary.tables.items():
        path = directory / f"{name}.csv"
        path.write_text(as_csv(records), encoding="utf-8")
        written.append(path)
    run = directory / "run.json"
    run.write_text(json.dumps(summary.metadata, indent=1) + "\n", encoding="utf-8")
    written.append(run)
    return written


def write_remote(summary: Summary, lake: Location) -> list[str]:
    """The same bytes in ADLS, so the committed copy has a source to be a copy of."""
    root = lake.child(*SUMMARY_ROOT)
    lake.filesystem.create_dir(root.root, recursive=True)
    written = []
    for name, records in summary.tables.items():
        target = root.child(f"{name}.csv")
        with lake.filesystem.open_output_stream(target.root) as handle:
            handle.write(as_csv(records).encode("utf-8"))
        written.append(target.root)
    target = root.child("run.json")
    with lake.filesystem.open_output_stream(target.root) as handle:
        handle.write((json.dumps(summary.metadata, indent=1) + "\n").encode("utf-8"))
    written.append(target.root)
    return written


def _fmt(value: object) -> str:
    return f"{value:,}" if isinstance(value, int) else str(value)


def markdown(summary: Summary) -> str:
    """The written report: what reconciles, what refuses, and the widest gaps."""
    coverage = sorted(summary.tables.get("coverage", []), key=lambda r: -r["pairs_formed"])
    refusals = summary.tables.get("refusals", [])
    magnitude = summary.tables.get("magnitude", [])
    exemplars = summary.tables.get("exemplars", [])
    meta = summary.metadata

    lines = [
        "# Reconciliation report",
        "",
        "Generated by the `report` stage from the gold layer. Every figure is",
        "measured; none is entered by hand. Regenerate with",
        "`python -m reckoner_job --stage report`.",
        "",
        f"- **Built** {meta['built_at']}",
        f"- **Bronze ingest** {meta['bronze_ingest_date'] or 'unknown'}",
        f"- **Silver published** hospital {meta['silver_hospital_published_at'] or 'unknown'},"
        f" payer {meta['silver_payer_published_at'] or 'unknown'}",
        f"- **Gold published** {meta['gold_published_at'] or 'unknown'}",
        "",
        "## Caveats",
        "",
    ]
    lines += [f"- {caveat}" for caveat in meta["caveats"]]
    lines += [
        "",
        "## Coverage",
        "",
        "**Read the shares, not the counts.**",
        "",
        "Each hospital rate is compared once, against the carrier's distribution of",
        "comparable rates for the same code, facility, setting and billing class",
        "(ADR 0006). **Compared** counts those hospital rates.",
        "",
        "Two shares, side by side for this release (ADR 0005). The **raw** share is",
        "over every hospital rate. The **like-class** share leaves out rates whose",
        "only counterparts were the other billing class: a facility charge whose",
        "insurer publishes only the professional fee has nothing to be compared to.",
        "",
        "| system | hospital rates | compared | raw share | like-class share |"
        " material | residual | offsets |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in coverage:
        like = row.get("like_class_share")
        lines.append(
            f"| {row['system']} | {_fmt(row['hospital_rates'])} "
            f"| {_fmt(row['pairs_formed'])} | {row['comparable_share']:.2%} "
            f"| {'—' if like in (None, '') else f'{like:.2%}'} "
            f"| {_fmt(row['material'])} | {_fmt(row['unexplained_and_material'])} "
            f"| {_fmt(row['systematic_offsets'])} |"
        )

    lines += [
        "",
        "**Residual** is the finding: pairs that are material *and* unexplained after",
        "every deterministic rule. **Offsets** are contracts where one constant ratio",
        "covers many services, which is a single fact about two base rates rather than",
        "one finding per code.",
        "",
        "## Why candidates are refused",
        "",
        "System grain: the comparability layer counts a refusal without retaining the",
        "refused candidate's carrier or code type.",
        "",
        "| system | reason | candidates |",
        "|---|---|---|",
    ]
    for row in sorted(refusals, key=lambda r: (r["system"], -r["candidates"])):
        lines.append(f"| {row['system']} | `{row['reason']}` | {_fmt(row['candidates'])} |")

    lines += [
        "",
        "## How large the surviving disagreements are",
        "",
        "| system | carrier | code type | residual | median | p90 | median $ | implausible |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in sorted(magnitude, key=lambda r: -r["residual_pairs"])[:20]:
        lines.append(
            f"| {row['system']} | {row['carrier']} | {row['code_type']} "
            f"| {_fmt(row['residual_pairs'])} | {row['median_relative_difference']:.1%} "
            f"| {row['p90_relative_difference']:.1%} | ${row['median_abs_difference_usd']:,.0f} "
            f"| {_fmt(row['implausible_pairs'])} |"
        )

    lines += [
        "",
        "## Widest disagreements",
        "",
        "Capped per system and carrier. A rate ten times another for the same service",
        "and payer is flagged implausible: that is more likely a unit or methodology",
        "mismatch than a negotiated difference.",
        "",
        "| system | facility | carrier | code | hospital | payer | difference |",
        "|---|---|---|---|---|---|---|",
    ]
    widest = sorted(exemplars, key=lambda r: -r["relative_difference"])[:25]
    for row in widest:
        flag = " ⚠" if row.get("is_implausible") else ""
        lines.append(
            f"| {row['system']} | {row['facility']} | {row['carrier']} "
            f"| `{row['code']}` | ${row['hospital_rate']:,.0f} | ${row['payer_rate']:,.0f} "
            f"| {row['relative_difference']:+.0%}{flag} |"
        )

    aligned = summary.tables.get("vintage_alignment", [])
    if aligned:
        headline = vintage.summarise(summary.tables.get("exemplars", []))
        lines += [
            "",
            "## How far apart in time the two sides are",
            "",
            "Vintage mismatch is structural: hospital files update at least annually,",
            "payer files monthly. A variance may be a timing artifact rather than a",
            f"disagreement. Pairs more than {vintage.MAX_VINTAGE_DAYS} days apart are refused",
            "before they reach here, so `beyond limit` should read zero — a non-zero",
            "count means something got through that should not have.",
            "",
            f"Across all pairs: median **{headline['median_gap_days']} days**, "
            f"p90 {headline['p90_gap_days']}, max {headline['max_gap_days']}, "
            f"{headline['unknown_vintage_pairs']:,} with a vintage missing on one side.",
            "",
            "| system | carrier | pairs | median | p90 | max | unknown | beyond limit |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for row in sorted(aligned, key=lambda r: -r["pairs"])[:20]:
            lines.append(
                f"| {row['system']} | {row['carrier']} | {_fmt(row['pairs'])} "
                f"| {row['median_gap_days']} | {row['p90_gap_days']} | {row['max_gap_days']} "
                f"| {_fmt(row['unknown_vintage_pairs'])} | {_fmt(row['beyond_limit'])} |"
            )

    lines += [
        "",
        "## Tables",
        "",
        "The same numbers as CSV, in `summary/`, which is what the published page reads:",
        "",
    ]
    lines += [f"- `summary/{name}.csv` — {_fmt(n)} rows" for name, n in summary.rows().items()]
    lines += ["- `summary/run.json` — provenance and caveats", ""]
    return "\n".join(lines)


__all__ = [
    "PROVENANCE_CAVEAT",
    "REPORT_PATH",
    "SUMMARY_ROOT",
    "Summary",
    "as_csv",
    "build",
    "markdown",
    "metadata",
    "read_gold",
    "write_local",
    "write_remote",
]
