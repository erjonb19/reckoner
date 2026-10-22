import csv, json, sys, glob, time, urllib.request, urllib.parse
out = sys.argv[1]; inbox = sys.argv[2]
queries = {
 "montefiore": ["montefiore*", "einstein*", "albert einstein*", "nyack hospital*", "white plains hospital*", "burke rehabilitation*", "st lukes cornwall*", "st. lukes cornwall*", "mount vernon hospital*", "new rochelle hospital*", "schaffer extended*"],
 "mount_sinai": ["mount sinai*", "icahn school*", "beth israel medical*", "new york eye and ear*", "south nassau*"],
 "northwell": ["northwell*", "north shore*", "long island jewish*", "lenox hill*", "staten island university*", "nuvance*"],
}
res = {}
def fetch(q, skip):
    u = "https://npiregistry.cms.hhs.gov/api/?" + urllib.parse.urlencode({"version": "2.1", "organization_name": q, "state": "NY", "enumeration_type": "NPI-2", "limit": 200, "skip": skip})
    with urllib.request.urlopen(u, timeout=30) as r: return json.load(r).get("results", [])
for sysname, qs in queries.items():
    for q in qs:
        for skip in range(0, 1001, 200):
            try: rs = fetch(q, skip)
            except Exception as e: print("err", q, skip, e); break
            for r in rs:
                b = r["basic"]; res[str(r["number"])] = {"system": sysname, "query": q, "name": b.get("organization_name")}
            if len(rs) < 200: break
            time.sleep(0.3)
        print(sysname, q, sum(1 for v in res.values() if v["query"] == q), flush=True)
# hospital file headers (v3 type_2_npi)
for f in glob.glob(inbox + "/*standardcharges*.csv"):
    with open(f, encoding="utf-8", errors="replace") as fh:
        rd = csv.reader(fh); h = next(rd); v = next(rd)
    if "type_2_npi" in h:
        for n in v[h.index("type_2_npi")].split("|"):
            n = n.strip()
            if n: res.setdefault(n, {"system": "hospital_file_header", "query": f.split("/")[-1].split("\\")[-1], "name": v[0]})
json.dump(res, open(out, "w"), indent=0)
print(len(res))
