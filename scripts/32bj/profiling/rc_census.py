"""How do revenue codes appear in an Anthem in-network file? Structure census of in_network only.

Usage: python -I rc_census.py FILE.json.gz OUT.json
Streams in_network.item; provider_references are skipped by the parser, not built.
"""
import gzip
import json
import sys
import time
from collections import Counter, defaultdict

import ijson

FILE, OUT = sys.argv[1], sys.argv[2]
c = defaultdict(Counter)
samples = defaultdict(list)
t0 = time.time()


def keep(k, v, n=12):
    if len(samples[k]) < n:
        samples[k].append(v)


with gzip.open(FILE, "rb") as f:
    for i, item in enumerate(ijson.items(f, "in_network.item"), 1):
        if i % 5000 == 0:
            print(i, f"{time.time()-t0:.0f}s", file=sys.stderr, flush=True)
        bct = item.get("billing_code_type")
        arr = item.get("negotiation_arrangement")
        c["item_keys"][" ".join(sorted(item))] += 1
        c["type_x_arrangement"][f"{bct} | {arr}"] += 1
        for b in item.get("bundled_codes") or []:
            c["bundled_code_types"][f"{bct} -> {b.get('billing_code_type')}"] += 1
            keep(f"bundle {bct}", {"code": item.get("billing_code"), "desc": item.get("description"), "bundled": b})
        for b in item.get("covered_services") or []:
            c["covered_service_types"][f"{bct} -> {b.get('billing_code_type')}"] += 1
            keep(f"covered {bct}", {"code": item.get("billing_code"), "covered": b})
        if bct == "RC":
            keep("rc item", {"code": item.get("billing_code"), "desc": item.get("description"), "arr": arr, "keys": sorted(item)}, 25)
        for nr in item.get("negotiated_rates") or []:
            for pr in nr.get("negotiated_prices") or []:
                bc = pr.get("billing_class")
                c["price_keys"][f"{bct} | {bc} | " + " ".join(sorted(pr))] += 1
                mods = pr.get("billing_code_modifier") or []
                if mods:
                    c["modifiers"][f"{bct} | {bc} | {','.join(map(str, mods))}"] += 1
                    # a 4-digit modifier that looks like a revenue code would be the RC+CPT link
                    if any(str(m).isdigit() and len(str(m)) in (3, 4) for m in mods):
                        keep("numeric modifier", {"code": item.get("billing_code"), "type": bct, "mods": mods, "class": bc})
                ai = pr.get("additional_information")
                if ai:
                    c["additional_information"][f"{bct} | {bc} | {str(ai)[:120]}"] += 1
                if bc == "institutional":
                    c["inst_type_x_negtype_x_setting"][f"{bct} | {pr.get('negotiated_type')} | {pr.get('setting')}"] += 1
                if bct == "RC":
                    c["rc_negtype_x_setting"][f"{item.get('billing_code')} | {pr.get('negotiated_type')} | {pr.get('setting')} | {bc}"] += 1

json.dump({"counts": {k: dict(v.most_common(200)) for k, v in c.items()}, "samples": samples, "seconds": round(time.time() - t0)},
          open(OUT, "w"), indent=1, default=str)
print("done", round(time.time() - t0), "s", file=sys.stderr)
