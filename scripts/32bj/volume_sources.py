"""Rank the "most common" codes for the 32BJ tables from public volume data; write top25.csv.

Usage:
    python -I scripts/32bj/volume_sources.py OUT_DIR

Sources (docs/32bj-evaluation.md, 8.5), all public, raw responses saved in OUT_DIR:

- APR-DRG: NY SPARCS Hospital Inpatient Discharges (De-Identified) 2023, health.data.ny.gov
  dataset 46xm-urtu, discharges with Payment Typology 1 = Private Health Insurance,
  counted by APR-DRG x severity.
- MS-DRG: CMS Medicare Inpatient Hospitals - by Geography and Service, 2024, New York
  state row per MS-DRG (Tot_Dschrgs). Medicare fee-for-service only: leans to older
  patients, and childbirth is nearly absent.
- Outpatient: the CPT/HCPCS codes among CMS's 70 shoppable services (84 FR 65571),
  ranked by New York facility-setting services in CMS Medicare Physician & Other
  Practitioners - by Geography and Service, 2024 (Place_Of_Srvc = F, Tot_Srvcs).
"""

from __future__ import annotations

import csv
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SPARCS = "https://health.data.ny.gov/resource/46xm-urtu.json"
CMS_INPATIENT_GEO_2024 = "https://data.cms.gov/data-api/v1/dataset/2941ab09-8cee-49d8-9703-f3c5b854e388/data"
CMS_PHYSICIAN_GEO_2024 = "https://data.cms.gov/data-api/v1/dataset/6fea9d79-0129-4e4c-b1b8-23cd86a4f435/data"

# CPT/HCPCS codes of the 70 CMS-specified shoppable services (84 FR 65571-65572; CMS,
# "10 Steps to Making Public Standard Charges for Shoppable Services"). The five
# MS-DRG entries on that list (216, 460, 470, 473, 743) are inpatient and not used here;
# code ranges ("84153-84154") and alternatives ("81000 or 81001") are expanded.
SHOPPABLE = [
    "90832", "90834", "90837", "90846", "90847", "90853", "99203", "99204", "99205", "99243", "99244",
    "99385", "99386", "80048", "80053", "80055", "80061", "80069", "80076", "81000", "81001", "81002",
    "81003", "84153", "84154", "84443", "85025", "85027", "85610", "85730", "70450", "70553", "72110",
    "72148", "72193", "73721", "74177", "76700", "76805", "76830", "77065", "77066", "77067", "19120",
    "29826", "29881", "42820", "43235", "43239", "45378", "45380", "45385", "45391", "47562", "49505",
    "55700", "55866", "59400", "59510", "59610", "62322", "62323", "64483", "66821", "66984", "93000",
    "93452", "95810", "97110",
]


def get_json(url: str, params: dict[str, str]) -> Any:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(full, timeout=120) as r:
        return json.load(r)


def cms_all(url: str, filters: dict[str, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    while True:
        page = get_json(url, {**{f"filter[{k}]": v for k, v in filters.items()}, "size": "5000", "offset": str(len(rows))})
        rows += page
        if len(page) < 5000:
            return rows


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    top: list[dict[str, Any]] = []

    sparcs = get_json(SPARCS, {
        "$select": "apr_drg_code, apr_severity_of_illness_code, apr_drg_description, apr_severity_of_illness, count(*) as discharges",
        "$where": "payment_typology_1 = 'Private Health Insurance'",
        "$group": "apr_drg_code, apr_severity_of_illness_code, apr_drg_description, apr_severity_of_illness",
        "$order": "discharges DESC", "$limit": "50000"})
    (out / "sparcs_2023_private_by_aprdrg_soi.json").write_text(json.dumps(sparcs, indent=0))
    ranked = sorted(sparcs, key=lambda r: -int(r["discharges"]))
    for i, r in enumerate(ranked[:25], 1):
        code = f'{int(r["apr_drg_code"]):03d}{r["apr_severity_of_illness_code"]}'
        top.append({"table": "inpatient_apr_drg", "rank": i, "code_type": "APR-DRG", "code": code,
                    "description": f'{r["apr_drg_description"]} (severity {r["apr_severity_of_illness_code"]}: {r["apr_severity_of_illness"]})',
                    "volume": int(r["discharges"]), "volume_unit": "SPARCS 2023 discharges, private insurance, NY"})

    inpatient = cms_all(CMS_INPATIENT_GEO_2024, {"Rndrng_Prvdr_Geo_Lvl": "State", "Rndrng_Prvdr_Geo_Desc": "New York"})
    (out / "cms_inpatient_geo_2024_ny.json").write_text(json.dumps(inpatient, indent=0))
    for i, r in enumerate(sorted(inpatient, key=lambda r: -int(float(r["Tot_Dschrgs"])))[:25], 1):
        top.append({"table": "inpatient_ms_drg", "rank": i, "code_type": "MS-DRG", "code": f'{int(r["DRG_Cd"]):03d}',
                    "description": r["DRG_Desc"], "volume": int(float(r["Tot_Dschrgs"])),
                    "volume_unit": "Medicare FFS discharges, NY, 2024"})

    phys = cms_all(CMS_PHYSICIAN_GEO_2024, {"Rndrng_Prvdr_Geo_Lvl": "State", "Rndrng_Prvdr_Geo_Desc": "New York",
                                            "Place_Of_Srvc": "F"})
    (out / "cms_physician_geo_2024_ny_facility.json").write_text(json.dumps(phys, indent=0))
    vol: dict[str, tuple[float, str]] = {}
    for r in phys:
        code = r["HCPCS_Cd"]
        if code in SHOPPABLE:
            prev = vol.get(code, (0.0, r["HCPCS_Desc"]))
            vol[code] = (prev[0] + float(r["Tot_Srvcs"]), r["HCPCS_Desc"])
    ranked_codes = sorted(SHOPPABLE, key=lambda c: -vol.get(c, (0.0, ""))[0])
    for i, c in enumerate(ranked_codes[:25], 1):
        v, desc = vol.get(c, (0.0, ""))
        top.append({"table": "outpatient", "rank": i, "code_type": "HCPCS" if c[0].isalpha() else "CPT", "code": c,
                    "description": desc, "volume": int(v),
                    "volume_unit": "Medicare services in a facility setting, NY, 2024 (CMS shoppable list)"})

    with (out / "top25.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(top[0]))
        w.writeheader()
        w.writerows(top)
    for t in ("inpatient_apr_drg", "inpatient_ms_drg", "outpatient"):
        rows = [r for r in top if r["table"] == t]
        print(t, len(rows), [f'{r["code"]}:{r["volume"]}' for r in rows[:6]], file=sys.stderr)
    missing = [c for c in SHOPPABLE if c not in vol]
    print("shoppable codes with no NY facility volume:", missing, file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]))
