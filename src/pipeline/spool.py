"""Copy the silver a mart run needs to local disk once, then scan the copy.

The mart scans silver dozens of times per system: once per code shard for the
hospital side, and once per shard and carrier for the payer side. Against ADLS
every scan is a run of small ranged reads, footers and column chunks, and each
is a billed read operation. In September that came to 7.56 million of them,
$3.78, most of it on one day of rebuilds.

A local copy changes where the bytes come from and nothing else. The files are
the same files, opened with the remote dataset's own schema, so every scan sees
the same rows in the same order and gold is byte-identical
(``scripts/measure_mart_reads.py`` proves it on a system). The copy is made in
large sequential chunks, so each file costs a handful of reads, not one per
column chunk per shard.

Disk is the constraint, and it is checked rather than assumed. A Container Apps
replica over 1 vCPU has 8 GiB of ephemeral storage. The largest reconciled
system's partition is about 2.1 GB and payer silver about 0.44 GB, and a
system's copy is removed before the next is made. When the space is not there,
the mart reads ADLS directly, as it did before, and says so.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pyarrow.dataset as ds
import pyarrow.fs as pafs

from storage import Location

#: Bytes per read when copying. Large on purpose: the whole point is few reads.
CHUNK_BYTES = 64 * 2**20

#: Free space required beyond the copy itself, as a fraction of it.
HEADROOM = 0.2

#: Called with what happened: ``(event, **fields)``. The job logs it.
SpoolHook = Callable[..., None]


def remote_bytes(source: Location) -> int:
    """Total size of the files under ``source``: a listing, no data read."""
    selector = pafs.FileSelector(source.root, recursive=True)
    return sum(
        info.size or 0
        for info in source.filesystem.get_file_info(selector)
        if info.type == pafs.FileType.File
    )


def copy_down(
    source: Location,
    destination: Path,
    *,
    on_event: SpoolHook | None = None,
) -> bool:
    """Copy ``source`` to ``destination``, or return False if it would not fit.

    Nothing is half-copied: a copy that fails part way is removed, and the
    caller falls back to reading ``source`` directly.
    """
    event = on_event or (lambda *_a, **_k: None)
    needed = remote_bytes(source)
    destination.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(destination).free
    if free < needed * (1 + HEADROOM):
        event("spool_skipped", source=source.root, needed=needed, free=free)
        return False
    local = pafs.LocalFileSystem()
    try:
        pafs.copy_files(
            source.root,
            destination.as_posix(),
            source_filesystem=source.filesystem,
            destination_filesystem=local,
            chunk_size=CHUNK_BYTES,
            use_threads=True,
        )
    except Exception as exc:
        shutil.rmtree(destination, ignore_errors=True)
        event("spool_failed", source=source.root, error=f"{type(exc).__name__}: {exc}"[:300])
        return False
    event("spooled", source=source.root, bytes=needed)
    return True


def open_like(remote: ds.Dataset, source: Location, copied: Path, base: Path) -> ds.Dataset:
    """A local dataset that reads exactly as ``remote`` does.

    ``copied`` holds a copy of ``source``, one directory of ``remote``; ``base``
    is the local directory standing where ``remote``'s root stands, so the hive
    partition keys parse the same. Two things are taken from ``remote`` rather
    than rediscovered locally:

    - **The file order.** A scan returns fragments in the dataset's file order,
      and some gold tables keep that order. ADLS lists files one way and a local
      disk another, and on the first measurement the spooled ``rates`` table
      held the same 50 rows as the direct one in a different order. So the
      local dataset is built from ``remote.files``, in that order, mapped onto
      the copies.
    - **The schema.** ``ds.dataset`` takes it from the first file it opens, and
      a subset of the files could open a different first file.
    """
    prefix = _posix(source.root).rstrip("/") + "/"
    paths = [
        (copied / _posix(name).removeprefix(prefix)).as_posix()
        for name in remote.files
        if _posix(name).startswith(prefix)
    ]
    # Every copied file must be one the remote dataset reads, and the other way
    # round. A mapping that matched nothing once produced an empty dataset, and
    # an empty dataset reconciles to an empty gold that looks like a result.
    on_disk = sorted(p.as_posix() for p in copied.rglob("*.parquet"))
    if not paths or sorted(paths) != on_disk:
        raise SpoolMismatch(
            f"{len(paths)} files mapped from {source.root}, {len(on_disk)} copied to {copied}"
        )
    return ds.dataset(
        paths,
        schema=remote.schema,
        format="parquet",
        partitioning="hive",
        partition_base_dir=base.as_posix(),
    )


class SpoolMismatch(RuntimeError):
    """The copy and the dataset it stands in for do not hold the same files."""


def _posix(path: str) -> str:
    return path.replace("\\", "/")


def only_in_partition(dataset: ds.Dataset, hospital: str, slug: str) -> bool:
    """Whether every row for ``hospital`` lives under ``hospital_slug=slug``.

    Spooling one system's partition is exact only if no row for it sits in
    another partition. Row-group statistics answer this from footers alone:
    each file holds one hospital, so no rows are read.
    """
    where = (ds.field("hospital") == hospital) & (ds.field("hospital_slug") != slug)
    return int(dataset.count_rows(filter=where)) == 0


def remove(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


__all__ = [
    "CHUNK_BYTES",
    "HEADROOM",
    "SpoolHook",
    "SpoolMismatch",
    "copy_down",
    "only_in_partition",
    "open_like",
    "remote_bytes",
    "remove",
]
