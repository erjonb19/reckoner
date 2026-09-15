"""Copy a curated slice to wherever the seam points, one system at a time.

ADR 0001 calls the shape "local parse then cloud load". The parse and the local
landing exist; this is the load, and until now nothing implemented it -- the
seam from ADR 0002 could only read.

Three things it does deliberately.

**It streams.** ``ds.write_dataset`` is given a scanner rather than a table, so
rows go out in batches and Northwell's 89 million never exist at once. That is
the same mistake this project has made three times in different layers, and it
is cheaper to not make it a fourth time than to fix it later.

**It re-derives the partition rather than copying the tree.** The local lake
carries partition paths written before #13, where a US-format date was sliced
mid-field and its slashes read as directory separators: Catholic Health sits
under ``vintage=2/25/20``. The ``file_vintage`` column was never wrong, only the
key, so publishing recomputes the key with :func:`partition_vintage` and the
published copy comes out clean. Copying bytes would have carried the defect
into the authoritative store, where it is far more expensive to correct.

**It verifies by reading back.** A write that reports success and lands a
different number of rows is the failure worth catching, and the only way to
catch it is to count what arrived. The check is a row count, not a checksum:
Parquet written by a different writer version is not byte-identical to its
source even when the data is.

Nothing here is Fabric-specific, and nothing here is cloud-specific either --
the destination is a :class:`storage.Location`, which is local unless configured
otherwise. That is what lets the whole path be proven on this machine before an
account exists.

    python -m storage.publish --hospital "Crouse Health" --to out/published
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.dataset as ds

from hospital.landing import partition_vintage
from reconcile.curated import open_curated
from storage import Location, local, resolve

#: What the published copy is partitioned by. Hospital first because every
#: query starts by naming a system; ``code_type`` second because it is the
#: sharpest filter in the lake -- CDM is 118 of the 156 million rows and almost
#: no cross-source question wants it, so keying on it lets a reconciliation read
#: 38M rows instead of 156M; vintage last because that is what a freshness
#: question filters on. The three together give 73 partitions at a median of
#: 56,016 rows, which is neither one giant file nor a directory of scraps.
PARTITION_KEYS = ("hospital_slug", "code_type", "vintage")

#: The partition keys this module *computes* rather than reads. ``code_type`` is
#: absent deliberately: it is a real column in the source, and dropping it from
#: the projection the way the other two are dropped would remove the values the
#: partitioning is built from. ``write_dataset`` still lifts it out of the file
#: payload and into the path, and hive partitioning puts it back on read.
RECOMPUTED_KEYS = ("hospital_slug", "vintage")

#: Rows per output file. The largest partition holds 84,862,576 rows, which in
#: one file is a multi-gigabyte object that no reader can split. Capping the
#: file rather than the partition keeps the layout intact -- the instruction was
#: not to trade partitioning away for footprint -- while leaving each file in a
#: range a single worker can take.
MAX_ROWS_PER_FILE = 5_000_000

#: The codec the curated lake already uses. ``write_dataset`` defaults to snappy,
#: and taking that default published silver at 6.2 GB against a 3.5 GB source --
#: not a partitioning cost, just a weaker codec applied to the same rows. Silver
#: is read far more often than it is written and lives on metered storage, so the
#: slower compress is paid once and the smaller read is earned every time.
COMPRESSION = "zstd"

#: Rows per batch out of the scanner. Small enough that a batch is cheap to
#: hold, large enough that a 89M-row system is not written in a million pieces.
BATCH_ROWS = 100_000


@dataclass(frozen=True)
class PublishResult:
    """What was written, and whether reading it back agrees."""

    hospital: str
    destination: str
    rows_read: int
    rows_written: int
    partitions: int

    @property
    def verified(self) -> bool:
        return self.rows_read == self.rows_written and self.rows_read > 0

    def describe(self) -> str:
        state = "verified" if self.verified else "MISMATCH"
        return (
            f"{self.hospital} -> {self.destination}: {self.rows_written:,} rows "
            f"across {self.partitions} partitions ({state})"
        )


def _slugify(value: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "unknown"


#: Columns normalised to lower case on the way into silver. The lake carries
#: both ``Facility`` (5,528,886 rows, NYC Health + Hospitals) and ``facility``
#: (5,049,845, everyone else) for the same thing. The comparability layer
#: casefolds so this is not a correctness bug there, but anything doing an exact
#: match -- a Spark ``GROUP BY``, a Power BI slicer -- would split one value in
#: two. Bronze keeps what the hospital published; silver is the conformed copy,
#: and this is what conformed means.
_LOWERCASED = ("billing_class",)


def _with_clean_partition(batch: pa.RecordBatch, slug: str) -> pa.RecordBatch:
    """Attach the partition columns, recomputing vintage and conforming case."""
    vintages = [partition_vintage(v) for v in batch.column("file_vintage").to_pylist()]
    table = pa.Table.from_batches([batch])
    for name in _LOWERCASED:
        if name in table.schema.names:
            index = table.schema.get_field_index(name)
            folded = [
                (v.strip().casefold() or None) if isinstance(v, str) else v
                for v in table.column(name).to_pylist()
            ]
            table = table.set_column(index, name, pa.array(folded, pa.string()))
    table = table.append_column("hospital_slug", pa.array([slug] * batch.num_rows, pa.string()))
    table = table.append_column("vintage", pa.array(vintages, pa.string()))
    return table


def publish_hospital(
    root: Path,
    hospital: str,
    destination: Location,
    *,
    source: Location | None = None,
) -> PublishResult:
    """Write one system's curated rows to ``destination`` and verify the count."""
    dataset = open_curated(root, location=source)
    where = ds.field("hospital") == hospital
    rows_read = dataset.count_rows(filter=where)
    if rows_read == 0:
        raise ValueError(f"no curated rows for {hospital!r}; nothing to publish")

    slug = _slugify(hospital)
    # Written straight into the destination, not a per-system subdirectory: the
    # hive partition already creates `hospital_slug=`, so nesting would repeat
    # it and give every system its own little lake instead of one shared tree.
    target = destination

    # Read columns explicitly rather than "everything": the source carries the
    # partition-derived `hospital_slug` and `vintage`, and re-adding them would
    # collide with the clean ones computed here.
    columns = [n for n in dataset.schema.names if n not in RECOMPUTED_KEYS]
    scanner = dataset.scanner(columns=columns, filter=where, batch_size=BATCH_ROWS)

    # Built from the projection rather than by consuming a batch: taking one to
    # learn the schema would either scan twice or drop the row it looked at.
    schema = scanner.projected_schema.append(pa.field("hospital_slug", pa.string())).append(
        pa.field("vintage", pa.string())
    )

    # write_dataset does not object to a partition field the schema lacks -- it
    # quietly writes a shallower tree and reports success. A silver copy keyed
    # on two of three columns is not a smaller mistake than a failed write; it
    # is the same wrong layout with nothing to say so. Checked here instead.
    absent = [key for key in PARTITION_KEYS if key not in schema.names]
    if absent:
        raise ValueError(
            f"cannot partition {hospital!r} on {absent}: column(s) not in the curated schema; "
            "publishing would silently land a shallower tree"
        )

    ds.write_dataset(
        _rebatched(scanner, slug),
        base_dir=target.root,
        filesystem=target.filesystem,
        format="parquet",
        partitioning=ds.partitioning(
            pa.schema(
                [
                    ("hospital_slug", pa.string()),
                    ("code_type", pa.string()),
                    ("vintage", pa.string()),
                ]
            ),
            flavor="hive",
        ),
        schema=schema,
        file_options=ds.ParquetFileFormat().make_write_options(compression=COMPRESSION),
        max_rows_per_file=MAX_ROWS_PER_FILE,
        # Must carry {i}; without it every chunk past the first overwrites the
        # one before and the write reports success having kept 5M of 85M rows.
        basename_template="part-{i}.parquet",
        existing_data_behavior="overwrite_or_ignore",
    )

    # Count only this system: the destination is a shared tree, so an unfiltered
    # count would grow with every system published and always look verified.
    written = ds.dataset(target.root, filesystem=target.filesystem, partitioning="hive")
    mine = ds.field("hospital_slug") == slug
    rows_written = written.count_rows(filter=mine)
    partitions = len(
        {str(f).rsplit("/", 1)[0] for f in written.files if f"hospital_slug={slug}/" in str(f)}
    )
    return PublishResult(
        hospital=hospital,
        destination=f"{target.describe()}/hospital_slug={slug}",
        rows_read=rows_read,
        rows_written=rows_written,
        partitions=partitions,
    )


#: Where the silver manifest lives, mirroring the data path it describes.
SILVER_MANIFEST = ("_meta", "silver", "hospital_rates", "upload_manifest.json")


def build_manifest(
    destination: Location, results: list[PublishResult], *, layer: str
) -> dict[str, Any]:
    """Describe what landed, one entry per file.

    Read back from the destination rather than predicted from the source, and
    that is worth being explicit about: it means the manifest cannot catch a bad
    write. Nothing is lost by this, because :func:`publish_hospital` already
    verifies each system's row count against the source before it returns, and
    the totals here are cross-checked against that sum below. What the manifest
    is *for* is the drift that happens afterwards -- a file deleted, truncated,
    or republished around the pipeline -- which by definition cannot be known at
    write time and which nothing else would notice.
    """
    written = ds.dataset(destination.root, filesystem=destination.filesystem, partitioning="hive")
    uploads: list[dict[str, Any]] = []
    for path in sorted(str(f).replace("\\", "/") for f in written.files):
        parts = dict(segment.split("=", 1) for segment in path.split("/") if "=" in segment)
        info = destination.filesystem.get_file_info(path)
        uploads.append(
            {
                "destination": path,
                "hospital_slug": parts.get("hospital_slug", "?"),
                "code_type": parts.get("code_type", "?"),
                "vintage": parts.get("vintage", "?"),
                "rows": ds.dataset(
                    path, filesystem=destination.filesystem, format="parquet"
                ).count_rows(),
                "bytes": info.size or 0,
            }
        )

    rows_landed = sum(u["rows"] for u in uploads)
    rows_from_source = sum(r.rows_read for r in results)
    by_slug: dict[str, int] = {}
    for upload in uploads:
        key = str(upload["hospital_slug"])
        by_slug[key] = by_slug.get(key, 0) + 1

    return {
        "layer": layer,
        # Named so a reader of the manifest knows which partition column groups
        # its entries, rather than the checker having to guess per layer.
        "group_key": "hospital_slug",
        "data_root": destination.root,
        "published_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "files": len(uploads),
        "rows": rows_landed,
        "rows_from_source": rows_from_source,
        # The one claim here that is not self-referential: the bytes and rows
        # above were read back, but this says they agree with what was asked for.
        "verified": rows_landed == rows_from_source,
        "megabytes": round(sum(u["bytes"] for u in uploads) / 1e6, 1),
        "by_hospital_slug": dict(sorted(by_slug.items())),
        "uploads": uploads,
    }


def write_manifest(root: Location, manifest: dict[str, Any]) -> str:
    """Write the manifest under ``_meta``, which is keyed off the lake root.

    ``root`` is the lake, not the destination: ``_meta`` sits beside ``silver/``
    rather than inside it, so that one place lists everything published.
    """
    target = root.child(*SILVER_MANIFEST)
    # open_output_stream does not create the parents. ADLS with a hierarchical
    # namespace needs the directories to exist, and a local filesystem raises
    # outright -- which is how this was found.
    root.filesystem.create_dir(root.child(*SILVER_MANIFEST[:-1]).root, recursive=True)
    with root.filesystem.open_output_stream(target.root) as handle:
        handle.write(json.dumps(manifest, indent=1).encode("utf-8"))
    return target.root


def _rebatched(scanner: ds.Scanner, slug: str) -> object:
    """Scanner batches with the clean partition columns attached, still streamed."""
    for batch in scanner.to_batches():
        table = _with_clean_partition(batch, slug)
        yield from table.to_batches()


#: Where hospital silver lands, relative to the lake root.
SILVER_HOSPITAL_ROOT = ("silver", "hospital_rates")

#: The free grant on a pay-as-you-go account's first 12 months: 5 GB of hot LRS
#: block blob. A target rather than a limit -- past it the rate is roughly
#: $0.02 per GB per month, so a gigabyte of overage costs less than a bus fare
#: and is not worth distorting the partition layout to avoid.
FREE_TIER_BYTES = 5_000_000_000
OVERAGE_PER_GB_MONTH = 0.02

#: Payer silver is not built yet, but its footprint is knowable now: it is the
#: conformed copy of payer bronze, which is 559,607,543 bytes across 118 files.
#: Conforming does not add rows, and compacting EmblemHealth's 98 files (20.6 MB
#: between them) into one improves the compression rather than worsening it, so
#: bronze's size is a ceiling for it rather than a guess.
PLANNED_PAYER_SILVER_BYTES = 559_607_543


def measure(location: Location) -> tuple[int, int]:
    """Files and bytes currently under ``location``. Metadata only."""
    import pyarrow.fs as pafs

    selector = pafs.FileSelector(location.root, recursive=True, allow_not_found=True)
    infos = [i for i in location.filesystem.get_file_info(selector) if i.type == pafs.FileType.File]
    return len(infos), sum(i.size or 0 for i in infos)


def local_bytes(root: Path) -> int:
    """Size of the curated lake about to be published."""
    return sum(p.stat().st_size for p in (root / "curated").rglob("*.parquet"))


def project_footprint(destination_root: Location, incoming: int) -> dict[str, Any]:
    """What the account will hold after this write, and after the work still planned.

    Reported before the write rather than after, because after is a discovery
    and before is a decision.
    """
    files, current = measure(destination_root)
    after = current + incoming
    eventual = after + PLANNED_PAYER_SILVER_BYTES
    over = max(0, eventual - FREE_TIER_BYTES)
    return {
        "current_files": files,
        "current_bytes": current,
        "incoming_bytes": incoming,
        "after_this_write_bytes": after,
        "planned_payer_silver_bytes": PLANNED_PAYER_SILVER_BYTES,
        "eventual_bytes": eventual,
        "free_tier_bytes": FREE_TIER_BYTES,
        "eventual_over_free_tier_bytes": over,
        "estimated_overage_usd_per_month": round(over / 1e9 * OVERAGE_PER_GB_MONTH, 4),
    }


def publish_all(
    root: Path, destination_root: Location, *, source: Location | None = None
) -> tuple[list[PublishResult], dict[str, Any]]:
    """Publish every system in the lake, then describe what landed.

    Systems are published one at a time rather than in a single write so that a
    failure names the system it failed on, and so the row-count verification is
    per system -- one number for 156 million rows across twelve publishers would
    tell you that something was wrong and nothing about where.
    """
    dataset = open_curated(root, location=source)
    systems = sorted(
        {h for h in dataset.to_table(columns=["hospital"]).column("hospital").to_pylist() if h}
    )
    destination = destination_root.child(*SILVER_HOSPITAL_ROOT)

    results = []
    for name in systems:
        result = publish_hospital(root, name, destination, source=source)
        print(f"  {result.describe()}", flush=True)
        if not result.verified:
            raise RuntimeError(f"row count mismatch publishing {name!r}; stopping before manifest")
        results.append(result)

    manifest = build_manifest(destination, results, layer="silver/hospital_rates")
    return results, manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("data/lake"))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--hospital", help="health system, exact name")
    group.add_argument("--all", action="store_true", help="every system, then write the manifest")
    parser.add_argument(
        "--to", type=Path, help="local destination; omit to use the configured storage"
    )
    args = parser.parse_args(argv)

    destination = local(args.to) if args.to else resolve()
    print(f"destination      : {destination.describe()}")

    if args.all:
        projection = project_footprint(destination, local_bytes(args.root))
        print("free-tier check (before the write):")
        for key, value in projection.items():
            unit = f"  ({value / 1e9:.3f} GB)" if key.endswith("_bytes") else ""
            print(f"  {key:34} {value:>15,}{unit}")
        print()

        results, manifest = publish_all(args.root, destination)
        where = write_manifest(destination, manifest)
        print(f"\nmanifest         : {where}")
        print(
            json.dumps({k: v for k, v in manifest.items() if k != "uploads"}, indent=1, default=str)
        )
        ok = bool(manifest["verified"]) and all(r.verified for r in results)
        print(f"\n{len(results)} systems, {manifest['files']} files, {manifest['rows']:,} rows")
        return 0 if ok else 1

    result = publish_hospital(args.root, args.hospital, destination.child(*SILVER_HOSPITAL_ROOT))
    print(json.dumps(result.__dict__ | {"verified": result.verified}, indent=1, default=str))
    print(f"\n{result.describe()}")
    return 0 if result.verified else 1


if __name__ == "__main__":  # pragma: no cover - entrypoint
    sys.exit(main())
