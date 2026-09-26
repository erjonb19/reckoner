"""Check a system's gold in the lake against a recorded baseline.

The proof that the mart's spool changed no output: after the first spooled run
(2026-10-01), White Plains gold must match what the 2026-09-23 run wrote. Eight
tables compare by file sha256. ``rates`` compares by its rows, order-independent,
because the 2026-09-23 file was written before ``preserve_order`` and its row
order was not reproducible.

    RECKONER_STORAGE=adls ... python scripts/verify_gold_baseline.py \\
        docs/measurements/white-plains-gold-2026-09-23.json

Reads one system's gold, about 1 MB. Exits non-zero on any difference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import storage
from pipeline.mart import TABLES


def rows_digest(data: bytes) -> tuple[str, int]:
    table = pq.read_table(pa.BufferReader(data))
    rows = sorted(json.dumps(r, sort_keys=True, default=str) for r in table.to_pylist())
    return hashlib.sha256("\n".join(rows).encode()).hexdigest(), table.num_rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("baseline", type=Path)
    args = parser.parse_args(argv)

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    lake = storage.resolve()
    failures = 0
    for name, expected in sorted(baseline["files"].items()):
        table = name.split("/")[0]
        if table not in TABLES:
            continue  # triage writes these, not the mart
        path = lake.child("gold", *name.split("/")).root
        with lake.filesystem.open_input_stream(path) as handle:
            data = handle.readall()
        if table == "rates":
            digest, count = rows_digest(data)
            ok = digest == baseline["rates_rows_sorted_sha256"] and count == baseline["rates_rows"]
            how = f"rows {count}, sorted-rows sha256 {digest[:16]}"
        else:
            digest = hashlib.sha256(data).hexdigest()
            ok = digest == expected["sha256"]
            how = f"sha256 {digest[:16]}"
        failures += not ok
        print(f"{'same' if ok else 'DIFFERENT':9} {name}  ({how})")
    print("byte-identical" if not failures else f"{failures} tables differ")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
