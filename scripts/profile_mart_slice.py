"""Profile the slice that reaches 7 GB, without changing anything about it.

7:Mount Sinai Queens -- shard "7", facility "Mount Sinai Queens", 395,462 payer
rates. Issue #47. Measurement only: nothing here alters structure, because the
point is that four structural changes were already made on the evidence of RSS
alone and none of them fixed it.
"""

from __future__ import annotations

import linecache
import tracemalloc
from dataclasses import replace

from pipeline.mart import SHARED_CODE_TYPES
from reconcile.gold import reconcile_shard
from reconcile.silver import (
    hospital_shard,
    open_hospital_silver,
    open_payer_silver,
    payer_files_from_silver,
    payer_shard,
)
from storage import resolve

SHARD = "7"
FACILITY = "Mount Sinai Queens"
SYSTEM = "Mount Sinai"
HOSPITAL = "Mount Sinai Health System"
SEP = chr(92)


def mib(n: float) -> str:
    return f"{n / (1024 * 1024):8.1f} MiB"


def where_of(frame: tracemalloc.Frame) -> str:
    tail = frame.filename.replace(SEP, "/").split("/src/")[-1]
    return tail + ":" + str(frame.lineno)


def show(stats: list, limit: int) -> None:
    for i, stat in enumerate(stats[:limit], 1):
        frame = stat.traceback[0]
        size = getattr(stat, "size_diff", None)
        size = stat.size if size is None else size
        count = getattr(stat, "count_diff", None)
        count = stat.count if count is None else count
        print(f"{i:2}. {mib(size)}  {count:>9,} objs  {where_of(frame)}")
        line = linecache.getline(frame.filename, frame.lineno).strip()
        if line:
            print(f"      {line[:96]}")


def main() -> None:
    lake = resolve()
    hosp = open_hospital_silver(lake)
    pay = open_payer_silver(lake)
    files = payer_files_from_silver(pay)

    print("loading the slice (not profiled) ...", flush=True)
    left_all = hospital_shard(hosp, HOSPITAL, SHARED_CODE_TYPES, SHARD)
    left = [r for r in left_all if r.hospital == FACILITY]
    del left_all
    base = payer_shard(pay, files, SYSTEM, SHARED_CODE_TYPES, SHARD)
    right = [replace(r, hospital=FACILITY) for r in base]
    del base
    print(f"  hospital rates {len(left):,}   payer rates {len(right):,}", flush=True)

    tracemalloc.start(25)
    before = tracemalloc.take_snapshot()
    entry, _ = tracemalloc.get_traced_memory()

    mart = reconcile_shard(left, right)

    after = tracemalloc.take_snapshot()
    current, peak = tracemalloc.get_traced_memory()
    print(f"\npairs formed {len(mart.rows):,}, excluded {sum(mart.excluded.values()):,}")
    print(f"traced at entry {mib(entry)} | retained {mib(current)} | PEAK {mib(peak)}")

    print("\n=== grown during the call (top 12) ===")
    show(after.compare_to(before, "lineno"), 12)

    print("\n=== inside variance.py / comparability.py only ===")
    inside = [
        s
        for s in after.statistics("lineno")
        if "variance.py" in s.traceback[0].filename or "comparability.py" in s.traceback[0].filename
    ]
    show(inside, 10)

    print("\n=== by allocating file ===")
    totals: dict[str, int] = {}
    for stat in after.statistics("lineno"):
        name = stat.traceback[0].filename.replace(SEP, "/").split("/")[-1]
        totals[name] = totals.get(name, 0) + stat.size
    for name, size in sorted(totals.items(), key=lambda kv: -kv[1])[:10]:
        print(f"   {mib(size)}  {name}")

    tracemalloc.stop()


if __name__ == "__main__":
    main()
