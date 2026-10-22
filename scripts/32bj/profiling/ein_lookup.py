import json, sys, time, urllib.request, concurrent.futures as cf
prof = json.load(open(sys.argv[1])); out = sys.argv[2]
eins = set()
for p in prof:
    for k in p.get("tins", {}):
        t, v = k.split(":", 1)
        if t == "ein": eins.add(v.zfill(9))
try: cache = json.load(open(out))
except Exception: cache = {}
todo = sorted(e for e in eins if e not in cache)
def get(e):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(f"https://projects.propublica.org/nonprofits/api/v2/organizations/{e}.json", timeout=20) as r:
                o = json.load(r)["organization"]; return e, {"name": o.get("name"), "city": o.get("city"), "state": o.get("state"), "ntee": o.get("ntee_code")}
        except urllib.error.HTTPError as x:
            if x.code == 404: return e, None
            time.sleep(2 * (attempt + 1))
        except Exception:
            time.sleep(2 * (attempt + 1))
    return e, "error"
with cf.ThreadPoolExecutor(4) as ex:
    for i, (e, r) in enumerate(ex.map(get, todo)):
        cache[e] = r
        if i % 200 == 0:
            json.dump(cache, open(out, "w")); print(i, len(todo), flush=True)
json.dump(cache, open(out, "w"))
print("found", sum(1 for v in cache.values() if isinstance(v, dict)), "of", len(cache), "errors", sum(1 for v in cache.values() if v == "error"))
