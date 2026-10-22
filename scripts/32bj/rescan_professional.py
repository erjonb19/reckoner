"""Collect Anthem's professional rates for the codes in St. Luke's Cornwall's hospital file.

Usage:
    python -I scripts/32bj/rescan_professional.py FILE.json.gz REFS_JSON CROSSCHECK_CSV OUT_DIR

Tests one explanation for the outpatient disagreement in docs/32bj-evaluation.md 8.10:
that St. Luke's own file lists professional amounts in its Anthem / Local 32BJ
outpatient column, while Anthem's institutional rates are facility amounts. Streams
in_network once (ijson, one item at a time) and writes two Parquet files to OUT_DIR:

- ``st_lukes_professional.parquet``: every professional price on a provider reference
  that includes St. Luke's (REFS_JSON, from the groups table), for the codes in
  CROSSCHECK_CSV, with network, service codes (place of service) and modifiers
- ``professional_rate_sets.parquet``: for the same codes, every distinct professional
  rate Anthem publishes for any provider, so a match to someone else's fee schedule is
  visible too
"""

from __future__ import annotations

import csv
import gzip
import json
import sys
import time
from pathlib import Path

import ijson
import pyarrow as pa
import pyarrow.parquet as pq


def main(src: Path, refs_json: Path, crosscheck_csv: Path, out: Path) -> None:
    refs = {int(k) for k in json.loads(refs_json.read_text())}
    with crosscheck_csv.open(encoding="utf-8") as f:
        codes = {
            (r["code_type"], r["code"])
            for r in csv.DictReader(f)
            if r["code_type"] in ("CPT", "HCPCS")
        }
    rows: dict[str, list] = {
        k: []
        for k in (
            "code_type",
            "code",
            "negotiated_type",
            "setting",
            "rate",
            "service_code",
            "modifier",
            "n_st_lukes_refs",
            "arrangement",
        )
    }
    rate_sets: dict[tuple[str, str], set[float]] = {}
    t0 = time.time()
    with gzip.open(src, "rb") as f:
        for i, item in enumerate(ijson.items(f, "in_network.item")):
            if i and i % 5000 == 0:
                print(
                    f"  {i} items, {len(rows['code'])} St. Luke's professional prices, "
                    f"{time.time() - t0:.0f}s",
                    file=sys.stderr,
                    flush=True,
                )
            key = (item.get("billing_code_type"), str(item.get("billing_code")))
            if key not in codes:
                continue
            for nr in item.get("negotiated_rates") or []:
                hit = sum(1 for r in nr.get("provider_references") or [] if int(r) in refs)
                for pr in nr.get("negotiated_prices") or []:
                    if pr.get("billing_class") != "professional":
                        continue
                    rate = round(float(pr["negotiated_rate"]), 2)
                    rate_sets.setdefault(key, set()).add(rate)
                    if hit:
                        rows["code_type"].append(key[0])
                        rows["code"].append(key[1])
                        rows["negotiated_type"].append(pr.get("negotiated_type"))
                        rows["setting"].append(pr.get("setting"))
                        rows["rate"].append(rate)
                        rows["service_code"].append(
                            ",".join(map(str, pr.get("service_code") or []))
                        )
                        rows["modifier"].append(
                            ",".join(map(str, pr.get("billing_code_modifier") or []))
                        )
                        rows["n_st_lukes_refs"].append(hit)
                        rows["arrangement"].append(item.get("negotiation_arrangement"))
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(rows), out / "st_lukes_professional.parquet", compression="zstd")
    pq.write_table(
        pa.table(
            {
                "code_type": [k[0] for k in rate_sets for _ in rate_sets[k]],
                "code": [k[1] for k in rate_sets for _ in rate_sets[k]],
                "rate": [r for k in rate_sets for r in sorted(rate_sets[k])],
            }
        ),
        out / "professional_rate_sets.parquet",
        compression="zstd",
    )
    print(
        f"done: {len(rows['code'])} St. Luke's professional prices over "
        f"{len(set(zip(rows['code_type'], rows['code'], strict=True)))} "
        f"codes; rate sets for {len(rate_sets)} codes; {time.time() - t0:.0f}s",
        file=sys.stderr,
    )


if __name__ == "__main__":
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
