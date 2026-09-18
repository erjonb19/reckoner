"""Find where one slice's memory goes, step by step, without exceeding a budget.

Parameterised, because the interesting slice changes. Issue #47.

    RECKONER_STORAGE=adls ... python scripts/profile_mart_slice.py \
        --hospital "NYU Langone Health" --system "NYU Langone" \
        --facility "NYU Langone|Brooklyn" --shard 1 --budget-mib 1500

**It stops rather than reproducing the peak.** The slice under investigation
reaches 8 GB in a container, and the laptop this runs on has less headroom than
that. So every step reports Arrow's high-water, Python's traced peak and the
process RSS, and the run aborts the moment RSS crosses ``--budget-mib``, naming
the step that crossed it. Knowing which step allocates is the whole question;
watching it finish is not worth an OOM.

Both gauges, because one of them has already been wrong once.
``pa.total_allocated_bytes`` is live Arrow at this instant and reads 0 between
steps however much passed through; ``pool.max_memory`` is the high-water and is
what "did Arrow use a lot" actually means. tracemalloc covers the Python side,
which Arrow's numbers say nothing about.
"""

from __future__ import annotations

import argparse
import ctypes
import linecache
import sys
import tracemalloc
from dataclasses import replace
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds

from payer.curated import PayerFilter
from payer.curated import aggregate_rates as aggregate_payer
from payer.curated import to_comparable_rates as payer_to_rates
from pipeline.mart import SHARED_CODE_TYPES
from reconcile.gold import reconcile_shard
from reconcile.silver import (
    hospital_shard,
    open_hospital_silver,
    open_payer_silver,
    payer_files_from_silver,
)
from storage import resolve

SEP = chr(92)


class Budget(RuntimeError):
    """Raised when a step would take the machine past what was allowed."""


def rss_mib() -> int | None:
    """Process resident set size, on Windows or Linux, with no dependency."""
    if sys.platform == "win32":

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_uint32),
                ("PageFaultCount", ctypes.c_uint32),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetCurrentProcess()
        # K32GetProcessMemoryInfo on kernel32 first: psapi.dll is not always
        # loadable under the Git Bash shim, and the kernel32 forwarder is.
        call = getattr(kernel32, "K32GetProcessMemoryInfo", None)
        if call is None:
            return None
        # argtypes are load-bearing: GetCurrentProcess returns the pseudo-handle
        # -1, which ctypes truncates to a 32-bit int without them, and the call
        # then fails silently and reports zero.
        call.restype = ctypes.c_int
        call.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_uint32]
        if not call(ctypes.c_void_p(handle), ctypes.byref(counters), counters.cb):
            return None
        return int(counters.WorkingSetSize // (1024 * 1024))
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


class Steps:
    """Reports after every step, and refuses to continue past the budget."""

    def __init__(self, budget_mib: int) -> None:
        self.budget = budget_mib
        self.pool = pa.default_memory_pool()
        self.baseline = rss_mib() or 0
        print(f"budget {budget_mib} MiB of process RSS; baseline {self.baseline} MiB\n")

    def report(self, label: str) -> None:
        rss = rss_mib()
        shown = "  n/a" if rss is None else f"{rss:>6}"
        live = pa.total_allocated_bytes() / 1e9
        peak = self.pool.max_memory() / 1e9
        traced_now, traced_peak = tracemalloc.get_traced_memory()
        print(
            f"  {label:38} RSS {shown} MiB | arrow live {live:6.3f} GB "
            f"peak {peak:6.3f} GB | python now {traced_now / 1e9:6.3f} GB "
            f"peak {traced_peak / 1e9:6.3f} GB"
        )
        if rss is not None and rss > self.budget:
            raise Budget(
                f"step {label!r} took RSS to {rss} MiB, past the {self.budget} MiB budget. "
                "This is the step that allocates."
            )


def top_lines(limit: int = 10) -> None:
    snapshot = tracemalloc.take_snapshot()
    print(f"\n  top {limit} Python allocations still held:")
    for i, stat in enumerate(snapshot.statistics("lineno")[:limit], 1):
        frame = stat.traceback[0]
        where = frame.filename.replace(SEP, "/").split("/src/")[-1]
        line = linecache.getline(frame.filename, frame.lineno).strip()
        print(f"  {i:2}. {stat.size / 1e6:8.1f} MB {stat.count:>9,} objs  {where}:{frame.lineno}")
        if line:
            print(f"        {line[:92]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hospital", required=True)
    parser.add_argument("--system", required=True)
    parser.add_argument("--facility", required=True)
    parser.add_argument("--shard", required=True)
    parser.add_argument("--budget-mib", type=int, default=1500)
    args = parser.parse_args()

    tracemalloc.start(15)
    steps = Steps(args.budget_mib)
    lake = resolve()

    try:
        hosp = open_hospital_silver(lake)
        pay = open_payer_silver(lake)
        steps.report("datasets opened")

        files = payer_files_from_silver(pay)
        steps.report(f"payer metadata ({len(files)} files)")

        left_all = hospital_shard(hosp, args.hospital, SHARED_CODE_TYPES, args.shard)
        steps.report(f"hospital shard ({len(left_all):,} rates)")

        left = [r for r in left_all if r.hospital == args.facility]
        del left_all
        steps.report(f"facility slice ({len(left):,} rates)")

        # Per carrier, as the mart does, so the shape matches production.
        base = []
        for carrier in sorted({f.carrier for f in files}):
            table = aggregate_payer(
                pay.filter(ds.field("carrier") == carrier),
                PayerFilter(
                    systems=(args.system,),
                    code_types=SHARED_CODE_TYPES,
                    code_prefix=args.shard,
                ),
            )
            if table.num_rows:
                base.extend(payer_to_rates(table, files))
            del table
            steps.report(f"payer aggregated: {carrier} (total {len(base):,})")

        right = [replace(r, hospital=args.facility) for r in base]
        del base
        steps.report(f"attributed to facility ({len(right):,} rates)")

        mart = reconcile_shard(left, right)
        steps.report(f"paired ({len(mart.rows):,} pairs)")
        print(f"\n  pairs {len(mart.rows):,}, excluded {sum(mart.excluded.values()):,}")
        top_lines()
    except Budget as stop:
        print(f"\n  STOPPED: {stop}")
        top_lines()
        return 2
    finally:
        tracemalloc.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
