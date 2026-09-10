"""Run the variance mart against real data and print what it found.

Everything the mart has produced so far came from throwaway scripts, which meant
no number in any write-up could be checked by re-running something. This is that
something. It reads the curated hospital lake and the parsed payer parquet, joins
them, and reports the counts -- comparable, refused by reason, and the candidate
explanation of what survived.

Two modes, and the difference is the grain the payer data can support:

* ``range`` (default) compares a payer's rate against the range its system's
  hospitals publish. The payer files resolve no finer than the health system, so
  this is the honest question: does the insurer's number fall inside what the
  hospitals published? One comparison per service and carrier.
* ``pairs`` runs the pairwise mart, attributing the payer rate to each facility
  in turn. Kept because it is what produces the refusal profile -- which rules
  fire, and how often -- but its variance counts are inflated by that fan-out
  and should not be read as findings.

    python -m reconcile.mart_cli --hospital "Mount Sinai" --payer-root ../mrf_pipeline/payer_parquet

**Large systems must be sharded.** The payer aggregation materialises the whole
filtered table and then takes a distinct over every column, so its peak memory
scales with rows going in rather than results coming out. NYU Langone sends 15.1M
rows into that call, reached 58 GB of virtual memory on a 15.6 GB machine, and
took the terminal down with it. ``--all-shards`` runs the sweep a shard at a time
and combines, which is exact because every join key contains the code:

    python -m reconcile.mart_cli --hospital "NYU Langone Health" \\
        --system "NYU Langone" --payer-root ../mrf_pipeline/payer_parquet --all-shards

It costs one dataset scan per shard, so it is slower than a single pass and worth
reaching for only when a single pass will not fit. ``range`` mode only: a variance
mart carries cross-row state and its counts do not combine across shards.
"""

from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.dataset as ds

from payer.curated import (
    PayerFilter,
    discover_payer_files,
    open_payer_dataset,
)
from payer.curated import (
    aggregate_rates as aggregate_payer,
)
from payer.curated import (
    to_comparable_rates as payer_to_rates,
)
from reconcile.comparability import ComparableRate
from reconcile.curated import NEEDED_COLUMNS, open_curated
from reconcile.curated import to_comparable_rates as hosp_to_rates
from reconcile.system_range import RangeComparison, compare_to_system_range, summarise
from reconcile.variance import (
    apply_systematic_offsets,
    cross_source_variance,
)

#: Code systems both sides publish. Anything else cannot be paired: a hospital's
#: APC has no payer counterpart, and a payer's LOCAL code has no meaning outside
#: its own file.
SHARED_CODE_TYPES = ("HCPCS", "MS-DRG", "CPT")

#: Leading characters a billing code takes: CPT and MS-DRG are numeric, HCPCS is
#: a letter followed by four digits. Sharding on this character is what keeps a
#: large system inside memory, on both sides of the join.
SHARDS = tuple("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")

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


def load_hospital_side(
    root: Path, hospital: str, code_types: tuple[str, ...], shard: str | None = None
) -> list[ComparableRate]:
    """Aggregated hospital rates for one system, optionally one code shard.

    Sharding on the leading character of the code keeps a 156 million row lake
    out of memory. The join key contains the code, so no pair straddles a shard
    and the totals stay exact.
    """
    dataset = open_curated(root)
    where = (ds.field("hospital") == hospital) & (ds.field("code_type").isin(list(code_types)))
    if shard:
        where = where & pc.starts_with(ds.field("code"), shard)
    scanned = dataset.to_table(columns=list(NEEDED_COLUMNS), filter=where)
    if scanned.num_rows == 0:
        return []
    table = scanned.group_by(_HOSPITAL_KEYS).aggregate(
        [
            ("rate_dollar", "approximate_median"),
            ("rate_dollar", "count"),
            ("methodology", "min"),
        ]
    )
    del scanned
    return hosp_to_rates(table)


def run_pairs(
    hospital_side: list[ComparableRate],
    payer_side: list[ComparableRate],
    max_vintage_days: int,
) -> dict[str, object]:
    """The pairwise mart: what the comparability layer refuses, and why.

    Calls :func:`cross_source_variance` rather than reimplementing the join, so
    what runs here is the code the tests cover. Systematic offsets are applied
    afterwards, because a constant ratio repeated across hundreds of services is
    one fact about two base rates and reporting it per code buries the question.
    """
    mart = cross_source_variance(hospital_side, payer_side, max_vintage_days=max_vintage_days)
    offsets = apply_systematic_offsets(mart)
    return {
        "hospital_rates": len(hospital_side),
        "payer_rates": len(payer_side),
        "pairs_formed": len(mart.rows),
        "comparable_share": round(mart.comparable_share, 4),
        "material": len(mart.material),
        "unexplained_and_material": len(mart.unexplained),
        "excluded_by_reason": dict(sorted(mart.excluded.items(), key=lambda kv: -kv[1])),
        "explanation": dict(sorted(mart.by_explanation().items(), key=lambda kv: -kv[1])),
        "systematic_offsets": [o.describe() for o in offsets],
        "caveats": mart.provenance.caveats,
    }


def range_rows(
    hospital_side: list[ComparableRate], payer_side: list[ComparableRate]
) -> list[RangeComparison]:
    """The range comparison for one slice: is the payer's rate inside the hospitals'?

    Returns the comparisons rather than a summary so a sharded run can
    accumulate them and summarise once at the end. That is exact rather than
    approximate: every key here is ``(code, code_type, carrier)`` and a shard is
    defined by the code's first character, so no key is split across two shards
    and no comparison is counted twice.
    """
    by_facility: dict[tuple[str, str, str], dict[str, list[float]]] = {}
    for rate in hospital_side:
        if rate.rate_kind != "dollar" or not rate.rate_dollar:
            continue
        key = (rate.code, rate.code_type or "", rate.payer)
        by_facility.setdefault(key, {}).setdefault(rate.hospital, []).append(rate.rate_dollar)
    hospital = {
        key: {f: statistics.median(v) for f, v in facilities.items()}
        for key, facilities in by_facility.items()
    }

    payer: dict[tuple[str, str, str], list[float]] = {}
    for rate in payer_side:
        if rate.rate_kind != "dollar" or not rate.rate_dollar:
            continue
        if rate.billing_class != "facility":
            continue
        payer.setdefault((rate.code, rate.code_type or "", rate.payer), []).append(rate.rate_dollar)

    return compare_to_system_range(hospital, payer)


def summarise_range(rows: list[RangeComparison]) -> dict[str, object]:
    """Headline counts over however many shards were accumulated.

    Re-sorts before taking the widest: each shard arrives sorted within itself,
    and concatenated shards are not.
    """
    ordered = sorted(rows, key=lambda c: (-c.gap, -c.payer_rate))
    out: dict[str, object] = dict(summarise(ordered))
    out["by_carrier"] = dict(collections.Counter(r.payer for r in ordered).most_common())
    out["widest"] = [r.describe() for r in ordered[:10]]
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("data/lake"))
    parser.add_argument(
        "--payer-root", type=Path, required=True, help="directory holding the payer parquet"
    )
    parser.add_argument("--hospital", required=True, help="health system, exact name")
    parser.add_argument("--system", help="how the payer files name it; defaults to --hospital")
    parser.add_argument("--mode", choices=("range", "pairs"), default="range")
    parser.add_argument("--code-types", default=",".join(SHARED_CODE_TYPES))
    parser.add_argument("--max-vintage-days", type=int, default=400)
    parser.add_argument("--shard", help="restrict to codes starting with this, to bound memory")
    parser.add_argument(
        "--all-shards",
        action="store_true",
        help="sweep every shard and combine; the only way to run a system too large for one pass",
    )
    parser.add_argument("--json", type=Path, help="write the result here as well as printing it")
    args = parser.parse_args(argv)

    if args.all_shards and args.shard:
        parser.error("--shard and --all-shards are mutually exclusive")
    if args.all_shards and args.mode != "range":
        parser.error(
            "--all-shards supports --mode range only: a variance mart carries "
            "cross-row state and its counts do not combine across shards"
        )

    code_types = tuple(t.strip() for t in args.code_types.split(",") if t.strip())
    system = args.system or args.hospital

    # include_quarantined so the gate's exclusions can be reported rather than
    # silently shrinking the denominator of everything below.
    everything = discover_payer_files(args.payer_root, include_quarantined=True)
    files = [f for f in everything if not f.is_quarantined]
    quarantined = [f for f in everything if f.is_quarantined]
    print(f"payer files      : {len(files)} ({', '.join(sorted({f.carrier for f in files}))})")
    if quarantined:
        print(f"quarantined      : {len(quarantined)} failed the contract and were not read")
        for bad in quarantined[:5]:
            print(f"    {bad.stem}: {bad.contract_errors[0]}")

    # One pass when unsharded, so the default path is unchanged.
    shards = SHARDS if args.all_shards else (args.shard or "",)
    rows: list[RangeComparison] = []
    facility_set: set[str] = set()
    payer_rows = hospital_rows = 0

    for shard in shards:
        payer_table = aggregate_payer(
            open_payer_dataset(files),
            PayerFilter(systems=(system,), code_types=code_types, code_prefix=shard),
        )
        payer_side = payer_to_rates(payer_table, files, facilities=None)
        # Freed before the hospital side is built: holding both peaks is what
        # this whole mechanism exists to avoid.
        del payer_table
        hospital_side = load_hospital_side(args.root, args.hospital, code_types, shard)
        payer_rows += len(payer_side)
        hospital_rows += len(hospital_side)
        facility_set.update(r.hospital for r in hospital_side)

        if args.mode == "pairs":
            if not hospital_side or not payer_side:
                print("\nnothing to compare: one side is empty")
                return 1
            print(f"payer rates      : {payer_rows:,} for {system!r}")
            print(f"hospital rates   : {hospital_rows:,} for {args.hospital!r}")
            print(f"facilities       : {len(facility_set)}")
            result = run_pairs(hospital_side, payer_side, args.max_vintage_days)
            break

        found = range_rows(hospital_side, payer_side)
        rows.extend(found)
        if args.all_shards:
            print(
                f"  shard {shard}: payer {len(payer_side):,}, hospital "
                f"{len(hospital_side):,} -> {len(found):,} comparisons "
                f"({len(rows):,} total)",
                flush=True,
            )
        del payer_side, hospital_side
    else:
        print(f"payer rates      : {payer_rows:,} for {system!r}")
        print(f"hospital rates   : {hospital_rows:,} for {args.hospital!r}")
        print(f"facilities       : {len(facility_set)}")
        if not rows:
            print("\nnothing to compare: no shard produced a comparison")
            return 1
        result = summarise_range(rows)

    facilities = sorted(facility_set)
    result["hospital"] = args.hospital
    result["system"] = system
    result["mode"] = args.mode
    result["facilities"] = facilities

    print()
    print(json.dumps(result, indent=1, default=str))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
        print(f"\nwritten to {args.json}")
    return 0


if __name__ == "__main__":  # pragma: no cover - entrypoint
    sys.exit(main())
