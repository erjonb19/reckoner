"""Profile payer/plan composition across every discovered MRF.

    python -m hospital.profile_cli --discovery out/discovery.jsonl --out out/profile.jsonl

Streams each file, stopping after --max-mb of *decoded* bytes. Payer/plan pairs
repeat every few thousand lines, so a capped pass enumerates them reliably; the
per-file `truncated` flag marks where the percentages are a sample rather than a
census.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

from hospital.profile import PAIR_SEPARATOR, MrfProfile, profile_stream
from hospital.streaming import MrfStream

USER_AGENT = "ny-price-transparency/0.1 (price transparency research profiler)"


def profile_url(client: httpx.Client, url: str, max_bytes: int) -> MrfProfile:
    try:
        with client.stream("GET", url, follow_redirects=True) as response:
            if not response.is_success:
                return MrfProfile(url=url, error=f"HTTP {response.status_code}")
            stream = MrfStream(response.iter_bytes(), max_bytes)
            return profile_stream(stream, url)
    except Exception as exc:
        return MrfProfile(url=url, error=f"{type(exc).__name__}: {exc}")


def _serialise(hospital: str, domain: str, profile: MrfProfile) -> dict[str, Any]:
    # Built by hand rather than with dataclasses.asdict: asdict rebuilds a
    # Counter by passing an iterable of (key, count) pairs to Counter(), which
    # counts the tuples instead of restoring them, yielding tuple keys that JSON
    # cannot encode.
    return {
        "hospital": hospital,
        "domain": domain,
        "url": profile.url,
        "container": profile.container,
        "layout": profile.layout,
        "bytes_scanned": profile.bytes_scanned,
        "truncated": profile.truncated,
        "rate_lines": profile.rate_lines,
        "pairs": dict(profile.pairs),
        "methodology": dict(profile.methodology),
        "value_kind": dict(profile.value_kind),
        "product_class": dict(profile.product_class),
        "joint": dict(profile.joint),
        "error": profile.error,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, default=Path("out/discovery.jsonl"))
    parser.add_argument("--out", type=Path, default=Path("out/profile.jsonl"))
    parser.add_argument("--max-mb", type=int, default=48, help="decoded byte cap per file")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, help="profile only the first N MRFs")
    args = parser.parse_args(argv)

    targets: list[tuple[str, str, str]] = []
    with args.discovery.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            for url in row["mrf_urls"]:
                targets.append((row["hospital"], row["domain"], url))
    if args.limit:
        targets = targets[: args.limit]

    max_bytes = args.max_mb * 1024 * 1024
    print(f"profiling {len(targets)} MRFs, cap {args.max_mb} MB decoded each\n")

    results: list[dict[str, Any]] = []
    with (
        httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=180.0) as client,
        ThreadPoolExecutor(max_workers=args.workers) as pool,
    ):
        futures = [
            pool.submit(_one, client, hospital, domain, url, max_bytes)
            for hospital, domain, url in targets
        ]
        for done, future in enumerate(futures, start=1):
            row = future.result()
            results.append(row)
            flag = "!" if row["error"] else ("~" if row["truncated"] else " ")
            print(
                f"[{done:>3}/{len(targets)}]{flag} {row['hospital'][:26]:<26} "
                f"{row['layout']:<9} {row['rate_lines']:>9,} lines  "
                f"{len(row['pairs']):>3} pairs  {row['error'] or ''}"
            )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    _summarise(results)
    return 0


def _one(
    client: httpx.Client, hospital: str, domain: str, url: str, max_bytes: int
) -> dict[str, Any]:
    return _serialise(hospital, domain, profile_url(client, url, max_bytes))


def _summarise(results: list[dict[str, Any]]) -> None:
    pairs: Counter[str] = Counter()
    products: Counter[str] = Counter()
    values: Counter[str] = Counter()
    ok = [r for r in results if not r["error"]]
    for row in ok:
        pairs.update(row["pairs"])
        products.update(row["product_class"])
        values.update(row["value_kind"])

    total = sum(values.values())
    print(f"\n{len(ok)}/{len(results)} MRFs profiled; {total:,} rate lines sampled")
    print(f"distinct payer||plan strings across all hospitals: {len(pairs):,}")
    print(f"distinct payer strings: {len({p.split(PAIR_SEPARATOR)[0] for p in pairs}):,}")

    print("\nproduct class:")
    for cls, n in products.most_common():
        print(f"  {cls:<24} {n:>10,}  {n / max(total, 1):>6.1%}")
    print("\nvalue kind:")
    for kind, n in values.most_common():
        print(f"  {kind:<24} {n:>10,}  {n / max(total, 1):>6.1%}")


if __name__ == "__main__":
    raise SystemExit(main())
