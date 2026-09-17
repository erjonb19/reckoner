"""Where the slice's memory actually goes: the load, not the pairing.

tracemalloc says cross_source_variance peaks at 11.1 MiB. So the 7.1 GB
attributed to this slice is spent before it -- and tracemalloc cannot see it,
because Arrow allocates in C++. Arrow keeps its own high-water mark.
"""

from __future__ import annotations

from dataclasses import replace

import pyarrow as pa
import pyarrow.dataset as ds

from payer.curated import PayerFilter
from payer.curated import aggregate_rates as aggregate_payer
from payer.curated import to_comparable_rates as payer_to_rates
from pipeline.mart import SHARED_CODE_TYPES
from reconcile.silver import (
    hospital_shard,
    open_hospital_silver,
    open_payer_silver,
    payer_files_from_silver,
)
from storage import resolve

SHARD = "7"
FACILITY = "Mount Sinai Queens"
SYSTEM = "Mount Sinai"
HOSPITAL = "Mount Sinai Health System"


def gb(n: float) -> str:
    return f"{n / 1e9:6.3f} GB"


def main() -> None:
    pool = pa.default_memory_pool()
    lake = resolve()
    hosp = open_hospital_silver(lake)
    pay = open_payer_silver(lake)
    files = payer_files_from_silver(pay)
    print(
        f"after opening + metadata: arrow now {gb(pool.bytes_allocated())} "
        f"peak {gb(pool.max_memory())}"
    )

    left = hospital_shard(hosp, HOSPITAL, SHARED_CODE_TYPES, SHARD)
    print(
        f"hospital_shard ({len(left):,} rates): arrow now {gb(pool.bytes_allocated())} "
        f"peak {gb(pool.max_memory())}"
    )

    carriers = sorted({f.carrier for f in files})
    rates = []
    for carrier in carriers:
        table = aggregate_payer(
            pay.filter(ds.field("carrier") == carrier),
            PayerFilter(systems=(SYSTEM,), code_types=SHARED_CODE_TYPES, code_prefix=SHARD),
        )
        got = payer_to_rates(table, files) if table.num_rows else []
        rates.extend(got)
        print(
            f"  carrier {carrier:10} rows {table.num_rows:>8,} -> {len(got):>8,} rates"
            f" | arrow now {gb(pool.bytes_allocated())} peak {gb(pool.max_memory())}"
        )
        del table

    print(f"payer base total {len(rates):,}")
    right = [replace(r, hospital=FACILITY) for r in rates]
    print(f"after attributing to one facility ({len(right):,}): arrow peak {gb(pool.max_memory())}")
    print()
    print(f"ARROW HIGH-WATER FOR THIS SLICE: {gb(pool.max_memory())}")


if __name__ == "__main__":
    main()
