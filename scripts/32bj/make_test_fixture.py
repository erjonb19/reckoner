"""Cut the 32BJ page's test fixture from the real tables: a small slice of real Anthem rows.

Usage:
    python -I scripts/32bj/make_test_fixture.py DERIVED_DIR tests/fixtures/thirtytwobj

Keeps, from facility_rates.parquet (public Anthem data, 2026-10-01):
- the top-25 APR-DRG and MS-DRG codes at Montefiore Medical Center, St. Luke's Cornwall,
  The Mount Sinai Hospital, Mount Sinai South Nassau and NYU Langone Tisch/Kimmel;
- the same codes at one Northwell acute hospital (standard only) and one Northwell unit
  that is in the 32BJ tier;
- a few outpatient codes and per diem rows, so the tests can prove they stay hidden.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import duckdb

NPIS = (
    # acute campuses: Montefiore Medical Center, St. Luke's Cornwall, The Mount Sinai
    # Hospital, Mount Sinai South Nassau, NYU Langone Tisch/Kimmel
    "1952476988",
    "1003923434",
    "1932103413",
    "1922079094",
    "1801992631",
    # Northwell: LIJ (standard rates only) and SIUH's psych unit (in the 32BJ tier)
    "1285641514",
    "1912925199",
)
OUTPATIENT = ("70450", "74177", "66984")


def main(derived: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.sql("set memory_limit = '1GB'")
    top = (derived / "top25.csv").as_posix()
    rates_out = (out / "facility_rates.parquet").as_posix()
    con.sql(f"""
        copy (
            select * from read_parquet('{(derived / "facility_rates.parquet").as_posix()}')
            where billing_npi in {NPIS}
              and ((code_type, code) in (select code_type, code
                                          from read_csv('{top}', all_varchar = true)
                                          where "table" <> 'outpatient')
                   or (code_type = 'CPT' and code in {OUTPATIENT})
                   or (negotiated_type = 'per diem' and code_type = 'MS-DRG'))
        ) to '{(out / "facility_rates.parquet").as_posix()}' (format parquet, compression zstd)""")
    con.sql(f"""
        copy (
            select i.* from read_parquet('{(derived / "facility_items.parquet").as_posix()}') i
            where (source_file, item_idx) in (select source_file, item_idx
                                              from read_parquet('{rates_out}'))
        ) to '{(out / "facility_items.parquet").as_posix()}' (format parquet, compression zstd)""")
    shutil.copy(derived / "top25.csv", out / "top25.csv")
    shutil.copy(derived / "st_lukes_crosscheck.json", out / "st_lukes_crosscheck.json")
    n = con.sql(
        f"select count(*) from read_parquet('{(out / 'facility_rates.parquet').as_posix()}')"
    ).fetchone()
    print(f"fixture rows: {n[0] if n else 0}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
