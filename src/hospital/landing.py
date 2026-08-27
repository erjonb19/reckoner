"""Land curated rates, rejects and the load audit.

Writes Parquet to a local root today. ADLS Gen2 is the authoritative store per
the architecture rules, but the Fabric/Azure tenant question is still open, so
the root is a plain path and swapping it for an `abfss://` URI is a
configuration change rather than a rewrite -- pyarrow takes a filesystem object.

Two properties matter more than the storage target:

* **Atomic.** Rows are written to a staging directory and promoted only when the
  batch completes, so an interrupted download never leaves a half-loaded
  partition that looks complete.
* **Idempotent.** A batch is identified by the source URL plus a checksum of the
  decoded bytes. Re-running over an unchanged file replaces the same partition
  rather than appending a duplicate.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from hospital.curate import CuratedRate, Reject

ROWS_PER_BATCH = 50_000

RATE_SCHEMA = pa.schema(
    [
        ("batch_id", pa.string()),
        ("source_url", pa.string()),
        ("hospital", pa.string()),
        ("location_name", pa.string()),
        ("file_vintage", pa.string()),
        ("code", pa.string()),
        ("code_type", pa.string()),
        ("description", pa.string()),
        ("setting", pa.string()),
        ("billing_class", pa.string()),
        ("payer_name_raw", pa.string()),
        ("plan_name_raw", pa.string()),
        ("payer_key", pa.string()),
        ("plan_key", pa.string()),
        ("product_class", pa.string()),
        ("rate_kind", pa.string()),
        ("rate_dollar", pa.float64()),
        ("rate_percentage", pa.float64()),
        ("rate_algorithm", pa.string()),
        ("methodology", pa.string()),
        ("gross_charge", pa.float64()),
        ("discounted_cash", pa.float64()),
        ("row_hash", pa.string()),
    ]
)

REJECT_SCHEMA = pa.schema(
    [
        ("batch_id", pa.string()),
        ("source_url", pa.string()),
        ("ordinal", pa.int64()),
        ("reason", pa.string()),
        ("detail", pa.string()),
        ("payer_name", pa.string()),
        ("code", pa.string()),
    ]
)


@dataclass
class LoadAudit:
    """One row of LOAD_AUDIT. Written whether the batch succeeds or fails."""

    batch_id: str
    source_url: str
    hospital: str
    started_at: str
    finished_at: str | None = None
    file_vintage: str | None = None
    layout: str | None = None
    bytes_read: int = 0
    checksum: str | None = None
    rows_seen: int = 0
    rows_filtered: int = 0
    rows_in: int = 0
    rows_out: int = 0
    rows_rejected: int = 0
    reject_reasons: dict[str, int] | None = None
    attempts: int = 1
    status: str = "running"
    error: str | None = None

    @property
    def reject_rate(self) -> float:
        return self.rows_rejected / self.rows_in if self.rows_in else 0.0


class HashingStream:
    """Passes bytes through while accumulating a checksum and a byte count."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = chunks
        self._digest = hashlib.sha256()
        self.bytes_read = 0

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._chunks:
            self._digest.update(chunk)
            self.bytes_read += len(chunk)
            yield chunk

    @property
    def checksum(self) -> str:
        return self._digest.hexdigest()


class Landing:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.curated = root / "curated"
        self.staging = root / "_staging"
        self.audit_path = root / "_audit" / "load_audit.jsonl"

    # -- audit -----------------------------------------------------------

    def read_audit(self) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        with self.audit_path.open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def append_audit(self, audit: LoadAudit) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(audit), ensure_ascii=False) + "\n")

    def already_loaded(self, source_url: str, checksum: str) -> str | None:
        """Return the prior batch id if this exact file has been loaded."""
        for row in self.read_audit():
            if (
                row.get("source_url") == source_url
                and row.get("checksum") == checksum
                and row.get("status") == "ok"
            ):
                return str(row.get("batch_id"))
        return None

    # -- data ------------------------------------------------------------

    def stage_writer(self, batch_id: str, name: str, schema: pa.Schema) -> _StagedWriter:
        path = self.staging / batch_id / f"{name}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        return _StagedWriter(path, schema)

    def promote(
        self, batch_id: str, hospital_slug: str, vintage: str | None, source_url: str
    ) -> list[Path]:
        """Move a completed batch out of staging into the curated tree.

        The newest load of a given source file supersedes the previous one:
        files are named `<source_key>-<batch_id>.parquet`, and any earlier file
        for the same source is removed. Without this a republished MRF would sit
        alongside its own prior version and double-count every rate.
        """
        staged = self.staging / batch_id
        if not staged.exists():
            return []
        vintage_part = (vintage or "unknown")[:7]
        key = source_key(source_url)
        moved: list[Path] = []
        for source in sorted(staged.glob("*.parquet")):
            # Partition key is `hospital_slug`, not `hospital`: a Hive partition
            # key that shares a name with a column in the file makes the whole
            # dataset unreadable ("incompatible types: string vs dictionary"),
            # because pyarrow reconstructs the key as a dictionary column and
            # then cannot merge it with the real one.
            target_dir = (
                self.curated
                / f"hospital_{source.stem}"
                / f"hospital_slug={hospital_slug}"
                / f"vintage={vintage_part}"
            )
            target_dir.mkdir(parents=True, exist_ok=True)
            for superseded in target_dir.glob(f"{key}-*.parquet"):
                superseded.unlink()
            target = target_dir / f"{key}-{batch_id}.parquet"
            shutil.move(str(source), str(target))
            moved.append(target)
        shutil.rmtree(staged, ignore_errors=True)
        return moved

    def discard(self, batch_id: str) -> None:
        shutil.rmtree(self.staging / batch_id, ignore_errors=True)

    def sweep_staging(self) -> list[str]:
        """Remove staged batches orphaned by a killed run.

        A process killed mid-batch cannot run its own cleanup, so staging
        accumulates directories that will never be promoted. They are invisible
        to readers, but they are not free -- one interrupted pass over this
        corpus left hundreds of MB behind.
        """
        if not self.staging.exists():
            return []
        orphans = [path.name for path in self.staging.iterdir() if path.is_dir()]
        for name in orphans:
            shutil.rmtree(self.staging / name, ignore_errors=True)
        return orphans


class _StagedWriter:
    """Buffers rows and flushes row groups, so memory stays flat."""

    def __init__(self, path: Path, schema: pa.Schema) -> None:
        self.path = path
        self.schema = schema
        self._writer: pq.ParquetWriter | None = None
        self._buffer: list[dict[str, Any]] = []
        self.rows = 0

    def add(self, record: CuratedRate | Reject) -> None:
        self._buffer.append(asdict(record))
        self.rows += 1
        if len(self._buffer) >= ROWS_PER_BATCH:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        table = pa.Table.from_pylist(self._buffer, schema=self.schema)
        if self._writer is None:
            self._writer = pq.ParquetWriter(self.path, self.schema, compression="zstd")
        self._writer.write_table(table)
        self._buffer.clear()

    def close(self) -> None:
        self.flush()
        if self._writer is not None:
            self._writer.close()
            self._writer = None


def source_key(source_url: str) -> str:
    """Stable short identity for a source file, used to name its partition file."""
    return hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:8]


def new_batch_id(source_url: str, now: datetime | None = None) -> str:
    # Microseconds, not seconds: two loads of the same URL inside one second
    # would otherwise share a batch id, and the audit could no longer identify a
    # batch uniquely.
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S%f")
    return f"{stamp}-{source_key(source_url)}"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
