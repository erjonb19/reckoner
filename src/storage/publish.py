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
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds

from hospital.landing import partition_vintage
from reconcile.curated import open_curated
from storage import Location, local, resolve

#: What the published copy is partitioned by. Hospital first because every
#: query starts by naming a system, vintage second because that is what a
#: freshness question filters on.
PARTITION_KEYS = ("hospital_slug", "vintage")

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


def _with_clean_partition(batch: pa.RecordBatch, slug: str) -> pa.RecordBatch:
    """Attach the partition columns, recomputing vintage from the raw value."""
    vintages = [partition_vintage(v) for v in batch.column("file_vintage").to_pylist()]
    table = pa.Table.from_batches([batch])
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
    columns = [n for n in dataset.schema.names if n not in PARTITION_KEYS]
    scanner = dataset.scanner(columns=columns, filter=where, batch_size=BATCH_ROWS)

    # Built from the projection rather than by consuming a batch: taking one to
    # learn the schema would either scan twice or drop the row it looked at.
    schema = scanner.projected_schema.append(pa.field("hospital_slug", pa.string())).append(
        pa.field("vintage", pa.string())
    )

    ds.write_dataset(
        _rebatched(scanner, slug),
        base_dir=target.root,
        filesystem=target.filesystem,
        format="parquet",
        partitioning=ds.partitioning(
            pa.schema([("hospital_slug", pa.string()), ("vintage", pa.string())]), flavor="hive"
        ),
        schema=schema,
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


def _rebatched(scanner: ds.Scanner, slug: str) -> object:
    """Scanner batches with the clean partition columns attached, still streamed."""
    for batch in scanner.to_batches():
        table = _with_clean_partition(batch, slug)
        yield from table.to_batches()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("data/lake"))
    parser.add_argument("--hospital", required=True, help="health system, exact name")
    parser.add_argument(
        "--to", type=Path, help="local destination; omit to use the configured storage"
    )
    args = parser.parse_args(argv)

    destination = local(args.to) if args.to else resolve()
    print(f"destination      : {destination.describe()}")

    result = publish_hospital(args.root, args.hospital, destination)
    print(json.dumps(result.__dict__ | {"verified": result.verified}, indent=1, default=str))
    print(f"\n{result.describe()}")
    return 0 if result.verified else 1


if __name__ == "__main__":  # pragma: no cover - entrypoint
    sys.exit(main())
