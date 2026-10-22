"""Write facility_rates.parquet: every non-professional rate at the facilities in facilities.csv.

Usage:
    python -I scripts/32bj/build_facility_rates.py DERIVED_DIR FACILITIES_CSV [MEMORY_LIMIT]

The logic is ``pipeline.thirtytwobj_view.build_facility_rates``, so the page and this
script cannot disagree on what a 32BJ-tier or a standard rate is. DuckDB is capped at
MEMORY_LIMIT (default 1GB) and spills to DERIVED_DIR/_spill.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from pipeline.thirtytwobj_view import build_facility_rates

if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__)
    derived = Path(sys.argv[1])
    counts = build_facility_rates(
        derived,
        Path(sys.argv[2]),
        derived / "facility_rates.parquet",
        sys.argv[3] if len(sys.argv) == 4 else "1GB",
    )
    print("facility_rates.parquet rows by network:", counts, file=sys.stderr)
