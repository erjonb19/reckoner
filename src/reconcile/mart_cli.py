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
"""

from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
from pathlib import Path

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
from reconcile.system_range import compare_to_system_range, summarise
from reconcile.variance import (
    apply_systematic_offsets,
    cross_source_variance,
)

#: Code systems both sides publish. Anything else cannot be paired: a hospital's
#: APC has no payer counterpart, and a payer's LOCAL code has no meaning outside
#: its own file.
SHARED_CODE_TYPES = ("HCPCS", "MS-DRG", "CPT")

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
        where = where & ds.field("code").cast("string").starts_with(shard)
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


def run_range(
    hospital_side: list[ComparableRate], payer_side: list[ComparableRate]
) -> dict[str, object]:
    """The range comparison: does the payer's rate sit inside the hospitals'?"""
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

    rows = compare_to_system_range(hospital, payer)
    out: dict[str, object] = dict(summarise(rows))
    out["by_carrier"] = dict(collections.Counter(r.payer for r in rows).most_common())
    out["widest"] = [r.describe() for r in rows[:10]]
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
    parser.add_argument("--json", type=Path, help="write the result here as well as printing it")
    args = parser.parse_args(argv)

    code_types = tuple(t.strip() for t in args.code_types.split(",") if t.strip())
    system = args.system or args.hospital

    files = discover_payer_files(args.payer_root)
    payer_table = aggregate_payer(
        open_payer_dataset(files), PayerFilter(systems=(system,), code_types=code_types)
    )
    payer_side = payer_to_rates(payer_table, files, facilities=None)
    print(f"payer files      : {len(files)} ({', '.join(sorted({f.carrier for f in files}))})")
    print(f"payer rates      : {len(payer_side):,} for {system!r}")

    hospital_side = load_hospital_side(args.root, args.hospital, code_types, args.shard)
    print(f"hospital rates   : {len(hospital_side):,} for {args.hospital!r}")
    facilities = sorted({r.hospital for r in hospital_side})
    print(f"facilities       : {len(facilities)}")
    if not hospital_side or not payer_side:
        print("\nnothing to compare: one side is empty")
        return 1

    if args.mode == "range":
        result = run_range(hospital_side, payer_side)
    else:
        result = run_pairs(hospital_side, payer_side, args.max_vintage_days)
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
