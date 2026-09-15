"""Entrypoint for the scheduled Container Apps Job.

The job runs one *stage* per execution, named on the command line. Stages are
small and explicit rather than one monolithic run, because Container Apps bills
per second and a stage that fails should not drag the others down with it.

**The startup check is the reason this file has a preamble at all.** Container
Apps Consumption caps a job execution at 4 vCPU and 8 GiB, and this pipeline has
peaked at 9,808 MB -- above that ceiling. When a container exceeds its memory
limit the platform kills it, and what reaches the log is a terminated process
with no explanation: no traceback, no message, nothing to distinguish it from a
crash. That has already cost this project a full day of misdiagnosis on a local
run, where the evidence had to be recovered from Windows' Resource-Exhaustion
log after the fact.

So every execution logs, before doing any work: the memory ceiling it has been
given, the peak this stage is expected to reach, and whether the second fits
inside the first. An OOM then reads as a prediction that came true rather than a
silent kill.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime

import pyarrow

#: Peak resident memory each stage has actually been measured at, in MiB.
#: Measured, not estimated -- every figure here came off a real run, and the
#: ones that are absent are absent because nobody has measured them yet.
OBSERVED_PEAK_MIB: dict[str, int] = {
    "verify": 300,
    "manifest": 300,
    "contract": 700,
    "publish": 1200,
    "eligibility": 2900,
    # The mart is the outlier and the reason the ceiling matters: 9,808 MiB on a
    # sharded NYU Langone run. It does not fit in a Consumption job and is not
    # scheduled here; it is listed so the number is not forgotten.
    "mart": 9808,
}

STAGES = ("manifest", "contract", "verify", "publish", "eligibility")


def memory_ceiling_mib() -> int | None:
    """What this container is actually allowed, read from the cgroup.

    Read rather than assumed: the job definition says 4 GiB, but the definition
    is not what kills the process. If these ever disagree, the cgroup is right.
    """
    for path in (
        "/sys/fs/cgroup/memory.max",  # cgroup v2
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",  # v1
    ):
        try:
            with open(path) as handle:
                raw = handle.read().strip()
        except OSError:
            continue
        if raw in ("max", ""):
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        # v1 reports a sentinel near 2^63 when unlimited.
        if value > (1 << 62):
            continue
        return value // (1024 * 1024)
    return None


def peak_rss_mib() -> int | None:
    """Peak resident memory so far, or ``None`` where the platform cannot say.

    ``resource`` is Unix-only. The job runs on Linux, but this module is also
    imported on Windows during development, and a startup check that cannot be
    run locally is one nobody exercises until it matters.
    """
    try:
        import resource
    except ImportError:
        return None
    usage = resource.getrusage(resource.RUSAGE_SELF)  # type: ignore[attr-defined]
    # Linux reports kibibytes here; macOS reports bytes. The job runs on Linux.
    return int(usage.ru_maxrss) // 1024


def log(event: str, **fields: object) -> None:
    """One JSON object per line, so Log Analytics can parse it without a regex."""
    record: dict[str, object] = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "event": event,
    }
    record.update(fields)
    print(json.dumps(record, default=str), flush=True)


def preflight(stage: str) -> bool:
    """Log the ceiling against the expected peak. Returns whether it fits."""
    ceiling = memory_ceiling_mib()
    expected = OBSERVED_PEAK_MIB.get(stage)
    fits = None if (ceiling is None or expected is None) else expected < ceiling

    log(
        "preflight",
        stage=stage,
        memory_ceiling_mib=ceiling,
        expected_peak_mib=expected,
        headroom_mib=(ceiling - expected if ceiling is not None and expected is not None else None),
        fits=fits,
        note=(
            "ceiling unreadable; an OOM here will look like a silent kill"
            if ceiling is None
            else "no measured peak for this stage yet"
            if expected is None
            else "expected to fit"
            if fits
            else "EXPECTED TO EXCEED THE CEILING -- an OOM here is predicted, not mysterious"
        ),
    )
    return bool(fits) if fits is not None else True


def run_manifest() -> int:
    """Stage 1: does ADLS still match the manifests that described it?

    Both layers are checked in one execution, and a failure in one does not stop
    the other: "bronze drifted" and "bronze and silver both drifted" are
    different situations, and stopping at the first would report them the same
    way. The exit code is non-zero if either fails.

    A job reporting success with a bad diff in its logs is worse than one that
    fails: logs get read once something already looks wrong, and the execution
    status is what gets noticed first.
    """
    from pipeline import cap, manifest_check
    from storage import resolve

    location = resolve()
    status = cap.ingestion_status()

    layers = []
    ingest_date = manifest_check.latest_ingest_date(location)
    if ingest_date is None:
        log("manifest_no_baseline", layer="bronze/payer_tic", detail="no ingest_date= under _meta")
        return 1
    layers.append(manifest_check.bronze_payer(ingest_date))
    layers.append(manifest_check.SILVER_HOSPITAL)

    failed = []
    for layer in layers:
        try:
            diff = manifest_check.compare(location, layer)
        except Exception as exc:
            # An unreadable layer is a failure of the check, not an absence of
            # drift, and must not be reported as a clean run.
            log("manifest_unreadable", layer=layer.name, error=f"{type(exc).__name__}: {exc}")
            failed.append(layer.name)
            continue
        for record in manifest_check.telemetry(diff, status):
            log(record.pop("event"), **record)
        if not diff.matches:
            failed.append(layer.name)

    if failed:
        log("manifest_failed", layers=failed)
        return 1
    return 0


def run(stage: str, *, dry_run: bool) -> int:
    started = time.monotonic()
    log("stage_start", stage=stage, dry_run=dry_run)
    code = 0
    if dry_run:
        log("stage_skipped", stage=stage, reason="dry run")
    elif stage == "manifest":
        code = run_manifest()
    else:  # pragma: no cover - the remaining stages land in a later change
        log("stage_not_implemented", stage=stage)
    log(
        "stage_end",
        stage=stage,
        seconds=round(time.monotonic() - started, 2),
        peak_rss_mib=peak_rss_mib(),
        exit_code=code,
    )
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", required=True, choices=STAGES)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    log(
        "job_start",
        stage=args.stage,
        storage=os.environ.get("RECKONER_STORAGE", "local"),
        account=os.environ.get("RECKONER_ADLS_ACCOUNT", ""),
        python=sys.version.split()[0],
        # The cloud authentication path lives in pyarrow's bundled Azure SDK, so
        # which pyarrow the image resolved is a question a failed run will ask.
        pyarrow=pyarrow.__version__,
        identity=os.environ.get("AZURE_CLIENT_ID", "") or "default credential chain",
    )
    preflight(args.stage)
    code = run(args.stage, dry_run=args.dry_run)
    log("job_end", stage=args.stage, exit_code=code)
    return code


if __name__ == "__main__":
    sys.exit(main())
