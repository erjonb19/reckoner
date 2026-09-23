"""What fuzzy plan matching would change, measured over the real plan space.

Reads the hospital plan strings of the six reconciled systems from the local
curated lake (three columns, in batches, memory-guarded), pairs each with the
payer networks of its carrier, and compares the rules' verdict with the fuzzy
pass's. Report-only: the mart never calls the fuzzy pass.

Writes proposed labels for every combination the fuzzy pass changes to
``evals/plan_matching_proposed.jsonl``, unreviewed, so its new matches can be
scored once a person has checked them. The reviewed set does not cover them.

    PYTHONPATH=src RECKONER_STORAGE=adls ... python scripts/measure_plan_matching.py
"""

from __future__ import annotations

import collections
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.dataset as ds

from agents.entity_resolution import PayerCandidate, RuleBasedMatcher
from agents.plan_fuzzy import fuzzy_resolve
from agents.plan_resolution import PlanVerdict
from pipeline.mart import RECONCILABLE, SHARED_CODE_TYPES
from reconcile.curated import open_curated
from reconcile.silver import open_payer_silver, payer_files_from_silver
from storage import resolve

#: Payer-file carrier -> the canonical payer the hospital side resolves to.
CANONICAL = {
    "UHC": "UnitedHealthcare",
    "Aetna": "Aetna",
    "AetnaALIC": "Aetna",
    "Cigna": "Cigna",
    "Empire": "Anthem / Empire BCBS",
    "Emblem": "EmblemHealth",
}
#: Stop rather than risk the laptop: the scan is three string columns.
RSS_BUDGET_MIB = 3000


def rss_mib() -> int:
    import ctypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_uint32), ("faults", ctypes.c_uint32)] + [
            (f"f{i}", ctypes.c_size_t) for i in range(8)
        ]

    if sys.platform != "win32":
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
        return 0
    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    kernel32 = ctypes.WinDLL("kernel32")
    call = kernel32.K32GetProcessMemoryInfo
    call.restype = ctypes.c_int
    call.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_uint32]
    call(ctypes.c_void_p(kernel32.GetCurrentProcess()), ctypes.byref(counters), counters.cb)
    return int(counters.f1 // 2**20)  # WorkingSetSize


def hospital_plans(lake: Path) -> collections.Counter[tuple[str, str, str]]:
    systems = [s.hospital for s in RECONCILABLE if s.slug != "montefiore-health-system"]
    where = ds.field("hospital").isin(systems) & ds.field("code_type").isin(list(SHARED_CODE_TYPES))
    counts: collections.Counter[tuple[str, str, str]] = collections.Counter()
    for batch in open_curated(lake).to_batches(
        columns=["hospital", "payer_key", "plan_name_raw"],
        filter=where,
        batch_size=500_000,
        batch_readahead=0,
        fragment_readahead=1,
    ):
        rows = batch.to_pydict()
        for hospital, payer, plan in zip(
            rows["hospital"], rows["payer_key"], rows["plan_name_raw"], strict=True
        ):
            counts[(hospital, payer or "", plan or "")] += 1
        if rss_mib() > RSS_BUDGET_MIB:
            raise SystemExit(f"stopped: process over {RSS_BUDGET_MIB} MiB")
    return counts


def main() -> None:
    plans = hospital_plans(Path("data/lake"))
    networks: dict[str, set[str]] = collections.defaultdict(set)
    for f in payer_files_from_silver(open_payer_silver(resolve())):
        networks[CANONICAL.get(f.carrier, f.carrier)].add(f.network)
    proposals = RuleBasedMatcher().propose([PayerCandidate(p, "") for p in {k[1] for k in plans}])
    canonical = {p.key.partition(" || ")[0]: p.canonical_payer for p in proposals}

    # One verdict per plan: the best it reaches against any network its carrier
    # publishes, weighted once by the plan's rows. Weighting by row x network
    # let EmblemHealth's 99 contract files outweigh every other carrier.
    order = {"match": 3, "aggregate": 2, "no_match": 1, "unknown": 0}
    rows_by: collections.Counter[tuple[str, str, str]] = collections.Counter()
    new_matches: collections.Counter[tuple[str, str]] = collections.Counter()
    family_examples: collections.Counter[tuple[str, str]] = collections.Counter()
    reasons: dict[tuple[str, str], tuple[str, str, float, str]] = {}
    for (_, payer, plan), rows in plans.items():
        carrier = canonical.get(payer)
        if carrier not in networks or not plan:
            continue
        best_rules, best_fuzzy, family = "unknown", "unknown", False
        for network in sorted(networks[carrier]):
            fuzzy = fuzzy_resolve(plan, network)
            rules = str(fuzzy.rules.verdict)
            best_rules = max(best_rules, rules, key=order.__getitem__)
            best_fuzzy = max(best_fuzzy, str(fuzzy.verdict), key=order.__getitem__)
            family = family or fuzzy.tier == "family"
            key = (plan, network)
            if fuzzy.verdict is PlanVerdict.MATCH and rules != "match":
                new_matches[key] += rows
                reasons[key] = (rules, "match", fuzzy.confidence, fuzzy.reasoning)
            elif fuzzy.tier == "family":
                # One contract file per family stands in for all of them.
                family_key = (plan, network[:6])
                if family_key not in family_examples:
                    reasons[(plan, network)] = (rules, "unknown", fuzzy.confidence, fuzzy.reasoning)
                    family_examples[family_key] = 0
                family_examples[family_key] += rows
        rows_by[(carrier, "total", "")] += rows
        rows_by[(carrier, "rules", best_rules)] += rows
        rows_by[(carrier, "fuzzy", best_fuzzy)] += rows
        if family and best_fuzzy != "match":
            rows_by[(carrier, "family", "")] += rows

    print("Share of hospital rate rows whose plan reaches each verdict against *any*")
    print("network its carrier publishes (best verdict per plan, weighted by rows).\n")
    print("| carrier | rows | rules: match | fuzzy: match | family only | fuzzy: still unknown |")
    print("|---|---:|---:|---:|---:|---:|")
    grand: collections.Counter[str] = collections.Counter()
    for carrier in sorted({k[0] for k in rows_by}):
        total = rows_by[(carrier, "total", "")]
        cells = {
            "rules": rows_by[(carrier, "rules", "match")],
            "fuzzy": rows_by[(carrier, "fuzzy", "match")],
            "family": rows_by[(carrier, "family", "")],
            "unknown": rows_by[(carrier, "fuzzy", "unknown")],
        }
        grand["total"] += total
        for name, value in cells.items():
            grand[name] += value
        print(
            f"| {carrier} | {total:,} | {cells['rules'] / total:.2%} "
            f"| {cells['fuzzy'] / total:.2%} "
            f"| {cells['family'] / total:.2%} | {cells['unknown'] / total:.2%} |"
        )
    t = grand["total"]
    print(
        f"| **all five** | {t:,} | {grand['rules'] / t:.2%} | {grand['fuzzy'] / t:.2%} "
        f"| {grand['family'] / t:.2%} | {grand['unknown'] / t:.2%} |"
    )
    print(f"\nnew matches: {len(new_matches)} plan x network combinations")
    for (plan, network), rows in new_matches.most_common():
        rules, _, conf, why = reasons[(plan, network)]
        print(f"  {rows:>8,}  {plan!r} || {network}  ({rules} -> match, {conf:.2f}: {why})")

    stamp = datetime.now(UTC).date().isoformat()
    proposed = [(k, new_matches[k]) for k in new_matches] + [
        ((plan, next(n for (p, n) in reasons if p == plan and n.startswith(prefix))), rows)
        for (plan, prefix), rows in family_examples.most_common(20)
    ]
    out = Path("evals/plan_matching_proposed.jsonl")
    with out.open("w", encoding="utf-8") as handle:
        for (plan, network), rows in proposed:
            rules, verdict, conf, why = reasons[(plan, network)]
            handle.write(
                json.dumps(
                    {
                        "key": f"{plan} || {network}",
                        "expected": verdict,
                        "labelled_by": "fuzzy pass (proposed, unreviewed)",
                        "labelled_on": stamp,
                        "reviewed": False,
                        "note": f"rules said {rules}; fuzzy {conf:.2f}: {why}; {rows:,} rows",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    print(f"\nproposed labels: {len(proposed)} -> {out}")


if __name__ == "__main__":
    main()
