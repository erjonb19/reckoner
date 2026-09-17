"""Stage 1: does what is in ADLS still match the manifest that described it?

A manifest records what a publish intended: files, rows and bytes per group.
This reads it back, observes what is actually in the container now, and reports
the difference. A drift here means either something republished without going
through the manifest, or something was deleted -- both of which invalidate every
figure downstream, and neither of which announces itself.

Three decisions shape the output.

**Row counts come from the Parquet footers, not the blob listing.** A file can be
the right size and the wrong content; only the footer knows how many rows are in
it. It costs one metadata read per file and is the difference between checking
that bytes arrived and checking that data did.

**Files are identified by their path below the layer root, not their name.**
Bronze names every file after its source, so basenames happened to be unique
there. Silver does not: ``write_dataset`` names each partition's first file
``part-0.parquet``, so a set of basenames collapses 73 partitions into one entry
and a deleted partition would look like nothing at all.

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


@dataclass(frozen=True)
class Layer:
    """One published layer: where its manifest is, where its data is, how it groups.

    ``group_key`` is the partition column the per-record telemetry rolls up to --
    ``carrier`` for payer bronze, ``hospital_slug`` for hospital silver. It is
    declared per layer rather than guessed, and a manifest naming its own
    ``group_key`` overrides it, so a future layer needs no change here.
    """

    name: str
    manifest_path: tuple[str, ...]
    data_root: tuple[str, ...]
    group_key: str


def bronze_payer(ingest_date: str) -> Layer:
    """Payer TiC as landed, one directory per ingest date."""
    return Layer(
        name="bronze/payer_tic",
        manifest_path=("_meta", f"ingest_date={ingest_date}", "upload_manifest.json"),
        data_root=("bronze", "payer_tic", f"ingest_date={ingest_date}"),
        group_key="carrier",
    )


#: Hospital rates, conformed. Not keyed by ingest date: hospital files update
#: annually and are republished in place, so there is one current copy rather
#: than a series of dated ones.
SILVER_HOSPITAL = Layer(
    name="silver/hospital_rates",
    manifest_path=("_meta", "silver", "hospital_rates", "upload_manifest.json"),
    data_root=("silver", "hospital_rates"),
    group_key="hospital_slug",
)

#: Payer rates, conformed and compacted. Keyed by carrier rather than ingest
#: date: bronze keeps a dated copy of each landing, silver keeps the current
#: conformed one, so there is a single tree to check rather than a series.
SILVER_PAYER = Layer(
    name="silver/payer_rates",
    manifest_path=("_meta", "silver", "payer_rates", "upload_manifest.json"),
    data_root=("silver", "payer_rates"),
    group_key="carrier",
)


#: The reconciliation mart. One manifest covers every table under ``gold/``,
#: because they are written together by one stage and are only ever meaningful
#: together -- a residual without its denominators is not a partial answer.
GOLD = Layer(
    name="gold",
    manifest_path=("_meta", "gold", "upload_manifest.json"),
    data_root=("gold",),
    group_key="hospital_slug",
)


@dataclass
class Observation:
    """A count of files, rows and bytes, for one group or for everything."""

    group: str
    files: int = 0
    rows: int = 0
    bytes: int = 0


@dataclass
class ManifestDiff:
    """What a manifest promised against what the container holds."""

    layer: str
    ingest_date: str
    group_key: str
    expected: Observation
    observed: Observation
    per_group: list[dict[str, Any]] = field(default_factory=list)
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
            and all(row["matches"] for row in self.per_group)
        )


def _normalise(path: object) -> str:
    return str(path).replace("\\", "/")


def _below(path: object, marker: str) -> str:
    """The part of ``path`` under the layer root, which is what identifies a file.

    Manifests written from the upload side record container-relative
    destinations; a listing returns them prefixed by the container. Cutting at
    the layer root makes the two comparable without either side having to know
    how the other was produced.
    """
    text = _normalise(path)
    token = f"{marker}/"
    return text.split(token, 1)[1] if token in text else text.rsplit("/", 1)[-1]


def _group_of(path: str, group_key: str) -> str:
    for segment in path.split("/"):
        if segment.startswith(f"{group_key}="):
            return segment.split("=", 1)[1]
    return "?"


def read_manifest(location: Location, layer: Layer) -> dict[str, Any]:
    """Load the manifest a publish wrote, from ``_meta``."""
    path = location.child(*layer.manifest_path).root
    with location.filesystem.open_input_stream(path) as handle:
        # utf-8-sig: the verification file has been written by PowerShell before
        # now, which emits a BOM that json.loads will not tolerate.
        parsed: dict[str, Any] = json.loads(handle.readall().decode("utf-8-sig"))
    return parsed


def observe(location: Location, layer: Layer) -> tuple[Observation, dict[str, Any]]:
    """Count what is actually in the layer, per group, from the Parquet footers."""
    root = location.child(*layer.data_root)
    dataset = ds.dataset(
        root.root, filesystem=root.filesystem, format="parquet", partitioning="hive"
    )
    marker = "/".join(layer.data_root)
    per: dict[str, Observation] = {}
    total = Observation(group="*")
    keys: list[str] = []
    for path in dataset.files:
        text = _normalise(path)
        keys.append(_below(text, marker))
        group = _group_of(text, layer.group_key)
        info = root.filesystem.get_file_info(text)
        rows = ds.dataset(text, filesystem=root.filesystem, format="parquet").count_rows()
        entry = per.setdefault(group, Observation(group=group))
        entry.files += 1
        entry.rows += rows
        entry.bytes += info.size or 0
        total.files += 1
        total.rows += rows
        total.bytes += info.size or 0
    return total, {"per_group": per, "keys": keys}


def compare(location: Location, layer: Layer) -> ManifestDiff:
    """Read the manifest, observe the container, and diff the two."""
    manifest = read_manifest(location, layer)
    # A manifest may name its own grouping column; the layer's is the fallback,
    # which is what the bronze manifest -- written before the field existed --
    # relies on.
    group_key = str(manifest.get("group_key") or layer.group_key)
    marker = "/".join(layer.data_root)

    expected_per: dict[str, Observation] = {}
    expected_total = Observation(group="*")
    expected_keys = set()
    for upload in manifest["uploads"]:
        group = str(upload.get(group_key, "?"))
        entry = expected_per.setdefault(group, Observation(group=group))
        entry.files += 1
        entry.rows += upload["rows"]
        entry.bytes += upload["bytes"]
        expected_total.files += 1
        expected_total.rows += upload["rows"]
        expected_total.bytes += upload["bytes"]
        expected_keys.add(_below(upload["destination"], marker))

    observed_total, detail = observe(location, layer)
    observed_per: dict[str, Observation] = detail["per_group"]
    observed_keys = set(detail["keys"])

    rows: list[dict[str, Any]] = []
    for group in sorted(set(expected_per) | set(observed_per)):
        want = expected_per.get(group, Observation(group=group))
        got = observed_per.get(group, Observation(group=group))
        rows.append(
            {
                group_key: group,
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
        layer=layer.name,
        ingest_date=str(manifest.get("ingest_date") or manifest.get("published_at") or ""),
        group_key=group_key,
        expected=expected_total,
        observed=observed_total,
        per_group=rows,
        missing=sorted(expected_keys - observed_keys),
        unexpected=sorted(observed_keys - expected_keys),
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
        for part in [_normalise(entry.path).rsplit("/", 1)[-1]]
        if part.startswith("ingest_date=")
    )
    return dates[-1] if dates else None


def telemetry(diff: ManifestDiff, cap: CapProbe) -> list[dict[str, Any]]:
    """One record per group plus a summary, as Log Analytics will see them.

    Every record carries ``layer`` and ``group_key``, so two layers checked in
    one execution stay distinguishable in a query without a reader having to
    know which event name belongs to which layer.
    """
    records: list[dict[str, Any]] = []
    for row in diff.per_group:
        records.append(
            {
                "event": "manifest_group",
                "layer": diff.layer,
                "group_key": diff.group_key,
                "group": row[diff.group_key],
                "ingest_date": diff.ingest_date,
                **{k: v for k, v in row.items() if k != diff.group_key},
            }
        )
    records.append(
        {
            "event": "manifest_summary",
            "layer": diff.layer,
            "group_key": diff.group_key,
            "ingest_date": diff.ingest_date,
            "expected_files": diff.expected.files,
            "observed_files": diff.observed.files,
            "expected_rows": diff.expected.rows,
            "observed_rows": diff.observed.rows,
            "expected_bytes": diff.expected.bytes,
            "observed_bytes": diff.observed.bytes,
            "missing": diff.missing,
            "unexpected": diff.unexpected,
            "groups_mismatched": [r[diff.group_key] for r in diff.per_group if not r["matches"]],
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
    "GOLD",
    "OVER_QUOTA",
    "SILVER_HOSPITAL",
    "SILVER_PAYER",
    "CapProbe",
    "Layer",
    "ManifestDiff",
    "Observation",
    "bronze_payer",
    "compare",
    "counted",
    "latest_ingest_date",
    "observe",
    "read_manifest",
    "telemetry",
]
