import json
from collections import defaultdict, Counter
# Usage: python -I match.py DATA_DIR   (holds profile.json, ein_names.json, npi_refs.json)
import sys
D = sys.argv[1]
prof = json.load(open(f"{D}/profile.json")); names = json.load(open(f"{D}/ein_names.json")); refs = json.load(open(f"{D}/npi_refs.json"))
EINS = {
 "montefiore": {"131740114","131740119","471582973","201615393","462931956","462916938","863090734","474853506","010796859","830621846","472209056","205125301","131739937","141340054","131740115","462929888","131740130","471600439"},
 "mount_sinai": {"131624096","136171197","135564934","132997301","135562304","111352310","136271888","133838460","812057452","450537391","464242915","133444730"},
}
for p in prof:
    if "tins" not in p: continue
    f = p["file"]; per = Counter(); hits = defaultdict(list)
    for k, v in p["tins"].items():
        t, val = k.split(":", 1); val9 = val.zfill(9)
        for sysn, s in EINS.items():
            if t == "ein" and val9 in s: hits[f"{sysn}:EIN"].append((val9, v.get("rate_refs", v.get("rows")), (names.get(val9) or {}).get("name") if isinstance(names.get(val9), dict) else None))
        npis = v["npis"] + ([val] if t == "npi" else [])
        for n in set(npis):
            r = refs.get(n)
            if r: hits[f'{r["system"]}:NPI'].append((n, r["name"], f"tin={val9}", (names.get(val9) or {}).get("name") if isinstance(names.get(val9), dict) else None))
    all_npis = set(n for v in p["tins"].values() for n in v["npis"])
    print("==", f, "TINs", len(p["tins"]), "distinct NPIs", len(all_npis))
    for k, v in hits.items():
        print("  ", k, len(v))
        for x in v[:25]: print("     ", x)
