"""Stream-profile TiC in-network and allowed-amounts files inside zips with ijson.

Usage: python -I profile_tic.py OUT.json ZIP [ZIP ...]
Never materialises the in_network / out_of_network arrays: each item is read,
tallied and dropped.
"""
import gzip
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict

import ijson


def code_shape(code: str) -> str:
    c = str(code).strip().upper()
    if re.fullmatch(r"\d{5}", c):
        return "5-digit numeric (CPT cat I)"
    if re.fullmatch(r"\d{4}[A-Z]", c):
        return "4-digit+letter (CPT cat II/III/PLA)"
    if re.fullmatch(r"[A-V]\d{4}", c):
        return "letter+4 (HCPCS II)"
    if re.fullmatch(r"\d{3}", c):
        return "3-digit (DRG or RC)"
    if re.fullmatch(r"\d{4}", c):
        return "4-digit (RC or APR-DRG)"
    return "other"


def opener(z, info):
    f = z.open(info)
    return gzip.open(f) if info.filename.endswith(".gz") else f


def top_scalars(z, info):
    out = {}
    with opener(z, info) as f:
        for prefix, event, value in ijson.parse(f):
            if "." not in prefix and prefix and event in ("string", "number", "boolean", "null"):
                out[prefix] = value if not hasattr(value, "as_tuple") else float(value)
            if prefix == "" and event == "map_key":
                out.setdefault("_keys", []).append(value)
    return out


def tin_key(tin):
    return (tin.get("type"), str(tin.get("value")))


def profile_in_network(z, info):
    p = {"file": info.filename, "uncompressed_bytes": info.file_size}
    p["top"] = top_scalars(z, info)

    groups = {}
    with opener(z, info) as f:
        for ref in ijson.items(f, "provider_references.item"):
            tins, npis = [], set()
            for g in ref.get("provider_groups", []):
                t = g.get("tin", {})
                tins.append({"type": t.get("type"), "value": str(t.get("value")), "business_name": t.get("business_name")})
                npis.update(str(n) for n in g.get("npi", []))
            groups[ref["provider_group_id"]] = {"network_name": ref.get("network_name"), "tins": tins, "npis": sorted(npis), "location": ref.get("location")}

    c = defaultdict(Counter)
    codes = defaultdict(set)
    used_refs = Counter()
    inline_groups = 0
    n_items = n_prices = 0
    rates_by_type = defaultdict(list)
    exp = Counter()
    with opener(z, info) as f:
        for item in ijson.items(f, "in_network.item"):
            n_items += 1
            bct = item.get("billing_code_type")
            c["billing_code_type"][bct] += 1
            c["declared_vs_shape"][(bct, code_shape(item.get("billing_code")))] += 1
            c["negotiation_arrangement"][item.get("negotiation_arrangement")] += 1
            codes[bct].add(str(item.get("billing_code")))
            if item.get("bundled_codes"):
                c["has"]["bundled_codes"] += 1
            if item.get("covered_services"):
                c["has"]["covered_services"] += 1
            for nr in item.get("negotiated_rates", []):
                for r in nr.get("provider_references", []) or []:
                    used_refs[r] += 1
                if nr.get("provider_groups"):
                    inline_groups += 1
                for pr in nr.get("negotiated_prices", []):
                    n_prices += 1
                    c["negotiated_type"][pr.get("negotiated_type")] += 1
                    c["billing_class"][pr.get("billing_class")] += 1
                    c["setting"][pr.get("setting")] += 1
                    c["type_x_class"][(bct, pr.get("billing_class"), pr.get("negotiated_type"))] += 1
                    if pr.get("billing_code_modifier"):
                        c["has"]["billing_code_modifier"] += 1
                    exp[pr.get("expiration_date")] += 1
                    rv = pr.get("negotiated_rate")
                    if rv is not None:
                        rates_by_type[(bct, pr.get("negotiated_type"))].append(float(rv))
    p["n_provider_groups"] = len(groups)
    p["n_in_network_items"] = n_items
    p["n_negotiated_prices"] = n_prices
    p["inline_provider_groups"] = inline_groups
    p["counts"] = {k: {" | ".join(map(str, kk)) if isinstance(kk, tuple) else str(kk): v for kk, v in cnt.most_common()} for k, cnt in c.items()}
    p["distinct_codes"] = {str(k): len(v) for k, v in codes.items()}
    p["sample_codes"] = {str(k): sorted(v)[:15] for k, v in codes.items()}
    p["expiration_dates"] = dict(exp.most_common(10))
    stats = {}
    for k, v in rates_by_type.items():
        v.sort()
        stats[" | ".join(map(str, k))] = {"n": len(v), "min": v[0], "p50": v[len(v) // 2], "max": v[-1], "zero": sum(1 for x in v if x == 0)}
    p["rate_stats"] = stats
    # TIN roll-up: which TINs, how many NPIs, how many price rows reference them
    tin_roll = {}
    for gid, g in groups.items():
        for t in g["tins"]:
            key = f'{t["type"]}:{t["value"]}'
            r = tin_roll.setdefault(key, {"business_names": set(), "npis": set(), "groups": 0, "rate_refs": 0, "networks": set()})
            r["business_names"].add(t["business_name"])
            r["npis"].update(g["npis"])
            r["groups"] += 1
            r["rate_refs"] += used_refs.get(gid, 0)
            r["networks"].update(g["network_name"] or [])
    p["tins"] = {k: {"business_names": sorted(map(str, v["business_names"])), "n_npis": len(v["npis"]), "npis": sorted(v["npis"]), "groups": v["groups"], "rate_refs": v["rate_refs"], "networks": sorted(v["networks"])} for k, v in sorted(tin_roll.items(), key=lambda kv: -kv[1]["rate_refs"])}
    p["unreferenced_groups"] = sum(1 for gid in groups if gid not in used_refs)
    return p


def profile_oon(z, info):
    p = {"file": info.filename, "uncompressed_bytes": info.file_size, "compressed_bytes": info.compress_size}
    p["top"] = top_scalars(z, info)
    c = defaultdict(Counter)
    tins = defaultdict(lambda: {"npis": set(), "rows": 0})
    n_items = n_pay = 0
    with opener(z, info) as f:
        for item in ijson.items(f, "out_of_network.item"):
            n_items += 1
            c["billing_code_type"][item.get("billing_code_type")] += 1
            c["declared_vs_shape"][(item.get("billing_code_type"), code_shape(item.get("billing_code")))] += 1
            for aa in item.get("allowed_amounts", []):
                c["billing_class"][aa.get("billing_class")] += 1
                t = aa.get("tin", {})
                key = f'{t.get("type")}:{t.get("value")}'
                for pay in aa.get("payments", []):
                    n_pay += 1
                    tins[key]["rows"] += 1
                    for prov in pay.get("providers", []):
                        tins[key]["npis"].update(str(n) for n in prov.get("npi", []))
    p["n_out_of_network_items"] = n_items
    p["n_payments"] = n_pay
    p["counts"] = {k: {" | ".join(map(str, kk)) if isinstance(kk, tuple) else str(kk): v for kk, v in cnt.most_common()} for k, cnt in c.items()}
    p["tins"] = {k: {"rows": v["rows"], "npis": sorted(v["npis"])} for k, v in tins.items()}
    return p


def main():
    out_path, zips = sys.argv[1], sys.argv[2:]
    results = []
    for zp in zips:
        z = zipfile.ZipFile(zp)
        for info in z.infolist():
            name = info.filename
            if name.endswith("toc.json"):
                with z.open(info) as f:
                    results.append({"zip": zp, "file": name, "toc": json.load(f)})
                continue
            prof = profile_oon(z, info) if name.startswith("oon_") else profile_in_network(z, info)
            prof["zip"] = zp
            prof["zip_entry_compressed_bytes"] = info.compress_size
            results.append(prof)
            print("done", name, file=sys.stderr)
    with open(out_path, "w") as f:
        json.dump(results, f, indent=1, default=str)


if __name__ == "__main__":
    main()
