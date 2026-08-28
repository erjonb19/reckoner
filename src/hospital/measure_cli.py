"""Measure the true methodology mix for one representative MRF per health system.

    python -m hospital.measure_cli --out out/measure.jsonl

The 48 MB head-capped sweep in profile_cli answers "which payer/plan strings
exist" reliably, because those repeat early. It does NOT answer "what share of
rate lines carry a dollar amount", because these files are ordered and a prefix
is not a sample.

This pass fixes that, choosing the cheapest unbiased strategy per file:

* uncompressed CSV on a host honouring Range -> windows spread across the file
* anything compressed, or JSON -> full streaming read, since a deflate stream
  cannot be seeked into

The output feeds one decision: how much of the corpus is dollar-denominated, and
therefore how large the reconcilable universe actually is.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

from hospital.profile import MrfProfile, profile_stream
from hospital.sampling import DEFAULT_WINDOW_BYTES, DEFAULT_WINDOWS, sample_csv
from hospital.streaming import MrfStream

USER_AGENT = "reckoner/0.1 (price transparency research profiler)"

#: Below this, a full read is cheap enough that sampling adds no value.
FULL_READ_UNDER_BYTES = 64 * 1024 * 1024


def choose_targets(discovery: Path, profile: Path) -> list[tuple[str, str, str, int, str]]:
    """One median-sized MRF per hospital, with its size and container."""
    sizes: dict[str, int] = {}
    hospitals: dict[str, list[str]] = {}
    for line in discovery.open(encoding="utf-8"):
        row = json.loads(line)
        for probe in row["probes"]:
            if probe["content_length"]:
                sizes[probe["url"]] = probe["content_length"]
        if row["mrf_urls"]:
            hospitals[row["hospital"]] = row["mrf_urls"]

    containers: dict[str, str] = {}
    layouts: dict[str, str] = {}
    if profile.exists():
        for line in profile.open(encoding="utf-8"):
            row = json.loads(line)
            containers[row["url"]] = row["container"]
            layouts[row["url"]] = row["layout"]

    targets = []
    for hospital, urls in hospitals.items():
        known = [(sizes[u], u) for u in urls if u in sizes]
        if not known:
            continue
        known.sort()
        size, url = known[len(known) // 2] if len(known) % 2 else known[len(known) // 2 - 1]
        targets.append((hospital, url, containers.get(url, "?"), size, layouts.get(url, "?")))
    return targets


def measure_one(
    client: httpx.Client, hospital: str, url: str, container: str, size: int, layout: str
) -> dict[str, Any]:
    profile = MrfProfile(url=url, container=container, layout=layout)
    strategy = "full"
    try:
        windowed = (
            container == "plain"
            and layout.startswith("csv")
            and size > FULL_READ_UNDER_BYTES
            and _supports_range(client, url)
        )
        if windowed:
            strategy = "windowed"

            def fetch(start: int, end: int) -> bytes:
                response = client.get(
                    url, headers={"Range": f"bytes={start}-{end}"}, follow_redirects=True
                )
                response.raise_for_status()
                return response.content

            sample_csv(fetch, size, profile, DEFAULT_WINDOWS, DEFAULT_WINDOW_BYTES)
        else:
            with client.stream("GET", url, follow_redirects=True) as response:
                response.raise_for_status()
                profile = profile_stream(MrfStream(response.iter_bytes(), None), url)
    except Exception as exc:
        profile.error = f"{type(exc).__name__}: {exc}"

    return {
        "hospital": hospital,
        "url": url,
        "strategy": strategy,
        "container": container,
        "layout": profile.layout,
        "total_bytes": size,
        "bytes_read": profile.bytes_scanned,
        "rate_lines": profile.rate_lines,
        "value_kind": dict(profile.value_kind),
        "product_class": dict(profile.product_class),
        "joint": dict(profile.joint),
        "methodology": dict(profile.methodology),
        "pairs": dict(profile.pairs),
        "error": profile.error,
    }


def _supports_range(client: httpx.Client, url: str) -> bool:
    try:
        response = client.head(url, follow_redirects=True)
        return "bytes" in response.headers.get("accept-ranges", "").lower()
    except httpx.HTTPError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, default=Path("out/discovery.jsonl"))
    parser.add_argument("--profile", type=Path, default=Path("out/profile.jsonl"))
    parser.add_argument("--out", type=Path, default=Path("out/measure.jsonl"))
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args(argv)

    targets = choose_targets(args.discovery, args.profile)
    total_mb = sum(t[3] for t in targets) / 1e6
    print(f"measuring {len(targets)} representative MRFs ({total_mb:,.0f} MB on the wire max)\n")

    with (
        httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=600.0) as client,
        ThreadPoolExecutor(max_workers=args.workers) as pool,
    ):
        futures = [pool.submit(measure_one, client, *t) for t in targets]
        rows = []
        for done, future in enumerate(futures, start=1):
            row = future.result()
            rows.append(row)
            kinds = row["value_kind"]
            total = max(sum(kinds.values()), 1)
            print(
                f"[{done:>2}/{len(targets)}] {row['hospital'][:30]:<30} "
                f"{row['strategy']:<8} {row['bytes_read'] / 1e6:>8,.0f} MB read  "
                f"{row['rate_lines']:>9,} lines  "
                f"dollar {kinds.get('dollar', 0) / total:>6.1%}  {row['error'] or ''}"
            )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    _summarise(rows)
    return 0


def _summarise(rows: list[dict[str, Any]]) -> None:
    ok = [r for r in rows if not r["error"] and r["rate_lines"]]
    kinds: Counter[str] = Counter()
    products: Counter[str] = Counter()
    for row in ok:
        kinds.update(row["value_kind"])
        products.update(row["product_class"])
    total = max(sum(kinds.values()), 1)

    print(f"\n{len(ok)}/{len(rows)} measured; {total:,} rate lines")
    print("\nvalue kind (line-weighted across systems):")
    for kind, n in kinds.most_common():
        print(f"  {kind:<14} {n:>12,}  {n / total:>6.1%}")

    shares = [r["value_kind"].get("dollar", 0) / max(r["rate_lines"], 1) for r in ok]
    if shares:
        print(
            f"\nper-hospital dollar share: min {min(shares):.1%}  "
            f"median {statistics.median(shares):.1%}  max {max(shares):.1%}"
        )

    joint: Counter[str] = Counter()
    for row in ok:
        joint.update(row.get("joint", {}))
    jtotal = max(sum(joint.values()), 1)

    reconcilable = {"commercial", "commercial_aggregate", "exchange_individual"}
    commercial = sum(n for c, n in products.items() if c in reconcilable)
    ptotal = max(sum(products.values()), 1)
    both = sum(
        n
        for key, n in joint.items()
        if key.split("|")[0] in reconcilable and key.endswith("|dollar")
    )
    print(f"\ncommercial/exchange share of lines: {commercial / ptotal:.1%}")
    # Measured jointly, not as a product of marginals: exempt products lean far
    # harder on fee-schedule algorithms, so the two conditions are correlated.
    print(f"reconcilable (commercial AND dollar), measured: {both / jtotal:.1%}  ({both:,} lines)")

    print("\ndollar share within each product class:")
    for cls in sorted({key.split("|")[0] for key in joint}):
        in_class = sum(n for key, n in joint.items() if key.startswith(f"{cls}|"))
        dollars = joint.get(f"{cls}|dollar", 0)
        if in_class:
            print(f"  {cls:<24} {dollars / in_class:>6.1%} dollar   of {in_class:>10,} lines")


if __name__ == "__main__":
    raise SystemExit(main())
