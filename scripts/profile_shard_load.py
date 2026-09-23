"""Which half of a shard load holds the memory: the hospital side or the payer side.

The memory watcher shows a shard's first slice carrying the load's peak --
Montefiore shard 1 at 6,010 MiB against about 1,900 for the pairing that
follows -- and both NYU and Montefiore dying while loading the shard after. A
load is two calls, ``hospital_shard`` and ``payer_shard``, and the watcher
cannot tell them apart. This runs exactly one of them, in a fresh process, and
reports that process's peak working set and Arrow's high-water mark.

    PYTHONPATH=src RECKONER_STORAGE=adls ... python scripts/profile_shard_load.py \\
        nyu-langone-health 1 hospital

Reads silver from the lake the job reads, so the input is the job's input.
"""

from __future__ import annotations

import ctypes
import sys
import time

import pyarrow as pa

from pipeline.mart import RECONCILABLE, SHARED_CODE_TYPES
from reconcile.silver import (
    hospital_shard,
    open_hospital_silver,
    open_payer_silver,
    payer_files_from_silver,
    payer_shard,
)
from storage import resolve


def peak_mib() -> int | None:
    """Peak working set of this process (Windows) or VmHWM (Linux)."""
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
        call = kernel32.K32GetProcessMemoryInfo
        call.restype = ctypes.c_int
        call.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_uint32]
        if not call(
            ctypes.c_void_p(kernel32.GetCurrentProcess()), ctypes.byref(counters), counters.cb
        ):
            return None
        return int(counters.PeakWorkingSetSize // (1024 * 1024))
    with open("/proc/self/status", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) // 1024
    return None


def main(slug: str, shard: str, half: str) -> None:
    spec = next(s for s in RECONCILABLE if s.slug == slug)
    lake = resolve()
    before = peak_mib()
    started = time.monotonic()
    if half == "hospital":
        rates = hospital_shard(open_hospital_silver(lake), spec.hospital, SHARED_CODE_TYPES, shard)
    else:
        dataset = open_payer_silver(lake)
        files = payer_files_from_silver(dataset)
        rates = payer_shard(dataset, files, spec.system, SHARED_CODE_TYPES, shard)
    pool = pa.default_memory_pool()
    print(
        f"{slug} shard {shard} {half}: {len(rates):,} rates in "
        f"{time.monotonic() - started:.0f}s | peak working set {peak_mib()} MiB "
        f"(from {before}) | arrow high-water {pool.max_memory() / 2**20:,.0f} MiB "
        f"({pool.backend_name})"
    )


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
