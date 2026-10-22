"""Do RC bundles change a CPT/HCPCS rate? Compare bundle items with the plain ffs item for the same code and provider group.

Usage: python -I bundle_check.py FILE.json.gz OUT.json
"""
import gzip
import json
import sys
from collections import Counter, defaultdict

import ijson

FILE, OUT = sys.argv[1], sys.argv[2]
ffs = {}  # (code, group id, class, negotiated type, setting) -> set of rates
bun = defaultdict(set)  # same key -> set of (rate, rc set id)
rcsets = {}  # code -> list of RC tuples, one per bundle item
c = Counter()
with gzip.open(FILE, "rb") as f:
    for item in ijson.items(f, "in_network.item"):
        bct = item.get("billing_code_type")
        if bct not in ("CPT", "HCPCS"):
            continue
        code = item["billing_code"]
        bundle = item.get("negotiation_arrangement") == "bundle"
        rcs = tuple(sorted(b["billing_code"] for b in item.get("bundled_codes") or [] if b.get("billing_code_type") == "RC"))
        others = [b for b in item.get("bundled_codes") or [] if b.get("billing_code_type") != "RC"]
        if bundle:
            rcsets.setdefault(code, []).append(rcs)
            c["bundle items"] += 1
            c["bundle items with non-RC bundled codes"] += bool(others)
        for nr in item.get("negotiated_rates") or []:
            for pr in nr.get("negotiated_prices") or []:
                if pr.get("billing_class") != "institutional":
                    if bundle:
                        c["bundle prices, professional"] += 1
                    continue
                for g in nr.get("provider_references") or []:
                    k = (code, g, pr.get("negotiated_type"), pr.get("setting"))
                    r = round(float(pr["negotiated_rate"]), 2)
                    if bundle:
                        bun[k].add((r, hash(rcs)))
                        c["bundle prices, institutional (per group)"] += 1
                    else:
                        ffs.setdefault(k, set()).add(r)

out = dict(c)
out["codes with bundles"] = len(rcsets)
out["codes with >1 bundle item"] = sum(1 for v in rcsets.values() if len(v) > 1)
out["codes whose bundles have different RC sets"] = sum(1 for v in rcsets.values() if len(set(v)) > 1)
out["RCs per bundle: min/median/max"] = (lambda L: (L[0], L[len(L) // 2], L[-1]))(sorted(len(t) for v in rcsets.values() for t in v))
cmp = Counter()
ex = []
for k, vals in bun.items():
    rates = {r for r, _ in vals}
    base = ffs.get(k)
    if base is None:
        cmp["bundle key with no ffs rate for same code+group"] += 1
    elif rates == base:
        cmp["bundle rate == ffs rate"] += 1
    else:
        cmp["bundle rate != ffs rate"] += 1
        if len(ex) < 15:
            ex.append({"key": list(map(str, k)), "bundle": sorted(rates), "ffs": sorted(base)})
    if len({h for _, h in vals}) > 1 and len(rates) > 1:
        cmp["same code+group: different RC sets carry different rates"] += 1
out["comparison"] = dict(cmp)
out["examples_differ"] = ex
out["sample_rc_sets"] = {code: [list(t)[:12] + (["..."] if len(t) > 12 else []) for t in v[:3]] for code, v in list(rcsets.items())[:5]}
json.dump(out, open(OUT, "w"), indent=1, default=str)
print(json.dumps(out, indent=1, default=str)[:6000])
