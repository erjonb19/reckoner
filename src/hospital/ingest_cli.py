"""Phase 1 ingest: hospital MRF -> curated Parquet, rejects and LOAD_AUDIT.

    python -m hospital.ingest_cli --discovery out/discovery.jsonl --root data/lake

Reads each discovered MRF exactly once, streaming, and writes a filtered curated
slice. Raw files are never persisted -- rule 1, parse once and land curated.

Every batch is audited whether it succeeds or fails, and a batch that dies
mid-stream leaves nothing in the curated tree.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from hospital.codeset import CodeSet
from hospital.curate import CurateContext, Reject, curate
from hospital.landing import (
    RATE_SCHEMA,
    REJECT_SCHEMA,
    HashingStream,
    Landing,
    LoadAudit,
    new_batch_id,
    utc_now,
)
from hospital.parser import MrfParser
from hospital.streaming import MrfStream

USER_AGENT = "ny-price-transparency/0.1 (price transparency research ingest)"

#: Alert threshold. Above this share of rejected rows the load is suspect even
#: though it completed -- rule 4 says quarantine, but also says alert.
REJECT_RATE_ALERT = 0.25

#: Transient transport faults worth retrying. Observed live: NYC Health +
#: Hospitals' CDN truncates large responses mid-stream, which lost 4 of its 12
#: files on the first full run. A 404 or a parse fault is not retried -- those
#: do not get better by asking again.
TRANSIENT_ERRORS = (
    "RemoteProtocolError",
    "ReadError",
    "ReadTimeout",
    "ConnectError",
    "ConnectTimeout",
    "WriteError",
    "PoolTimeout",
    "IncompleteRead",
)
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 3.0


def is_transient(error: str | None) -> bool:
    if not error:
        return False
    return any(name in error for name in TRANSIENT_ERRORS)


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "unknown"


def ingest_one(
    client: httpx.Client,
    landing: Landing,
    hospital: str,
    url: str,
    max_rows: int | None = None,
    codes: CodeSet | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    backoff: float = BACKOFF_SECONDS,
) -> LoadAudit:
    """Ingest one MRF, retrying transient transport faults.

    The audit is appended once, for the final outcome, with the attempt count.
    """
    audit = LoadAudit(batch_id="", source_url=url, hospital=hospital, started_at=utc_now())
    for attempt in range(1, max_attempts + 1):
        audit = _ingest_attempt(client, landing, hospital, url, max_rows, codes)
        audit.attempts = attempt
        if audit.status != "failed" or not is_transient(audit.error):
            break
        if attempt < max_attempts:
            time.sleep(backoff * attempt)
    landing.append_audit(audit)
    return audit


def _ingest_attempt(
    client: httpx.Client,
    landing: Landing,
    hospital: str,
    url: str,
    max_rows: int | None = None,
    codes: CodeSet | None = None,
) -> LoadAudit:
    batch_id = new_batch_id(url)
    audit = LoadAudit(batch_id=batch_id, source_url=url, hospital=hospital, started_at=utc_now())
    rates = landing.stage_writer(batch_id, "rates", RATE_SCHEMA)
    rejects = landing.stage_writer(batch_id, "rejects", REJECT_SCHEMA)
    reasons: Counter[str] = Counter()
    codes = codes if codes is not None else CodeSet.everything()

    try:
        with client.stream("GET", url, follow_redirects=True) as response:
            response.raise_for_status()
            hashed = HashingStream(response.iter_bytes())
            stream = MrfStream(hashed, None)
            parser = MrfParser(stream)
            context = CurateContext(batch_id, url, hospital, parser.meta)

            for raw in parser:
                audit.rows_seen += 1
                if not codes.matches_any(raw.codes or ((raw.code or "", raw.code_type or ""),)):
                    # Out of scope, not invalid: filtered rows are counted but
                    # never quarantined, so the reject rate stays meaningful.
                    audit.rows_filtered += 1
                    continue
                # meta is populated as the header is consumed, so rebind it
                # rather than capturing a stale copy from before the first row.
                context = CurateContext(batch_id, url, hospital, parser.meta)
                audit.rows_in += 1
                result = curate(raw, context)
                if isinstance(result, Reject):
                    rejects.add(result)
                    reasons[result.reason] += 1
                    audit.rows_rejected += 1
                else:
                    rates.add(result)
                    audit.rows_out += 1
                if max_rows and audit.rows_seen >= max_rows:
                    break

            audit.bytes_read = hashed.bytes_read
            audit.checksum = hashed.checksum
            audit.layout = parser.meta.layout
            audit.file_vintage = parser.meta.last_updated_on

        rates.close()
        rejects.close()
        audit.reject_reasons = dict(reasons)

        prior = landing.already_loaded(url, audit.checksum or "")
        if prior:
            landing.discard(batch_id)
            audit.status = "duplicate"
            audit.error = f"identical file already loaded as {prior}"
        elif audit.rows_out == 0:
            landing.discard(batch_id)
            audit.status = "empty"
            audit.error = "no curated rows produced"
        else:
            landing.promote(batch_id, slugify(hospital), audit.file_vintage, url)
            audit.status = "ok"
    except Exception as exc:  # failure must be audited, not raised
        rates.close()
        rejects.close()
        landing.discard(batch_id)
        audit.status = "failed"
        audit.error = f"{type(exc).__name__}: {exc}"

    audit.finished_at = utc_now()
    return audit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, default=Path("out/discovery.jsonl"))
    parser.add_argument("--root", type=Path, default=Path("data/lake"))
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--limit", type=int, help="ingest only the first N MRFs")
    parser.add_argument("--max-rows", type=int, help="stop each file after N rate rows")
    parser.add_argument("--hospital", help="only this hospital (substring match)")
    parser.add_argument(
        "--codes",
        type=Path,
        default=Path("config/codes.yml"),
        help="target code set; pass --all-codes to disable filtering",
    )
    parser.add_argument("--all-codes", action="store_true")
    parser.add_argument(
        "--service-sheet",
        type=Path,
        help="derive the code filter from a contracting rate sheet (.xlsx)",
    )
    parser.add_argument(
        "--sheet-corrections",
        type=Path,
        default=Path("config/sheet_corrections.yml"),
    )
    args = parser.parse_args(argv)

    targets: list[tuple[str, str]] = []
    with args.discovery.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if args.hospital and args.hospital.casefold() not in row["hospital"].casefold():
                continue
            targets.extend((row["hospital"], url) for url in row["mrf_urls"])
    if args.limit:
        targets = targets[: args.limit]

    landing = Landing(args.root)
    orphans = landing.sweep_staging()
    if orphans:
        print(f"swept {len(orphans)} staged batches orphaned by an earlier run")
    if args.all_codes:
        codes, scope = CodeSet.everything(), "all codes"
    elif args.service_sheet:
        corrections = args.sheet_corrections if args.sheet_corrections.exists() else None
        codes = CodeSet.from_service_sheet(args.service_sheet, corrections)
        scope = f"{len(codes)} codes from {args.service_sheet.name}"
    else:
        codes = CodeSet.from_yaml(args.codes)
        scope = f"{len(codes)} target codes"
    print(f"ingesting {len(targets)} MRFs into {args.root} ({scope})\n")

    audits: list[LoadAudit] = []
    with (
        httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=900.0) as client,
        ThreadPoolExecutor(max_workers=args.workers) as pool,
    ):
        futures = [
            pool.submit(ingest_one, client, landing, hospital, url, args.max_rows, codes)
            for hospital, url in targets
        ]
        for done, future in enumerate(futures, start=1):
            audit = future.result()
            audits.append(audit)
            alert = "  <-- REJECT RATE" if audit.reject_rate > REJECT_RATE_ALERT else ""
            print(
                f"[{done:>3}/{len(targets)}] {audit.status:<9} {audit.hospital[:26]:<26} "
                f"seen {audit.rows_seen:>10,}  in {audit.rows_in:>8,}  out {audit.rows_out:>8,}  "
                f"rej {audit.reject_rate:>5.1%}{alert}"
                f"{f'  [{audit.attempts} attempts]' if audit.attempts > 1 else ''}"
                f"  {audit.error or ''}"
            )

    _summarise(audits)
    return 0


def _summarise(audits: list[LoadAudit]) -> None:
    statuses = Counter(a.status for a in audits)
    reasons: Counter[str] = Counter()
    for audit in audits:
        reasons.update(audit.reject_reasons or {})
    rows_seen = sum(a.rows_seen for a in audits)
    rows_in = sum(a.rows_in for a in audits)
    rows_out = sum(a.rows_out for a in audits)
    # A failed batch is discarded, so the rows it curated before dying never
    # reach the lake. Counting them here overstates what actually landed -- the
    # summary must agree with what a reader of the curated tree will find.
    rows_landed = sum(a.rows_out for a in audits if a.status == "ok")
    lost = rows_out - rows_landed

    print(f"\nstatus: {dict(statuses)}")
    print(
        f"rows seen {rows_seen:,}  in scope {rows_in:,}  "
        f"curated {rows_out:,}  rejected {rows_in - rows_out:,}"
    )
    print(f"rows landed in the curated tree: {rows_landed:,}")
    if lost:
        print(
            f"  ({lost:,} curated rows discarded with {statuses.get('failed', 0)} failed batches)"
        )
    if reasons:
        print("\nreject reasons:")
        for reason, count in reasons.most_common():
            print(f"  {reason:<26} {count:>10,}  {count / max(rows_in, 1):>6.2%}")


if __name__ == "__main__":
    raise SystemExit(main())
