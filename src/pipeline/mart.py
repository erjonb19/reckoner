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

from payer.curated import PayerFile
from reconcile.comparability import ComparableRate
from reconcile.eligibility import facility_only_hospitals
from reconcile.gold import Reconciliation, reconcile_shard
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

#: Progress hooks, so the job can log a shard without this module importing the
#: logger and the tests can watch it without capturing stdout.
ShardHook: TypeAlias = Callable[["SystemSpec", str, int, int, int], None]
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
)

#: One table per grain, each partitioned by ``hospital_slug``. Names match the
#: summary dataset the report stage publishes, so the mapping from gold to the
#: page is one to one and needs no translation table.
TABLES = ("coverage", "outcomes", "magnitude", "exemplars", "refusals")


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
        continue
    run.close()
    return run


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
    mart = reconcile_shard(
        left,
        right,
        max_vintage_days=max_vintage_days,
        assume_facility_when_unstated=eligible,
    )
    run.add_shard(f"{shard}:{facility}", mart, hospital_rates=len(left), payer_rates=len(right))
    if on_shard is not None:
        on_shard(spec, f"{shard}:{facility}", len(left), len(right), len(mart.rows))
    del right, mart
    gc.collect()


def build(
    lake: Location,
    *,
    on_shard: ShardHook | None = None,
    on_system: SystemHook | None = None,
) -> list[Reconciliation]:
    """Reconcile every system that can be reconciled."""
    hospital_dataset = open_hospital_silver(lake)
    payer_dataset = open_payer_silver(lake)
    payer_files = payer_files_from_silver(payer_dataset)

    # Computed from the lake, not listed: a hardcoded set stops being true the
    # moment the lake gains a system. Once per run, because it is a two-column
    # scan and the answer is the same for every system in it.
    facility_only = facility_only_hospitals(hospital_dataset)

    runs = []
    for spec in RECONCILABLE:
        run = reconcile_system(
            hospital_dataset,
            payer_dataset,
            payer_files,
            spec,
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


__all__ = [
    "GOLD_MANIFEST",
    "GOLD_ROOT",
    "RECONCILABLE",
    "SHARDS",
    "SHARED_CODE_TYPES",
    "TABLES",
    "SystemSpec",
    "build",
    "reconcile_system",
    "tables",
    "write",
]
