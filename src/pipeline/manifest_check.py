"""Stage 1: does what is in ADLS still match the manifest that described it?

The manifest records what a publish intended: files, rows and bytes per carrier.
This reads it back, observes what is actually in the container now, and reports
the difference. A drift here means either something republished without going
through the manifest, or something was deleted — both of which invalidate every
figure downstream, and neither of which announces itself.

Two decisions shape the output.

**Row counts come from the Parquet footers, not the blob listing.** A file can be
the right size and the wrong content; only the footer knows how many rows are in
it. It costs one metadata read per file and is the difference between checking
that bytes arrived and checking that data did.

**A mismatch exits non-zero.** A job that reports success with a bad diff in its
logs is worse than one that fails, because the logs are only read when something
already looks wrong. The execution status is the signal that gets noticed.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import pyarrow.dataset as ds
import pyarrow.fs as pafs

from pipeline.cap import OVER_QUOTA, CapProbe
from storage import Location


@dataclass
class CarrierObservation:
    carrier: str
    files: int = 0
    rows: int = 0
    bytes: int = 0


@dataclass
class ManifestDiff:
    """What the manifest promised against what the container holds."""

    ingest_date: str
    expected: CarrierObservation
    observed: CarrierObservation
    per_carrier: list[dict[str, Any]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)

    @property
    def matches(self) -> bool:
        return (
            not self.missing
            and not self.unexpected
            and self.expected.files == self.observed.files
            and self.expected.rows == self.observed.rows
            and self.expected.bytes == self.observed.bytes
            and all(row["matches"] for row in self.per_carrier)
        )


def read_manifest(location: Location, ingest_date: str) -> dict[str, Any]:
    """Load the manifest a publish wrote, from ``_meta``."""
    path = location.child("_meta", f"ingest_date={ingest_date}", "upload_manifest.json").root
    with location.filesystem.open_input_stream(path) as handle:
        # utf-8-sig: the verification file has been written by PowerShell before
        # now, which emits a BOM that json.loads will not tolerate.
        parsed: dict[str, Any] = json.loads(handle.readall().decode("utf-8-sig"))
    return parsed


def observe(location: Location, ingest_date: str) -> tuple[CarrierObservation, dict[str, Any]]:
    """Count what is actually in bronze, per carrier, from the Parquet footers."""
    root = location.child("bronze", "payer_tic", f"ingest_date={ingest_date}")
    dataset = ds.dataset(
        root.root, filesystem=root.filesystem, format="parquet", partitioning="hive"
    )
    per: dict[str, CarrierObservation] = {}
    total = CarrierObservation(carrier="*")
    names: list[str] = []
    for path in dataset.files:
        text = str(path).replace("\\", "/")
        names.append(text.rsplit("/", 1)[-1])
        carrier = next(
            (p.split("=", 1)[1] for p in text.split("/") if p.startswith("carrier=")), "?"
        )
        info = root.filesystem.get_file_info(text)
        one = ds.dataset(text, filesystem=root.filesystem, format="parquet")
        entry = per.setdefault(carrier, CarrierObservation(carrier=carrier))
        rows = one.count_rows()
        entry.files += 1
        entry.rows += rows
        entry.bytes += info.size or 0
        total.files += 1
        total.rows += rows
        total.bytes += info.size or 0
    return total, {"per_carrier": per, "names": names}


def compare(location: Location, ingest_date: str) -> ManifestDiff:
    """Read the manifest, observe the container, and diff the two."""
    manifest = read_manifest(location, ingest_date)
    uploads = manifest["uploads"]

    expected_per: dict[str, CarrierObservation] = {}
    expected_total = CarrierObservation(carrier="*")
    expected_names = set()
    for upload in uploads:
        carrier = upload["carrier"]
        entry = expected_per.setdefault(carrier, CarrierObservation(carrier=carrier))
        entry.files += 1
        entry.rows += upload["rows"]
        entry.bytes += upload["bytes"]
        expected_total.files += 1
        expected_total.rows += upload["rows"]
        expected_total.bytes += upload["bytes"]
        expected_names.add(upload["destination"].rsplit("/", 1)[-1])

    observed_total, detail = observe(location, ingest_date)
    observed_per: dict[str, CarrierObservation] = detail["per_carrier"]
    observed_names = set(detail["names"])

    rows: list[dict[str, Any]] = []
    for carrier in sorted(set(expected_per) | set(observed_per)):
        want = expected_per.get(carrier, CarrierObservation(carrier=carrier))
        got = observed_per.get(carrier, CarrierObservation(carrier=carrier))
        rows.append(
            {
                "carrier": carrier,
                "expected_files": want.files,
                "observed_files": got.files,
                "expected_rows": want.rows,
                "observed_rows": got.rows,
                "expected_bytes": want.bytes,
                "observed_bytes": got.bytes,
                "matches": (
                    want.files == got.files and want.rows == got.rows and want.bytes == got.bytes
                ),
            }
        )

    return ManifestDiff(
        ingest_date=ingest_date,
        expected=expected_total,
        observed=observed_total,
        per_carrier=rows,
        missing=sorted(expected_names - observed_names),
        unexpected=sorted(observed_names - expected_names),
    )


def latest_ingest_date(location: Location) -> str | None:
    """Newest ``ingest_date=`` under ``_meta``, or ``None`` if there is none."""
    meta = location.child("_meta")
    try:
        entries = location.filesystem.get_file_info(
            pafs.FileSelector(meta.root, recursive=False, allow_not_found=True)
        )
    except (OSError, ValueError):
        return None
    dates = sorted(
        part.split("=", 1)[1]
        for entry in entries
        for part in [str(entry.path).replace("\\", "/").rsplit("/", 1)[-1]]
        if part.startswith("ingest_date=")
    )
    return dates[-1] if dates else None


def telemetry(diff: ManifestDiff, cap: CapProbe) -> list[dict[str, Any]]:
    """One record per carrier plus a summary, as Log Analytics will see them."""
    records: list[dict[str, Any]] = []
    for row in diff.per_carrier:
        records.append({"event": "manifest_carrier", "ingest_date": diff.ingest_date, **row})
    records.append(
        {
            "event": "manifest_summary",
            "ingest_date": diff.ingest_date,
            "expected_files": diff.expected.files,
            "observed_files": diff.observed.files,
            "expected_rows": diff.expected.rows,
            "observed_rows": diff.observed.rows,
            "expected_bytes": diff.expected.bytes,
            "observed_bytes": diff.observed.bytes,
            "missing": diff.missing,
            "unexpected": diff.unexpected,
            "carriers_mismatched": [r["carrier"] for r in diff.per_carrier if not r["matches"]],
            "matches": diff.matches,
            # Carried on the summary so a capped day is visible in the same
            # record as the result it might have truncated.
            "log_ingestion_status": str(cap),
            "log_cap_hit": cap.cap_hit,
            # Why the status is unknown, when it is. An unexplained "unknown"
            # would leave log_cap_hit permanently false for an invisible reason.
            "log_probe_detail": cap.detail,
        }
    )
    return records


def counted(records: list[dict[str, Any]]) -> Counter[str]:
    return Counter(str(r["event"]) for r in records)


__all__ = [
    "OVER_QUOTA",
    "CapProbe",
    "CarrierObservation",
    "ManifestDiff",
    "compare",
    "counted",
    "latest_ingest_date",
    "observe",
    "read_manifest",
    "telemetry",
]
