"""NPPES org NPIs and ProPublica EINs for NYU Langone and St. Barnabas (public APIs only).

Usage: python -I extra_refs.py OUT.json
"""
import json, sys, urllib.request, urllib.parse, time
def get(url):
    with urllib.request.urlopen(url, timeout=30) as r: return json.load(r)
npis = {}
for sysn, q in [("nyu","NYU LANGONE*"),("nyu","NYU GROSSMAN*"),("nyu","NYU WINTHROP*"),("nyu","NEW YORK UNIVERSITY*"),("nyu","NYU HOSPITALS*"),
                ("st_barnabas","ST BARNABAS HOSPITAL*"),("st_barnabas","SBH HEALTH*"),("st_barnabas","SAINT BARNABAS HOSPITAL*"),("st_barnabas","ST. BARNABAS HOSPITAL*")]:
    skip = 0
    while skip <= 1000:
        u = "https://npiregistry.cms.hhs.gov/api/?" + urllib.parse.urlencode({"version":"2.1","organization_name":q,"state":"NY","limit":200,"skip":skip})
        res = get(u).get("results", [])
        for r in res: npis[r["number"]] = {"system": sysn, "query": q, "name": r["basic"].get("organization_name")}
        if len(res) < 200: break
        skip += 200; time.sleep(0.3)
eins = {}
for sysn, q in [("nyu","NYU Langone"),("nyu","New York University"),("nyu","NYU Winthrop"),("st_barnabas","St Barnabas Hospital"),("st_barnabas","SBH Health")]:
    u = "https://projects.propublica.org/nonprofits/api/v2/search.json?" + urllib.parse.urlencode({"q": q, "state[id]": "NY"})
    try:
        for o in get(u).get("organizations", []): eins[str(o["ein"]).zfill(9)] = {"system": sysn, "name": o["name"], "ntee": o.get("ntee_code")}
    except Exception as e: print("err", q, e)
    time.sleep(0.5)
json.dump({"npis": npis, "eins": eins}, open(sys.argv[1], "w"), indent=1)
from collections import Counter
print(Counter(v["system"] for v in npis.values()))
for e, v in eins.items(): print(e, v)
