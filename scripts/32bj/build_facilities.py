"""Find every facility of the four systems that bills a non-professional Anthem rate; write facilities.csv.

Usage:
    python -I scripts/32bj/build_facilities.py DERIVED_DIR OUT_CSV

Reads ``*.groups.parquet`` and ``*.prices.parquet`` written by extract_facility_rates.py,
takes every billing NPI (the TiC ``tin.value``; Anthem uses NPIs there, never EINs) that
has a non-professional price, resolves it in the public NPPES registry (cached in
DERIVED_DIR/nppes_cache.json), and keeps those whose organisation name belongs to
Montefiore, Mount Sinai, NYU Langone or Northwell. Campus labels for the acute hospitals
are set by hand below; every other facility is labelled from its NPPES name, city and
taxonomy.
"""

from __future__ import annotations

import csv
import json
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import duckdb

SYSTEM_NAMES = {
    "northwell": r"northwell|north shore[- ]?(lij|university)|long island jewish|lenox hill|staten island univ|cohen child"
                 r"|zucker|plainview hosp|syosset hosp|south shore univ|huntington hosp|glen cove hosp|mather hosp"
                 r"|peconic bay|phelps|northern westchester|stern family center",
    "mount_sinai": r"mount sinai|mt\.? sinai|icahn|beth israel|new york eye (and|&) ear|south nassau|st\.? luke'?s[- ]roosevelt",
    "montefiore": r"montefiore|einstein|nyack hosp|burke rehab|white plains hosp|st\.? luke'?s cornwall|mount vernon hosp|schaffer",
    "nyu": r"\bnyu\b|langone|new york university|winthrop",
}
# Names that match a pattern above but are not the system (found while building the list).
NOT_SYSTEM = r"north shore child and family|north shore medical group of the mount sinai"

# Acute campuses, labelled by hand. One Bronx column for Montefiore Medical Center
# (Moses, Weiler, Wakefield bill under one NPI).
CAMPUS = {
    "1952476988": "Montefiore Medical Center (Bronx)",
    "1104808062": "Montefiore Nyack",
    "1720414154": "Montefiore New Rochelle",
    "1992131320": "Montefiore Mount Vernon",
    "1447255153": "White Plains Hospital",
    "1003923434": "St. Luke's Cornwall",
    "1124295746": "St. Luke's Cornwall (psych unit)",
    "1134892847": "Montefiore Einstein Advanced Care (Elmsford)",
    "1932103413": "The Mount Sinai Hospital",
    "1053721563": "The Mount Sinai Hospital",
    "1245248939": "Mount Sinai Queens",
    "1174768071": "Mount Sinai Brooklyn",
    "1174689665": "Mount Sinai Brooklyn",
    "1104982917": "Mount Sinai Morningside/West (St. Luke's-Roosevelt)",
    "1245251222": "New York Eye and Ear Infirmary",
    "1922079094": "Mount Sinai South Nassau",
    "1669578324": "NYU Langone Tisch/Kimmel",
    "1801992631": "NYU Langone Tisch/Kimmel",
    "1598864118": "NYU Langone Tisch/Kimmel",
    "1427616002": "NYU Langone Long Island",
    "1619341716": "NYU Langone Brooklyn",
    "1831910355": "NYU Langone Long Island Community (Patchogue)",
    "1053354100": "NYU Langone Long Island Community (Patchogue)",
    "1366459570": "Northwell North Shore University Hospital",
    "1285641514": "Northwell Long Island Jewish Medical Center",
    "1174530349": "Northwell Cohen Children's",
    "1346603107": "Northwell LIJ Valley Stream",
    "1477616571": "Northwell Lenox Hill",
    "1407877137": "Northwell Staten Island University Hospital",
    "1295743060": "Northwell Glen Cove",
    "1508845322": "Northwell Huntington",
    "1083621130": "Northwell Plainview",
    "1457369381": "Northwell South Shore University Hospital",
    "1720185267": "Northwell Phelps",
    "1912992215": "Northwell Northern Westchester",
}


def nppes(npi: str, cache: dict[str, Any]) -> dict[str, Any]:
    if npi in cache:
        return cache[npi]
    for attempt in range(3):
        try:
            with urllib.request.urlopen(f"https://npiregistry.cms.hhs.gov/api/?version=2.1&number={npi}", timeout=30) as r:
                res = json.load(r).get("results") or []
            break
        except Exception:
            time.sleep(2 * (attempt + 1))
    else:
        return {"name": None, "error": "lookup failed"}
    if not res:
        cache[npi] = {"name": None}
        return cache[npi]
    r = res[0]
    b = r["basic"]
    tax = next((t for t in r.get("taxonomies", []) if t.get("primary")), (r.get("taxonomies") or [{}])[0])
    addr = next((a for a in r.get("addresses", []) if a.get("address_purpose") == "LOCATION"), {})
    cache[npi] = {"type": r.get("enumeration_type"),
                  "name": b.get("organization_name") or f'{b.get("first_name", "")} {b.get("last_name", "")}'.strip(),
                  "taxonomy": tax.get("code"), "taxonomy_desc": tax.get("desc"),
                  "city": addr.get("city"), "state": addr.get("state"), "address": addr.get("address_1")}
    time.sleep(0.15)
    return cache[npi]


def system_of(name: str) -> str | None:
    if not name or re.search(NOT_SYSTEM, name, re.I):
        return None
    for s, rx in SYSTEM_NAMES.items():
        if re.search(rx, name, re.I):
            return s
    return None


def main(derived: Path, out_csv: Path) -> None:
    # The 32BJ file's groups table is 114M rows: cap memory and spill to disk.
    con = duckdb.connect()
    con.sql("set memory_limit = '1GB'")
    con.sql("set preserve_insertion_order = false")
    con.sql("set threads = 2")
    con.sql(f"set temp_directory = '{(derived / '_spill').as_posix()}'")
    rows = con.sql(f"""
        with p as (select source_file, unnest(provider_references) gid from read_parquet('{derived.as_posix()}/*.prices.parquet')),
             c as (select source_file, gid, count(*) n from p group by all)
        select g.tin_value npi, sum(c.n) prices
        from c join read_parquet('{derived.as_posix()}/*.groups.parquet') g
          on g.source_file = c.source_file and g.provider_group_id = c.gid
        where g.tin_type = 'npi'
        group by 1 order by 2 desc""").fetchall()
    cache_path = derived / "nppes_cache.json"
    cache: dict[str, Any] = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    print(f"{len(rows)} billing NPIs with non-professional prices; {sum(1 for n, _ in rows if n not in cache)} to look up",
          file=sys.stderr, flush=True)
    out = []
    for i, (npi, n) in enumerate(rows, 1):
        info = nppes(npi, cache)
        if i % 100 == 0:
            cache_path.write_text(json.dumps(cache))
            print(f"  {i}/{len(rows)}", file=sys.stderr, flush=True)
        s = system_of(info.get("name") or "")
        if s:
            label = CAMPUS.get(npi) or f'{(info.get("name") or "").title()} ({(info.get("city") or "").title()})'
            out.append({"npi": npi, "system": s, "campus": label, "acute_campus": npi in CAMPUS,
                        "nppes_name": info.get("name"), "taxonomy": info.get("taxonomy_desc"),
                        "city": info.get("city"), "address": info.get("address"), "prices_in_source": n})
    cache_path.write_text(json.dumps(cache))
    missing = [k for k in CAMPUS if k not in {o["npi"] for o in out}]
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(sorted(out, key=lambda o: (o["system"], not o["acute_campus"], o["campus"])))
    print(f"wrote {len(out)} facilities to {out_csv}; hand-labelled campuses with no rates: {missing}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
