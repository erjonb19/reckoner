"""Distinct payer/plan strings for Mount Sinai and Northwell, the 32BJ term search, and per-file payer lists."""
import csv, json, re, sys
import duckdb
out_dir = sys.argv[1]
con = duckdb.connect(); con.execute("set enable_progress_bar=false"); con.execute("set threads=8")
base = "read_parquet('data/lake/curated/hospital_rates/**/*.parquet', hive_partitioning=true)"
rows = con.execute(f"""
  select hospital_slug, regexp_extract(source_url, '[^/]+$') as file, file_vintage, payer_name_raw, plan_name_raw, count(*) n
  from {base} where hospital_slug in ('mount-sinai-health-system','northwell-health') group by all""").fetchall()
TERMS = {
    "32": r"32", "BJ": r"bj", "Local 32": r"local\s*32", "32 BJ": r"32\s+bj", "32-BJ": r"32-bj",
    "Building Service": r"building\s*serv", "SEIU (not 1199)": r"seiu", "Healthcare Highways": r"healthcare\s*highways",
    "HCH": r"hch", "ClearCost": r"clear\s*cost", "Taft": r"taft", "union": r"union", "fund": r"fund",
}
def hits(payer, plan):
    s = f"{payer or ''} | {plan or ''}"
    found = [t for t, p in TERMS.items() if re.search(p, s, re.I)]
    if "SEIU (not 1199)" in found and "1199" in s:
        found.remove("SEIU (not 1199)")
    return found
# 2. matches, aggregated across files per system
agg = {}
for slug, f, v, payer, plan, n in rows:
    k = (slug, payer, plan); a = agg.setdefault(k, {"n": 0, "files": 0}); a["n"] += n; a["files"] += 1
print("== term matches (system | payer_name | plan_name | rows | files | terms)")
nm = 0
for (slug, payer, plan), a in sorted(agg.items()):
    h = hits(payer, plan)
    if h:
        nm += 1; print(f"{slug} | {payer!r} | {plan!r} | {a['n']:,} | {a['files']} | {', '.join(h)}")
print("matching distinct pairs:", nm, "of", len(agg))
# 3. distinct payer per file
per = {}
for slug, f, v, payer, plan, n in rows:
    per.setdefault((slug, f, v), {}).setdefault(payer, 0); per[(slug, f, v)][payer] += n
with open(f"{out_dir}/payer_names_by_file.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh); w.writerow(["system", "file", "file_vintage", "payer_name_raw", "rows"])
    for (slug, f, v), d in sorted(per.items()):
        for payer, n in sorted(d.items(), key=lambda kv: -kv[1]): w.writerow([slug, f, v, payer, n])
with open(f"{out_dir}/payer_plan_pairs.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh); w.writerow(["system", "payer_name_raw", "plan_name_raw", "rows", "files", "terms"])
    for (slug, payer, plan), a in sorted(agg.items()): w.writerow([slug, payer, plan, a["n"], a["files"], ";".join(hits(payer, plan))])
# Does each system's payer list repeat across files? Print the distinct payer lists grouped by identical sets.
print("== distinct payer_name sets per file")
groups = {}
for (slug, f, v), d in sorted(per.items()):
    groups.setdefault((slug, tuple(sorted(d, key=str))), []).append(f)
for (slug, payers), files in groups.items():
    print(f"\n{slug}: {len(files)} file(s) share this list of {len(payers)} payers: {', '.join(files)}")
    tot = {p: sum(per[(slug, f2, v2)].get(p, 0) for (s2, f2, v2) in per if s2 == slug and f2 in files) for p in payers}
    for p, n in sorted(tot.items(), key=lambda kv: -kv[1]): print(f"   {n:>10,}  {p}")
