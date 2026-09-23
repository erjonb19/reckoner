"""Reading both sides of the comparison out of silver, one shard at a time.

``mart_cli`` reads the hospital side from the local curated lake and the payer
side from the parser's output directory next door. Neither exists in a container,
and neither should: silver is the conformed copy and the authoritative input to
gold, so the job reads that.

**The payer converter is reused, not reimplemented.**
:func:`payer.curated.to_comparable_rates` maps a payer row onto a
``ComparableRate`` -- the TiC billing-class vocabulary, the percentage-versus-
dollar distinction that stops a 58 being subtracted from a $12,000 charge, the
setting inference, the system attribution policy. Rewriting that against silver
would mean a second copy of every one of those rules, drifting.

What it needs beyond the rows themselves is per-file metadata: the network a
stem encodes, and the file's vintage. Those came from walking a directory. Here
they are rebuilt from silver, which carries ``payer`` (the config label, equal to
the old filename stem) and ``last_updated_on`` on every row. Same objects, same
converter, different origin.

**The scan for that metadata is batched.** Two string columns over 56.8 million
rows is a couple of gigabytes if materialised, which would spend the container's
whole budget before any comparison happened. Distinct pairs are accumulated a
batch at a time and the batches are dropped.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds

from payer.curated import (
    PayerFile,
    PayerFilter,
    split_label,
)
from payer.curated import (
    aggregate_rates as aggregate_payer,
)
from payer.curated import (
    to_comparable_rates as payer_to_rates,
)
from reconcile.aggregate import median_by_group
from reconcile.comparability import ComparableRate
from reconcile.curated import NEEDED_COLUMNS, open_curated
from reconcile.curated import to_comparable_rates as hosp_to_rates
from storage import Location

#: Where each side lives under the lake root.
HOSPITAL_SILVER = ("silver", "hospital_rates")
PAYER_SILVER = ("silver", "payer_rates")

#: Rows per batch when scanning for file metadata. Small enough that a batch is
#: cheap to hold, large enough that 56.8M rows is not 56,800 round trips.
METADATA_BATCH = 200_000

_HOSPITAL_KEYS = [
    "hospital",
    "location_name",
    "source_url",
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


def open_hospital_silver(lake: Location) -> ds.Dataset:
    """The conformed hospital rates.

    Goes through :func:`open_curated` rather than a second ``ds.dataset`` call,
    so the one reader stays the one reader (ADR 0002). It looks for a nested
    ``curated/hospital_rates`` first and falls back to the location itself,
    which is what a silver root is.
    """
    return open_curated(Path("."), location=lake.child(*HOSPITAL_SILVER))


def open_payer_silver(lake: Location) -> ds.Dataset:
    target = lake.child(*PAYER_SILVER)
    return ds.dataset(target.root, filesystem=target.filesystem, partitioning="hive")


def payer_files_from_silver(dataset: ds.Dataset) -> list[PayerFile]:
    """Rebuild the per-file metadata the converter needs, from silver's own rows.

    ``payer`` is the config label the parser wrote, equal to the filename stem;
    :func:`split_label` decomposes it into carrier and network by the same
    convention the directory walk used. ``last_updated_on`` is the vintage the
    payer stamped -- the column the reader ignored for months while falling back
    to a hardcoded date map, which is the failure ADR 0001 exists about.

    ``path`` is synthetic. Nothing downstream reads it; it is kept only because
    :class:`PayerFile` is also what the local path constructs, and giving it a
    plausible value is cheaper than splitting the type in two.
    """
    scanner = dataset.scanner(columns=["payer", "last_updated_on"], batch_size=METADATA_BATCH)
    seen: dict[str, str | None] = {}
    for batch in scanner.to_batches():
        for stem, updated in zip(
            batch.column("payer").to_pylist(),
            batch.column("last_updated_on").to_pylist(),
            strict=True,
        ):
            if stem and stem not in seen:
                seen[stem] = updated or None
    files = []
    for stem, vintage in sorted(seen.items()):
        carrier, network = split_label(stem)
        files.append(
            PayerFile(
                path=Path(f"{stem}.parquet"),
                stem=stem,
                carrier=carrier,
                network=network,
                vintage=vintage,
            )
        )
    return files


#: Called at each step of a hospital shard load with the step's name and what it
#: produced. The mart passes nothing; the load profiler passes a function that
#: reads memory, which is how the step holding the memory gets a name.
LoadHook = Callable[..., None]


def hospital_shard(
    dataset: ds.Dataset,
    hospital: str,
    code_types: tuple[str, ...],
    shard: str = "",
    *,
    slug: str | None = None,
    readahead: bool = True,
    on_step: LoadHook | None = None,
    on_descriptions: Callable[[pa.Table], None] | None = None,
) -> list[ComparableRate]:
    """Aggregated hospital rates for one system and one code shard.

    Sharding on the leading character of the code is the memory bound, not a
    narrowing of the question: the join key contains the code, so no pair
    straddles a shard and the totals stay exact.

    ``slug`` adds the ``hospital_slug`` partition to the filter. The rows are
    the same -- a system's name and its slug identify the same partition --
    but a filter on the ``hospital`` column cannot skip a directory, so without
    it every shard load reads every system's files in silver and discards the
    rest. ``readahead=False`` cuts the scanner's fragment and batch readahead to
    the minimum of one. Both exist to be measured before either is made the default.
    """
    where = (ds.field("hospital") == hospital) & (ds.field("code_type").isin(list(code_types)))
    if slug:
        where = where & (ds.field("hospital_slug") == slug)
    if shard:
        where = where & pc.starts_with(ds.field("code"), shard)
    # One, not zero: pyarrow 25's scanner deadlocks with a readahead of 0 --
    # to_table never returns -- which a test found before a cloud run could
    # have hung on it for the job's full timeout.
    options: dict[str, int] = {} if readahead else {"fragment_readahead": 1, "batch_readahead": 1}
    step = on_step or (lambda *_a, **_k: None)
    step("start", files=len(dataset.files))
    # The description rides along only when asked for, and only where silver
    # has it: it is not a grouping key, so it changes no rate, and code lookup's
    # search is the one reader of it.
    describe = on_descriptions is not None and "description" in dataset.schema.names
    columns = [*NEEDED_COLUMNS, "description"] if describe else list(NEEDED_COLUMNS)
    scanned = dataset.to_table(columns=columns, filter=where, **options)
    step("scanned", rows=scanned.num_rows, table_mib=round(scanned.nbytes / 2**20, 1))
    if scanned.num_rows == 0:
        return []
    if describe and on_descriptions is not None:
        on_descriptions(
            scanned.group_by(["code_type", "code", "description"])
            .aggregate([("code", "count")])
            .rename_columns(["code_type", "code", "description", "rows"])
        )
    table = median_by_group(scanned, _HOSPITAL_KEYS, "rate_dollar", [("methodology", "min")])
    step("aggregated", rows=table.num_rows, table_mib=round(table.nbytes / 2**20, 1))
    del scanned
    step("scan_released")
    rates = hosp_to_rates(table)
    step("converted", rates=len(rates))
    return rates


def payer_shard(
    dataset: ds.Dataset,
    files: list[PayerFile],
    system: str,
    code_types: tuple[str, ...],
    shard: str = "",
    *,
    facilities: dict[str, list[str]] | None = None,
    carriers: tuple[str, ...] = (),
) -> list[ComparableRate]:
    """Aggregated payer rates for one system and one code shard.

    **Aggregated one carrier at a time.** ``aggregate_rates`` takes a DISTINCT
    over every column before the median per key, which is the same shape that
    once turned a mart run into 58 GB. A single leading-character shard of payer
    silver is still millions of rows and shards are not evenly sized: in a 4 GiB
    container this peaked at 4,063 MiB on shard ``1`` and was killed on shard 2.

    Splitting by carrier is exact, not an approximation. Every group-by key
    includes ``payer``, the config label that was the filename stem, and a stem
    belongs to exactly one carrier -- so no group and no duplicate row can
    straddle two carriers, and aggregating them separately gives the same rows
    as aggregating them together. It also lets Arrow skip whole partitions,
    since payer silver is keyed on carrier.
    """
    wanted = carriers or tuple(sorted({f.carrier for f in files}))
    rates: list[ComparableRate] = []
    for carrier in wanted:
        # Narrowed on the dataset rather than through PayerFilter, which has no
        # carrier field and should not grow one for this: carrier is a partition
        # column, so this is a directory skip and never reaches a row.
        table = aggregate_payer(
            dataset.filter(ds.field("carrier") == carrier),
            PayerFilter(systems=(system,), code_types=code_types, code_prefix=shard),
        )
        if table.num_rows:
            rates.extend(payer_to_rates(table, files, facilities=facilities))
        del table
    return rates


__all__ = [
    "HOSPITAL_SILVER",
    "PAYER_SILVER",
    "hospital_shard",
    "open_hospital_silver",
    "open_payer_silver",
    "payer_files_from_silver",
    "payer_shard",
]
