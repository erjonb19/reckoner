"""Discovery CLI: resolve cms-hpt.txt for the hospital list, then size every MRF.

    python -m discovery.cli --hospitals config/hospitals.yml --out out/discovery.jsonl

Answers Open Question 3 in docs/SPEC.md ("actual file sizes") without downloading
a single MRF. Writes one JSONL row per hospital and prints a summary table.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
import yaml

from discovery.crawler import DiscoveryResult, discover_many
from discovery.probe import ProbeResult, probe_many

USER_AGENT = "reckoner/0.1 (price transparency research crawler)"
DEFAULT_TIMEOUT = 30.0


def load_hospitals(path: Path) -> list[tuple[str, str]]:
    """Read ``(name, domain)`` pairs from the hospital list YAML."""
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = payload.get("hospitals", []) if isinstance(payload, dict) else payload
    hospitals: list[tuple[str, str]] = []
    for item in entries or []:
        if isinstance(item, str):
            hospitals.append((item, item))
        elif isinstance(item, dict) and item.get("domain"):
            domain = str(item["domain"])
            hospitals.append((str(item.get("name") or domain), domain))
    return hospitals


async def run(
    hospitals: list[tuple[str, str]],
    out_path: Path | None,
    concurrency: int,
    skip_probe: bool,
    fixture_dir: Path | None = None,
) -> int:
    limits = httpx.Limits(max_connections=concurrency * 2)
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=DEFAULT_TIMEOUT,
        limits=limits,
    ) as client:
        discoveries = await discover_many(client, [domain for _, domain in hospitals], concurrency)

        probes: dict[str, list[ProbeResult]] = {}
        if not skip_probe:
            for discovery in discoveries:
                if discovery.mrf_urls:
                    probes[discovery.domain] = await probe_many(
                        client, discovery.mrf_urls, concurrency
                    )

    if fixture_dir is not None:
        _save_fixtures(discoveries, fixture_dir)

    rows = [
        _row(name, discovery, probes.get(discovery.domain, []))
        for (name, _), discovery in zip(hospitals, discoveries, strict=True)
    ]

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    _print_summary(rows)
    return 0 if any(row["mrf_urls"] for row in rows) else 1


def _save_fixtures(discoveries: list[DiscoveryResult], fixture_dir: Path) -> None:
    """Persist every fetched cms-hpt.txt so parser tests run against real files."""
    fixture_dir.mkdir(parents=True, exist_ok=True)
    for discovery in discoveries:
        if discovery.raw_text is None:
            continue
        safe = discovery.domain.replace("/", "_").replace(":", "_")
        (fixture_dir / f"{safe}.txt").write_text(discovery.raw_text, encoding="utf-8")


def _row(name: str, discovery: DiscoveryResult, probes: list[ProbeResult]) -> dict[str, Any]:
    document = discovery.document
    return {
        "hospital": name,
        "domain": discovery.domain,
        "cms_hpt_url": discovery.url,
        "status": discovery.status,
        "error": discovery.error,
        "elapsed_ms": discovery.elapsed_ms,
        "source_format": document.source_format if document else None,
        "records": [asdict(record) for record in document.records] if document else [],
        "warnings": list(document.warnings) if document else [],
        "mrf_urls": list(discovery.mrf_urls),
        "probes": [asdict(probe) for probe in probes],
    }


def _print_summary(rows: list[dict[str, Any]]) -> None:
    print(f"{'hospital':<38} {'status':>6}  {'mrfs':>4}  {'largest':>10}  note")
    print("-" * 88)
    total_bytes = 0
    reachable = 0
    for row in sorted(rows, key=lambda r: str(r["hospital"]).lower()):
        sizes = [p["content_length"] for p in row["probes"] if p["content_length"]]
        largest = max(sizes, default=0)
        total_bytes += sum(sizes)
        if row["mrf_urls"]:
            reachable += 1
        note = row["error"] or (f"{len(row['warnings'])} parse warnings" if row["warnings"] else "")
        print(
            f"{str(row['hospital'])[:38]:<38} "
            f"{row['status'] or '-'!s:>6}  "
            f"{len(row['mrf_urls']):>4}  "
            f"{human_size(largest):>10}  {note}"
        )
    print("-" * 88)
    print(
        f"{reachable}/{len(rows)} hospitals resolved; {human_size(total_bytes)} total across MRFs"
    )


def human_size(num_bytes: int | None) -> str:
    if not num_bytes:
        return "-"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--hospitals", type=Path, help="YAML hospital list (see config/hospitals.yml)"
    )
    parser.add_argument(
        "--domain", action="append", default=[], help="probe a single domain; repeatable"
    )
    parser.add_argument("--out", type=Path, help="write JSONL results here")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument(
        "--skip-probe", action="store_true", help="resolve cms-hpt.txt but do not size MRFs"
    )
    parser.add_argument(
        "--save-fixtures",
        type=Path,
        help="write each fetched cms-hpt.txt here as a parser test fixture",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    hospitals: list[tuple[str, str]] = [(d, d) for d in args.domain]
    if args.hospitals:
        hospitals = load_hospitals(args.hospitals) + hospitals
    if not hospitals:
        print("nothing to do: pass --hospitals or --domain", file=sys.stderr)
        return 2

    return asyncio.run(
        run(hospitals, args.out, args.concurrency, args.skip_probe, args.save_fixtures)
    )


if __name__ == "__main__":
    raise SystemExit(main())
