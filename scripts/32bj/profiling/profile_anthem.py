"""Stream-profile one Anthem/Empire TiC in-network .json.gz with ijson, two passes.

Usage: python -I profile_anthem.py FILE.json.gz OUT_PREFIX

Pass 1 reads provider_references only and cuts the stream once the top-level
"in_network" key arrives, so the 50+ GB rate array is not parsed twice. Pass 2
streams in_network.item. Neither pass holds more than one item in memory.
Outputs OUT_PREFIX.json (profile) and OUT_PREFIX_inst.csv.gz (every target-system
price that is not professional, for later comparison).
"""
import csv
import gzip
import json
import re
import sys
import time
from collections import Counter, defaultdict

import ijson

HERE = sys.argv[3] if len(sys.argv) > 3 else "."
FILE, OUT = sys.argv[1], sys.argv[2]

# ---- target systems: EINs (explicit + ProPublica names) and organisation NPIs (NPPES + hospital headers)
EINS = {
    "montefiore": {"131740114", "131740119", "471582973", "201615393", "462931956", "462916938", "863090734", "474853506", "010796859",
                   "830621846", "472209056", "205125301", "131739937", "141340054", "131740115", "462929888", "131740130", "471600439"},
    "mount_sinai": {"131624096", "136171197", "135564934", "132997301", "135562304", "111352310", "136271888", "133838460", "812057452",
                    "450537391", "464242915", "133444730"},
    "northwell": {"112241326", "112868878", "111639818", "131740118", "131725076", "111667761", "111630914", "111661359", "111633487",
                  "131624070", "060646597", "066068853", "112965586", "453023019"},
    "nyu": {"133971298", "111633486", "472613531", "364841069", "111704595", "135562308", "371592643"},
    "st_barnabas": {"131740122"},
}
NAME_RX = {
    "northwell": re.compile(r"northwell|north shore|long island jewish|lenox hill|staten island univ|cohen children|zucker|nuvance|vassar|putnam hosp|sharon hosp|danbury|norwalk hosp|new milford|northern dutchess|plainview hosp|syosset hosp|south shore univ|huntington hosp|glen cove hosp|mather|peconic|phelps|northern westchester", re.I),
    "mount_sinai": re.compile(r"mount sinai|mt sinai|icahn|beth israel|new york eye and ear|south nassau", re.I),
    "montefiore": re.compile(r"montefiore|einstein|nyack hosp|burke rehab|white plains hosp|st lukes cornwall|mount vernon hosp|schaffer", re.I),
    "nyu": re.compile(r"\bnyu\b|langone|new york university|winthrop", re.I),
    "st_barnabas": re.compile(r"st\.? barnabas hospital|sbh health", re.I),
}
names = json.load(open(f"{HERE}/ein_names.json"))
for e, v in names.items():
    if isinstance(v, dict) and v.get("state") in ("NY", "CT", "NJ"):
        for s, rx in NAME_RX.items():
            if rx.search(v.get("name") or "") and not (s == "st_barnabas" and v.get("state") != "NY"):
                EINS[s].add(e.zfill(9))
extra = json.load(open(f"{HERE}/extra_refs.json"))
NPIS = {}
for n, v in json.load(open(f"{HERE}/npi_refs.json")).items():
    sysn = v["system"]
    if sysn == "hospital_file_header":
        continue
    NPIS[str(n)] = (sysn, v.get("name"))
for n, v in extra["npis"].items():
    NPIS[str(n)] = (v["system"], v.get("name"))
# hospital-file headers: EIN-NPI filename convention in our lake source URLs (public)
for n, s in [("1003923434", "montefiore"), ("1427616002", "nyu"), ("1669578324", "nyu"), ("1619341716", "nyu"), ("1801992631", "nyu")]:
    NPIS.setdefault(n, (s, "hospital file header"))
EIN2SYS = {e: s for s, es in EINS.items() for e in es}


class CutAt:
    """File wrapper that reports EOF once the top-level in_network key has been passed."""

    def __init__(self, f, marker=b'"in_network"'):
        self.f, self.m, self.done, self.tail = f, marker, False, b""

    def read(self, n=-1):
        if self.done or n == 0:  # ijson probes with read(0) and discards the result
            return b""
        b = self.f.read(n if n and n > 0 else 1 << 20)
        i = (self.tail + b).find(self.m)
        if i >= 0:
            # the text before the key ends "...],"; closing the object keeps the JSON valid
            self.done = True
            return b[: max(0, i - len(self.tail))] + b'"_cut":0}'
        self.tail = b[-len(self.m):]
        return b


def code_shape(c):
    c = str(c).strip().upper()
    for rx, lab in ((r"\d{5}", "5-digit"), (r"\d{4}[A-Z]", "4+letter"), (r"[A-V]\d{4}", "letter+4"), (r"\d{3}", "3-digit"), (r"\d{4}", "4-digit")):
        if re.fullmatch(rx, c):
            return lab
    return "other"


t0 = time.time()
with gzip.open(FILE, "rb") as f:
    head = f.read(4096).decode("utf-8", "replace")
top = dict(re.findall(r'"(reporting_entity_name|reporting_entity_type|last_updated_on|version|plan_name|plan_id|plan_id_type|plan_market_type)"\s*:\s*"([^"]*)"', head))

# ---- pass 1: provider_references
tin_types = Counter()
tins_seen = set()
npis_seen = set()
g_net = {}
g_hits = {}  # group id -> {system: set(evidence)}
n_groups = 0
with gzip.open(FILE, "rb") as raw:
    try:
        for ref in ijson.items(CutAt(raw), "provider_references.item"):
            n_groups += 1
            gid = ref["provider_group_id"]
            g_net[gid] = sys.intern("|".join(ref.get("network_name") or []))
            hits = defaultdict(set)
            for pg in ref.get("provider_groups") or []:
                t = pg.get("tin") or {}
                tt, tv = t.get("type"), str(t.get("value"))
                tin_types[tt] += 1
                key = f"{tt}:{tv.zfill(9) if tt == 'ein' else tv}"
                tins_seen.add(key)
                if tt == "ein" and tv.zfill(9) in EIN2SYS:
                    hits[EIN2SYS[tv.zfill(9)]].add("ein:" + tv.zfill(9))
                if tt == "npi" and tv in NPIS:
                    hits[NPIS[tv][0]].add(tv)
                for n in pg.get("npi") or []:
                    n = str(n)
                    npis_seen.add(int(n))
                    if n in NPIS:
                        hits[NPIS[n][0]].add(n)
            if hits:
                g_hits[gid] = {k: v for k, v in hits.items()}
    except ijson.IncompleteJSONError as e:
        print("pass1 parse error (not expected):", e, file=sys.stderr)
print(f"pass1 {n_groups} groups, {len(tins_seen)} TINs, {len(npis_seen)} NPIs, {len(g_hits)} target groups, {time.time()-t0:.0f}s", file=sys.stderr, flush=True)

# ---- pass 2: in_network
c = defaultdict(Counter)
codes = defaultdict(set)
tgt = defaultdict(Counter)  # (system) -> Counter over (code_type, billing_class, negotiated_type)
tgt_codes = defaultdict(set)
tgt_ev = defaultdict(Counter)  # system -> evidence key -> prices
used = set()
n_items = n_prices = inline = 0
inst_out = gzip.open(f"{OUT}_inst.csv.gz", "wt", newline="")
w = csv.writer(inst_out)
w.writerow(["system", "evidence", "network", "billing_code_type", "billing_code", "billing_class", "negotiated_type", "setting", "negotiated_rate", "service_code", "modifier", "arrangement", "expiration_date"])
with gzip.open(FILE, "rb") as f:
    for item in ijson.items(f, "in_network.item"):
        n_items += 1
        if n_items % 20000 == 0:
            print(f"  {n_items} items, {n_prices} prices, {time.time()-t0:.0f}s", file=sys.stderr, flush=True)
        bct, code = item.get("billing_code_type"), str(item.get("billing_code"))
        c["billing_code_type"][bct] += 1
        c["declared_vs_shape"][f"{bct} | {code_shape(code)}"] += 1
        c["negotiation_arrangement"][item.get("negotiation_arrangement")] += 1
        codes[bct].add(code)
        if item.get("bundled_codes"):
            c["has"]["bundled_codes"] += 1
        for nr in item.get("negotiated_rates") or []:
            refs = nr.get("provider_references") or []
            used.update(refs)
            if nr.get("provider_groups"):
                inline += 1
            sys_hit = defaultdict(set)
            nets = set()
            for r in refs:
                nets.add(g_net.get(r, "?"))
                for s, ev in (g_hits.get(r) or {}).items():
                    sys_hit[s].update(ev)
            for pg in nr.get("provider_groups") or []:  # inline groups, if any
                t = pg.get("tin") or {}
                tv = str(t.get("value"))
                if t.get("type") == "ein" and tv.zfill(9) in EIN2SYS:
                    sys_hit[EIN2SYS[tv.zfill(9)]].add(f"ein:{tv.zfill(9)}")
                if t.get("type") == "npi" and tv in NPIS:
                    sys_hit[NPIS[tv][0]].add(f"npi:{tv}")
            for pr in nr.get("negotiated_prices") or []:
                n_prices += 1
                bc, nt = pr.get("billing_class"), pr.get("negotiated_type")
                c["negotiated_type"][nt] += 1
                c["billing_class"][bc] += 1
                c["setting"][pr.get("setting")] += 1
                c["type_x_class"][f"{bct} | {bc} | {nt}"] += 1
                for nn in nets:
                    c["network_prices"][nn] += 1
                for s, ev in sys_hit.items():
                    tgt[s][f"{bct} | {bc} | {nt}"] += 1
                    tgt_codes[(s, bct, bc)].add(code)
                    for e in ev:
                        tgt_ev[s][f"{e} | {bc}"] += 1
                    if bc != "professional":
                        w.writerow([s, ";".join(sorted(ev))[:300], ";".join(sorted(nets)), bct, code, bc, nt, pr.get("setting"), pr.get("negotiated_rate"),
                                    ",".join(pr.get("service_code") or []), ",".join(pr.get("billing_code_modifier") or []), item.get("negotiation_arrangement"), pr.get("expiration_date")])
inst_out.close()

prof = {
    "file": FILE, "top": top, "n_provider_groups": n_groups, "unreferenced_groups": n_groups - len(used & set(g_net)),
    "tin_type_counts": dict(tin_types), "distinct_tins": len(tins_seen), "distinct_tins_by_type": dict(Counter(k.split(":")[0] for k in tins_seen)),
    "distinct_npis": len(npis_seen), "n_in_network_items": n_items, "n_negotiated_prices": n_prices, "inline_provider_group_rates": inline,
    "counts": {k: dict(v.most_common()) for k, v in c.items()},
    "distinct_codes": {str(k): len(v) for k, v in codes.items()},
    "target_groups": {s: sum(1 for h in g_hits.values() if s in h) for s in EINS},
    "target_prices": {s: dict(v.most_common()) for s, v in tgt.items()},
    "target_distinct_codes": {f"{s} | {b} | {bc}": len(v) for (s, b, bc), v in tgt_codes.items()},
    "target_evidence": {s: dict(v.most_common()) for s, v in tgt_ev.items()},
    "seconds": round(time.time() - t0),
}
json.dump(prof, open(f"{OUT}.json", "w"), indent=1, default=str)
print("done", FILE, prof["seconds"], "s", file=sys.stderr)
