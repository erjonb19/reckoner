"""Context for 'facility' hits, and whether POS-11-only and POS-22-only prices pair up per code and provider set."""
import sys, zipfile, ijson, re
from collections import Counter
z = zipfile.ZipFile(sys.argv[1]); info = z.infolist()[0]
with z.open(info) as f:
    buf = f.read()  # descriptions only; file is <=352 MB and this pass is for context, re-read for pairing below
for m in re.finditer(rb"facility", buf, flags=re.I):
    print("   ctx:", buf[max(0, m.start()-120): m.end()+60].decode("utf-8", "replace"))
del buf
pairs = Counter(); ratio = []; examples = []
with z.open(info) as f:
    for item in ijson.items(f, "in_network.item"):
        by = {}
        for nr in item["negotiated_rates"]:
            key = tuple(sorted(nr["provider_references"]))
            for p in nr["negotiated_prices"]:
                by.setdefault(key, {})[",".join(p.get("service_code") or [])] = float(p["negotiated_rate"])
        for key, d in by.items():
            if "11" in d and "22" in d:
                a, b = d["11"], d["22"]
                pairs["11>22" if a > b else "11<22" if a < b else "equal"] += 1
                if b > 0: ratio.append(a / b)
                if len(examples) < 6 and a != b: examples.append((item["billing_code"], item["description"][:30], a, b))
            elif "11" in d or "22" in d:
                pairs["unpaired single-POS"] += 1
print("POS-11 vs POS-22 pairs (same code, same provider set):", dict(pairs))
if ratio:
    ratio.sort(); print("   ratio 11/22: p10 %.2f  p50 %.2f  p90 %.2f" % (ratio[len(ratio)//10], ratio[len(ratio)//2], ratio[9*len(ratio)//10]))
for e in examples: print("   e.g.", e)
