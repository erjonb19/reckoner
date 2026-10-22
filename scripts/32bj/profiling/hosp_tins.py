"""Pass 2: service_code patterns, paired prices, and every row touching a hospital TIN."""
import sys, json, zipfile, ijson
from collections import Counter, defaultdict
zp, names_path = sys.argv[1], sys.argv[2]
names = json.load(open(names_path))
# Hospital = nonprofit with a hospital NTEE code (E20-E24) or a known hospital EIN from the HPT filenames.
HPT = {"131624096","135564934","132997301","135562304","111352310","060646597","066068853","141338467","141338586","146019179","815056290",
       "131740114","131740119","141340054","462916938","462931956"}
hosp = {e: v["name"] for e, v in names.items() if isinstance(v, dict) and (str(v.get("ntee") or "")[:3] in ("E20","E21","E22","E24") or e in HPT)}
z = zipfile.ZipFile(zp); info = z.infolist()[0]
g2tins = {}
with z.open(info) as f:
    for ref in ijson.items(f, "provider_references.item"):
        g2tins[ref["provider_group_id"]] = {pg["tin"]["value"].zfill(9) for pg in ref["provider_groups"]}
hosp_groups = {g for g, ts in g2tins.items() if ts & hosp.keys()}
present = sorted({t for g in hosp_groups for t in g2tins[g] if t in hosp})
print("hospital-coded TINs present:", len(present))
for t in present: print("   ", t, hosp[t])
sc = Counter(); per_nr = Counter(); pair = Counter(); h = defaultdict(Counter)
with z.open(info) as f:
    for item in ijson.items(f, "in_network.item"):
        for nr in item["negotiated_rates"]:
            prices = nr["negotiated_prices"]
            per_nr[len(prices)] += 1
            pats = tuple(sorted(",".join(p.get("service_code") or ["(none)"]) for p in prices))
            if len(prices) == 2:
                r = [float(p["negotiated_rate"]) for p in sorted(prices, key=lambda p: ",".join(p.get("service_code") or ["~"]))]
                pair[(pats, "first<second" if r[0] < r[1] else "first>second" if r[0] > r[1] else "equal")] += 1
            for p in prices:
                sc[",".join(p.get("service_code") or ["(none)"])] += 1
            hit = [t for r_ in nr["provider_references"] for t in g2tins[r_] if t in hosp]
            if hit:
                for p in prices:
                    h["billing_class"][p["billing_class"]] += 1
                    h["negotiated_type"][p["negotiated_type"]] += 1
                    h["code_type"][item["billing_code_type"]] += 1
                    h["service_code"][",".join(p.get("service_code") or ["(none)"])] += 1
                for t in set(hit): h["by_tin_prices"][t] += len(prices)
print("service_code pattern per price:", dict(sc))
print("prices per negotiated_rates entry:", dict(per_nr))
print("two-price entries, (service_code patterns sorted) and rate order:")
for k, v in pair.most_common(8): print("   ", v, k)
print("rows touching a hospital TIN:")
for k, c in h.items():
    if k != "by_tin_prices": print("   ", k, dict(c))
print("    prices per hospital TIN:", {f"{t} {hosp[t][:28]}": n for t, n in h["by_tin_prices"].most_common()})
