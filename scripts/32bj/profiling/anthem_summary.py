"""Resolve every matched NPI in NPPES, drop name-search noise, and roll up per file and system.

Usage: python -I anthem_summary.py DATA_DIR   (holds anthem_NY_*.json from profile_anthem.py)
"""
import glob, json, os, re, sys, time, urllib.request
from collections import Counter, defaultdict

HERE = sys.argv[1]
CACHE = os.path.join(HERE, "nppes_cache.json")
cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
SYS_RX = {
    "northwell": re.compile(r"northwell|north shore[- ]?(lij|university)|long island jewish|lenox hill|staten island univ|cohen child|zucker|nuvance|vassar|putnam hosp|sharon hosp|danbury hosp|norwalk hosp|new milford hosp|northern dutchess|plainview hosp|syosset hosp|south shore univ|huntington hosp|glen cove hosp|mather hosp|peconic bay|phelps|northern westchester", re.I),
    "mount_sinai": re.compile(r"mount sinai|mt\.? sinai|icahn|beth israel|new york eye (and|&) ear|south nassau|st\.? luke'?s[- ]roosevelt", re.I),
    "montefiore": re.compile(r"montefiore|einstein|nyack hosp|burke rehab|white plains hosp|st\.? luke'?s cornwall|mount vernon hosp|schaffer", re.I),
    "nyu": re.compile(r"\bnyu\b|langone|new york university|winthrop", re.I),
    "st_barnabas": re.compile(r"st\.? barnabas hosp|saint barnabas hosp|sbh health", re.I),
}
FACILITY_TAX = re.compile(r"^(28|27|26|31|32)")  # hospitals, units, clinics/centers, nursing, residential


def nppes(n):
    if n in cache:
        return cache[n]
    for _ in range(3):
        try:
            with urllib.request.urlopen(f"https://npiregistry.cms.hhs.gov/api/?version=2.1&number={n}", timeout=30) as r:
                res = json.load(r).get("results") or []
            break
        except Exception:
            time.sleep(2)
    else:
        return None
    if not res:
        cache[n] = {"name": None}
        return cache[n]
    r = res[0]
    b = r["basic"]
    tax = next((t for t in r.get("taxonomies", []) if t.get("primary")), (r.get("taxonomies") or [{}])[0])
    addr = next((a for a in r.get("addresses", []) if a.get("address_purpose") == "LOCATION"), {})
    cache[n] = {"type": r.get("enumeration_type"), "name": b.get("organization_name") or f'{b.get("first_name","")} {b.get("last_name","")}',
                "taxonomy": tax.get("code"), "taxonomy_desc": tax.get("desc"), "city": addr.get("city"), "state": addr.get("state")}
    time.sleep(0.15)
    return cache[n]


profiles = {os.path.basename(p)[7:-5]: json.load(open(p)) for p in sorted(glob.glob(os.path.join(HERE, "anthem_NY_*.json")))}
npis = sorted({ev.split(" | ")[0] for p in profiles.values() for s, d in p["target_evidence"].items() for ev in d if not ev.startswith("ein:")})
print("resolving", len(npis), "NPIs", file=sys.stderr)
for n in npis:
    nppes(n)
json.dump(cache, open(CACHE, "w"), indent=0)

out = {}
for f, p in profiles.items():
    per = {}
    for s, d in p["target_evidence"].items():
        rows = defaultdict(Counter)
        for ev, cnt in d.items():
            key, bc = ev.rsplit(" | ", 1)
            info = cache.get(key) or {}
            ok = bool(SYS_RX[s].search(info.get("name") or ""))
            kind = "facility" if (info.get("type") == "NPI-2" and FACILITY_TAX.match(info.get("taxonomy") or "")) else "group/other"
            rows[(key, info.get("name"), info.get("taxonomy_desc"), info.get("city"), ok, kind)][bc] += cnt
        per[s] = [{"npi": k[0], "name": k[1], "taxonomy": k[2], "city": k[3], "name_confirms_system": k[4], "kind": k[5], "prices": dict(v)}
                  for k, v in sorted(rows.items(), key=lambda kv: -sum(kv[1].values()))]
    out[f] = per
json.dump(out, open(os.path.join(HERE, "anthem_target_npis.json"), "w"), indent=1)

for f, per in out.items():
    print("==", f)
    for s, rows in per.items():
        conf = [r for r in rows if r["name_confirms_system"]]
        fac = [r for r in conf if r["kind"] == "facility"]
        print(f"  {s}: {len(rows)} matched NPIs, {len(conf)} confirmed by name, {len(fac)} facilities")
        for r in fac:
            print(f"     FAC {r['npi']} {r['name']} [{r['taxonomy']}, {r['city']}] {r['prices']}")
        noise = [r["name"] for r in rows if not r["name_confirms_system"]]
        if noise:
            print(f"     dropped as name noise ({len(noise)}): {noise[:8]}")
