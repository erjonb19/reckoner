"""Stage 2: build the reconciliation mart from silver and land it in gold.

One system at a time, one code shard at a time. The shard loop is the memory
bound: the unsharded mart peaked at 9,808 MiB against a container ceiling of
4,096, which is not a margin to tune but a wall to stay behind.

**Nothing is written until every system has been reconciled.** A half-written
gold layer whose manifest describes the whole would be a drift the stage-1 check
reports every month, and the run that caused it would already have exited
non-zero -- two alarms for one fault, one of them permanent. Gold is small
enough to hold in memory until the run is known to be complete.

The four systems here are the only ones that can be reconciled at all: the
hospital lake holds twelve and the payer target list seven, and these four appear
in both. That is a fact about name overlap, not about coverage, and
``docs/coverage.md`` carries the accounting.
"""

from __future__ import annotations

import gc
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, TypeAlias

import pyarrow as pa
import pyarrow.dataset as ds

from payer.curated import PayerFile, PayerFilter
from reconcile.comparability import ComparableRate
from reconcile.eligibility import facility_only_hospitals
from reconcile.gold import Reconciliation, stream_shard
from reconcile.silver import (
    hospital_shard,
    open_hospital_silver,
    open_payer_silver,
    payer_files_from_silver,
    payer_shard,
)
from storage import Location

#: Code systems both sides publish. Anything else cannot be paired: a hospital's
#: APC has no payer counterpart, and a payer's LOCAL code has no meaning outside
#: its own file.
SHARED_CODE_TYPES = ("HCPCS", "MS-DRG", "CPT")

#: CPT and MS-DRG are numeric, HCPCS is a letter then four digits.
SHARDS = tuple("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")

#: Payer rows above which a shard is split into two-character prefixes.
#:
#: NYU Langone is why this exists. It has the largest payer side -- 15.3M rows
#: against Northwell's 9.8M -- and exceeded 8 GiB on shard "0" in a container
#: holding nothing else, which is the Consumption ceiling. Splitting *every*
#: shard would be 1,296 of them and multiply the payer scans accordingly, so the
#: split is measured rather than blanket: only the shards that are actually big
#: pay for it. The count is a filtered count_rows, which reads footers rather
#: than rows.
SUBSHARD_ABOVE_ROWS = 2_000_000

#: Progress hooks, so the job can log a shard without this module importing the
#: logger and the tests can watch it without capturing stdout.
ShardHook: TypeAlias = Callable[["SystemSpec", str, int, int, int], None]
#: Told when a shard is split: the prefix, its payer row count, and how many
#: children it became.
PlanHook: TypeAlias = Callable[[str, int, int], None]
SystemHook: TypeAlias = Callable[[Reconciliation], None]

GOLD_ROOT = ("gold",)
GOLD_MANIFEST = ("_meta", "gold", "upload_manifest.json")


@dataclass(frozen=True)
class SystemSpec:
    """One reconcilable system, under the two names its two sources use."""

    hospital: str
    system: str
    slug: str


#: The two sides do not spell a system the same way, and neither spelling is
#: wrong -- the hospital MRF carries the legal name, the payer file a short
#: label. Written down here rather than matched at run time because a fuzzy
#: match that silently picks the wrong system produces a full, plausible mart.
RECONCILABLE = (
    SystemSpec("Mount Sinai Health System", "Mount Sinai", "mount-sinai-health-system"),
    SystemSpec("Northwell Health", "Northwell", "northwell-health"),
    SystemSpec("NYU Langone Health", "NYU Langone", "nyu-langone-health"),
    SystemSpec("NewYork-Presbyterian", "NYP", "newyork-presbyterian"),
    # Added once the hospital files arrived. Both sites block automated access,
    # so the MRFs were fetched by hand and ingested from data/Inbox -- see
    # docs/scope.md. Montefiore was previously out of scope and is in now with
    # permission.
    SystemSpec("White Plains Hospital", "White Plains", "white-plains-hospital"),
    SystemSpec(
        "Westchester Medical Center Health Network",
        "WMC",
        "westchester-medical-center-health-network",
    ),
    SystemSpec("Montefiore Health System", "Montefiore", "montefiore-health-system"),
)

#: One table per grain, each partitioned by ``hospital_slug``. Names match the
#: summary dataset the report stage publishes, so the mapping from gold to the
#: page is one to one and needs no translation table.
TABLES = (
    "coverage",
    "outcomes",
    "magnitude",
    "exemplars",
    "refusals",
    "triage_queue",
    "triage_summary",
)


def plan_shards(
    payer_dataset: ds.Dataset,
    system: str,
    code_types: tuple[str, ...],
    *,
    shards: tuple[str, ...] = SHARDS,
    threshold: int = SUBSHARD_ABOVE_ROWS,
    on_plan: PlanHook | None = None,
) -> tuple[str, ...]:
    """Single-character shards, with the large ones split in two characters.

    Measured, not assumed. A blanket two-character split is 1,296 shards and
    1,296 passes over payer silver; splitting only what is large keeps the
    common case at 36. The measurement is a filtered ``count_rows``, which reads
    Parquet footers and statistics rather than rows.

    A shard below the threshold is left whole even if it is the largest one
    present -- the threshold is about fitting in a container, not about
    balancing.
    """
    planned: list[str] = []
    for shard in shards:
        where = PayerFilter(
            systems=(system,), code_types=code_types, code_prefix=shard
        ).expression()
        rows = payer_dataset.count_rows(filter=where)
        if rows == 0:
            continue
        if rows <= threshold:
            planned.append(shard)
            continue
        children = [f"{shard}{c}" for c in SHARDS]
        planned.extend(children)
        if on_plan is not None:
            on_plan(shard, rows, len(children))
    return tuple(planned)


def reconcile_system(
    hospital_dataset: ds.Dataset,
    payer_dataset: ds.Dataset,
    payer_files: list[PayerFile],
    spec: SystemSpec,
    *,
    code_types: tuple[str, ...] = SHARED_CODE_TYPES,
    max_vintage_days: int = 400,
    shards: tuple[str, ...] = SHARDS,
    on_shard: ShardHook | None = None,
    assume_facility: bool = False,
) -> Reconciliation:
    """Reconcile one system across every shard, then close it once.

    ``close()`` is called here and only here: it fixes the systematic offsets
    over the whole system, which is the thing sharding would otherwise break.
    """
    run = Reconciliation(
        hospital=spec.hospital,
        system=spec.system,
        hospital_slug=spec.slug,
        assumed_facility_when_unstated=assume_facility,
    )
    for shard in shards:
        left = hospital_shard(hospital_dataset, spec.hospital, code_types, shard)
        if not left:
            continue

        # The payer file resolves to a system; the hospital MRF names a
        # facility. Bridging them is what makes a pair possible at all -- without
        # it every hospital row is excluded as having no counterpart, which is
        # what the runner did silently from #14 to #26.
        #
        # Done one facility at a time, and that is the difference between this
        # fitting in a container and not. Attributing a system's rates to all its
        # facilities at once multiplies the payer side by the facility count:
        # 395,462 rates became 3,163,696 on one Mount Sinai shard, to produce
        # 113,718 pairs. Almost every copy never met anything, and the copies are
        # what exhausted 8 GiB -- the Consumption ceiling, with no larger machine
        # to move to.
        #
        # Exact rather than approximate, for the same reason the carrier split
        # is: the join keys on the provider and an offset contract is keyed by
        # facility, so no pair and no contract spans two facilities. Verified on
        # the shard that broke it -- 113,718 pairs and 7,508,052 exclusions
        # either way.
        base = payer_shard(payer_dataset, payer_files, spec.system, code_types, shard)
        if not base:
            del left
            continue
        by_facility: dict[str, list[ComparableRate]] = {}
        for rate in left:
            by_facility.setdefault(rate.hospital, []).append(rate)
        del left

        for facility in sorted(by_facility):
            _reconcile_facility(
                run,
                spec,
                shard,
                facility,
                by_facility[facility],
                base,
                max_vintage_days=max_vintage_days,
                assume_facility=assume_facility,
                on_shard=on_shard,
            )
        del base, by_facility
        gc.collect()
        pa.default_memory_pool().release_unused()
        _trim_heap()
        continue
    run.close()
    return run


def _trim_heap() -> None:
    """Ask glibc to hand the top of the heap back, where that is a thing.

    ``gc.collect()`` and Arrow's ``release_unused()`` free memory; neither
    returns it to the operating system, and RSS is what the container is killed
    for. glibc only trims on free when the top chunk exceeds a threshold, so a
    loop that frees in one order and allocates in another can hold pages
    indefinitely. No-op on anything without glibc, which includes the laptop
    this is written on.
    """
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        return


def _reconcile_facility(
    run: Reconciliation,
    spec: SystemSpec,
    shard: str,
    facility: str,
    left: list[ComparableRate],
    base: list[ComparableRate],
    *,
    max_vintage_days: int,
    assume_facility: bool,
    on_shard: ShardHook | None,
) -> None:
    """One facility's slice of one shard.

    ``base`` is aggregated once per shard and attributed here, rather than
    re-aggregated per facility: the aggregation is the expensive part and it is
    identical for every facility, only the name attached to each rate differs.
    """
    right = [replace(rate, hospital=facility) for rate in base]
    if not right:
        return
    # The eligibility answer is at system grain, because that is what the lake's
    # `hospital` column holds, but a ComparableRate carries the resolved
    # facility. Expanding here is sound -- a system with no professional row
    # anywhere has no facility with one -- and getting it wrong is why the option
    # appeared to do nothing on its first run.
    eligible = frozenset({facility}) if assume_facility else frozenset()
    # Streamed, not collected. One slice of this system forms three million
    # pairs from a quarter-million inputs -- a 290-way fan-out, because the join
    # keys on carrier rather than plan -- and holding them all was roughly two
    # gigabytes for rows that are read once and mostly discarded.
    mart, rows = stream_shard(
        left,
        right,
        max_vintage_days=max_vintage_days,
        assume_facility_when_unstated=eligible,
    )
    pairs = run.add_shard(
        f"{shard}:{facility}",
        mart,
        hospital_rates=len(left),
        payer_rates=len(right),
        rows=rows,
    )
    if on_shard is not None:
        on_shard(spec, f"{shard}:{facility}", len(left), len(right), pairs)
    del right, mart, rows
    # Collected per slice, not per shard. Splitting the fan-out by facility
    # bounded what is live at once but not what is *garbage* at once: eight
    # slices each build and discard their own copy of the shard's payer rates,
    # so collecting only at the end of the loop let the same 3.16 million
    # objects pile up unreferenced. The run died on a slice of 2,787 rates,
    # which is the tell -- by then the size of the slice had stopped mattering.
    gc.collect()
    gc.collect()


def select(only: str | None) -> tuple[SystemSpec, ...]:
    """The systems to reconcile: one named, or all four.

    Matches on the slug or on either of the two names a system goes by, because
    the caller is a shell variable and being strict about which of three correct
    spellings it used would only produce a run that reconciles nothing.
    """
    wanted = (only or "").strip().casefold()
    if not wanted:
        # An environment variable set to blank is an unset one. Treating it as a
        # name would fail a run for the sake of a stray space in a shell.
        return RECONCILABLE
    chosen = [
        spec
        for spec in RECONCILABLE
        if wanted in {spec.slug.casefold(), spec.system.casefold(), spec.hospital.casefold()}
    ]
    if not chosen:
        known = ", ".join(spec.slug for spec in RECONCILABLE)
        raise ValueError(f"no reconcilable system matches {only!r}; known slugs: {known}")
    return tuple(chosen)


def build(
    lake: Location,
    *,
    only: str | None = None,
    on_shard: ShardHook | None = None,
    on_system: SystemHook | None = None,
    on_plan: PlanHook | None = None,
) -> list[Reconciliation]:
    """Reconcile the selected systems -- by default, all of them."""
    hospital_dataset = open_hospital_silver(lake)
    payer_dataset = open_payer_silver(lake)
    payer_files = payer_files_from_silver(payer_dataset)

    # Computed from the lake, not listed: a hardcoded set stops being true the
    # moment the lake gains a system. Once per run, because it is a two-column
    # scan and the answer is the same for every system in it.
    facility_only = facility_only_hospitals(hospital_dataset)

    runs = []
    for spec in select(only):
        # Planned per system, because how big a code prefix is depends on whose
        # payer file it is. NYU Langone needs shard "0" split; Mount Sinai does
        # not, and paying for 1,296 passes to discover that would be absurd.
        planned = plan_shards(payer_dataset, spec.system, SHARED_CODE_TYPES, on_plan=on_plan)
        run = reconcile_system(
            hospital_dataset,
            payer_dataset,
            payer_files,
            spec,
            shards=planned or SHARDS,
            on_shard=on_shard,
            assume_facility=spec.hospital in facility_only,
        )
        runs.append(run)
        if on_system is not None:
            on_system(run)
    return runs


def tables(runs: list[Reconciliation]) -> dict[str, list[dict[str, Any]]]:
    """Every gold table, as records, across all systems."""
    return {
        "coverage": [run.coverage_row() for run in runs],
        "outcomes": [row for run in runs for row in run.outcome_rows()],
        "magnitude": [row for run in runs for row in run.magnitude_rows()],
        "exemplars": [row for run in runs for row in run.exemplar_rows()],
        "refusals": [row for run in runs for row in run.refusal_rows()],
    }


def write(
    lake: Location, built: dict[str, list[dict[str, Any]]], *, compression: str = "zstd"
) -> dict[str, int]:
    """Write each table to ``gold/<table>/``, partitioned by system.

    Partitioned even though the tables are small, because the partition column
    is what a reader filters on and a hive path makes that a directory skip
    rather than a scan. It also makes the gold layer the same shape as the two
    silver layers, so one manifest check covers all three without a special case.
    """
    written: dict[str, int] = {}
    for name in TABLES:
        records = built.get(name, [])
        target = lake.child(*GOLD_ROOT, name)
        if not records:
            written[name] = 0
            continue
        table = pa.Table.from_pylist(records)
        ds.write_dataset(
            table,
            base_dir=target.root,
            filesystem=target.filesystem,
            format="parquet",
            partitioning=ds.partitioning(
                pa.schema([("hospital_slug", pa.string())]), flavor="hive"
            ),
            file_options=ds.ParquetFileFormat().make_write_options(compression=compression),
            basename_template="part-{i}.parquet",
            existing_data_behavior="delete_matching",
        )
        written[name] = table.num_rows
    return written


def gold_manifest(lake: Location, written: dict[str, int], systems: set[str]) -> dict[str, Any]:
    """The gold manifest after one stage wrote ``written`` for ``systems``.

    Describes the whole gold tree, because that is what stage 1 checks, and
    verifies only what this stage is responsible for: its own tables, in its own
    systems' partitions. Both narrowings are needed. Gold is written by the mart
    (five tables) and by triage (two more, into the same partitions), a system
    at a time. Checking every table let triage's rows fail a correct mart run;
    checking every system let other systems' rows fail a correct per-system run.

    A table this stage owns but wrote no rows for is still checked. Its old
    partition is not deleted by an empty write, so if one survives it is stale,
    and the check failing is the check working.
    """
    from storage import publish

    manifest = publish.build_manifest(
        lake.child(*GOLD_ROOT),
        [
            publish.PublishResult(
                subject=name,
                destination=name,
                rows_read=rows,
                rows_written=rows,
                partitions=len(systems),
            )
            for name, rows in written.items()
        ],
        layer="gold",
        group_key="hospital_slug",
        verify_only=systems,
        verify_subjects=set(written),
    )
    manifest["systems_written"] = sorted(systems)
    manifest["tables_written"] = sorted(written)
    manifest["complete"] = sorted(manifest["by_hospital_slug"]) == sorted(
        spec.slug for spec in RECONCILABLE
    )
    return manifest


__all__ = [
    "GOLD_MANIFEST",
    "GOLD_ROOT",
    "RECONCILABLE",
    "SHARDS",
    "SHARED_CODE_TYPES",
    "TABLES",
    "SystemSpec",
    "build",
    "gold_manifest",
    "reconcile_system",
    "select",
    "tables",
    "write",
]
