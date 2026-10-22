"""Cross-check St. Luke's Cornwall's own hospital file against Anthem's 32BJ file.

The hospital side is its Anthem / Local 32BJ rows.

Usage:
    python -I scripts/32bj/crosscheck_st_lukes.py DERIVED_DIR LAKE_DIR OUT_CSV

Both rules describe the same contract here, so agreement is a test of whether Anthem's
"NY Preferred Facility Tier1 32BJ" tier is what the hospital calls Local 32BJ.

Match rules (docs/32bj-evaluation.md, 8.6):
- APR-DRG case rate      <-> APR-DRG (any dollar negotiated type), on the four-digit code
- CPT/HCPCS fee schedule <-> CPT/HCPCS, on the code, outpatient; or case rate
- RC                     <-> RC, on the code
Dollar rates only. The hospital file is 2026-04 and Anthem 2026-10-01, so a uniform
ratio is an escalator, not a disagreement; the ratio distribution is printed for that.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

ST_LUKES_NPIS = ("1003923434", "1124295746", "1013184639")  # hospital, psych unit, ESRD
ST_LUKES_EIN = "141340054"
TIER = "NY Preferred Facility Tier1  32BJ"
DOLLAR = ("negotiated", "fee schedule", "derived")


def main(derived: Path, lake: Path, out_csv: Path) -> None:
    con = duckdb.connect()
    con.sql("set memory_limit = '1GB'")
    con.sql(f"set temp_directory = '{(derived / '_spill').as_posix()}'")
    # The Anthem side is the same tidy table the 32BJ page reads (build_facility_rates.py),
    # so the cross-check and the page cannot disagree on what a 32BJ-tier rate is.
    con.sql(f"""
        create view anthem as
        select distinct code_type, code, setting, negotiated_type, rate, billing_npi
        from read_parquet('{(derived / "facility_rates.parquet").as_posix()}')
        where network = '32BJ tier' and billing_npi in {ST_LUKES_NPIS}
          and negotiated_type in {DOLLAR}""")
    con.sql(f"""
        create view hospital as
        select distinct code_type, code, setting, methodology,
               round(rate_dollar, 2) as rate, file_vintage
        from read_parquet('{lake.as_posix()}/**/*.parquet', hive_partitioning = true)
        where source_url like '%{ST_LUKES_EIN}%' and plan_name_raw ilike '%32%bj%'
          and rate_kind = 'dollar' and methodology in ('case rate', 'fee schedule')
          and code_type in ('APR-DRG', 'CPT', 'HCPCS', 'RC')""")
    print(
        "hospital side:",
        con.sql(
            "select code_type, methodology, count(*), count(distinct code), "
            "any_value(file_vintage) from hospital group by all order by 1"
        ).fetchall(),
    )
    print(
        "anthem side:",
        con.sql(
            "select code_type, negotiated_type, count(*), count(distinct code) "
            "from anthem group by all order by 1"
        ).fetchall(),
    )

    # One row per hospital (code type, code, methodology). The Anthem side may carry several
    # rates for a code (by setting or billing NPI); the nearest one is compared and all are kept.
    con.sql("""
        create table cmp as
        with h as (select code_type, code, methodology,
                          list(distinct rate order by rate) as h_rates, min(rate) as h_rate
                   from hospital group by all),
             a as (select code_type, code, list(distinct rate order by rate) as a_rates,
                          list(distinct negotiated_type) as a_types from anthem group by all),
             j as (select coalesce(h.code_type, a.code_type) as code_type,
                          coalesce(h.code, a.code) as code,
                          h.methodology, h.h_rates, h.h_rate, a.a_rates, a.a_types
                   from h full join a on h.code_type = a.code_type and h.code = a.code)
        select *,
               case when h_rate is null or a_rates is null then null
                    else list_sort(list_transform(a_rates, x -> abs(x - h_rate)))[1] end as abs_gap,
               case when h_rate is null or a_rates is null or h_rate = 0 then null
                    else (list_sort(list_transform(a_rates, x -> [abs(x - h_rate), x]))[1])[2]
                         / h_rate end as ratio
        from j""")
    con.sql("""
        create table outcome as select *,
          case when h_rates is null then 'Anthem only'
               when a_rates is null then 'hospital file only'
               when abs_gap < 0.005 then 'exact to the cent'
               when abs(ratio - 1) <= 0.01 then 'within 1%'
               else 'differs' end as outcome
        from cmp""")
    print("\noutcomes by code type:")
    for r in con.sql(
        "select code_type, outcome, count(*) from outcome group by all order by 1, 3 desc"
    ).fetchall():
        print("  ", r)
    print("\nratio (Anthem / hospital) where both sides have the code:")
    for r in con.sql("""select code_type, count(*), round(quantile_cont(ratio, 0.05), 4),
                               round(median(ratio), 4),
                               round(quantile_cont(ratio, 0.95), 4)
                        from outcome where ratio is not null group by 1 order by 1""").fetchall():
        print("  ", r)
    print("\n20 largest disagreements:")
    for r in con.sql("""select code_type, code, methodology, h_rates, a_rates, round(ratio, 3)
                        from outcome where outcome = 'differs'
                        order by abs(ln(ratio)) desc nulls last limit 20""").fetchall():
        print("  ", r)
    con.sql(
        f"copy (select * from outcome order by code_type, code) to '{out_csv.as_posix()}' (header)"
    )

    # A short record for the page and the doc: counts per outcome among codes on both sides.
    both = con.sql("""select outcome, count(*) from outcome
                      where h_rates is not null and a_rates is not null
                      group by 1""").fetchall()
    counts = dict(both)
    total = sum(counts.values())
    agree = counts.get("exact to the cent", 0) + counts.get("within 1%", 0)
    med = con.sql("select median(ratio) from outcome where ratio is not null").fetchone()[0]
    record = {
        "codes_on_both_sides": total,
        "outcomes": counts,
        "agree_share": round(agree / total, 4) if total else None,
        "median_ratio": round(med, 4) if med is not None else None,
        "hospital_only": con.sql(
            "select count(*) from outcome where outcome = 'hospital file only'"
        ).fetchone()[0],
        "anthem_only": con.sql(
            "select count(*) from outcome where outcome = 'Anthem only'"
        ).fetchone()[0],
    }
    # Inpatient and outpatient agree very differently; one blended share hides that.
    split = {
        setting: con.sql(f"""select count(*) filter (where outcome in
                                 ('exact to the cent', 'within 1%')), count(*)
                             from outcome where h_rates is not null and a_rates is not null
                               and code_type {
            "in ('APR-DRG', 'MS-DRG')" if setting == "inpatient" else "not in ('APR-DRG', 'MS-DRG')"
        }""").fetchone()
        for setting in ("inpatient", "outpatient")
    }
    record["by_setting"] = {s: {"agree": a, "codes": n} for s, (a, n) in split.items()}
    (ia, i_n), (oa, o_n) = split["inpatient"], split["outpatient"]
    record["sentence"] = (
        "Cross-check: St. Luke's Cornwall's own hospital file (Anthem / Local 32BJ, April 2026) "
        "and Anthem's 32BJ tier (October 2026) give the same dollar rate, to within 1%, for "
        f"{ia:,} of the {i_n:,} inpatient case rates both publish, but for only {oa:,} of the "
        f"{o_n:,} outpatient codes ({oa / o_n:.0%})."
        if i_n and o_n
        else "Cross-check: no codes in common."
    )
    (out_csv.parent / "st_lukes_crosscheck.json").write_text(json.dumps(record, indent=1))
    print("\n", json.dumps(record, indent=1))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
