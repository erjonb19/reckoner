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
    # The worst measured peak of a system that completes: Northwell, 6,424 MiB
    # in a 8,192 MiB container, one system per execution. The unsharded figure
    # was 9,808 and is kept in ADR 0004 with the rest of the table.
    #
    # NYU Langone does not complete at any setting -- it exceeds the Consumption
    # ceiling in a process containing nothing else -- so for that one system
    # this number is optimistic and the preflight will say "fits" before an OOM.
    # Recorded here rather than inflated to cover it, because a figure chosen to
    # make the check pessimistic would stop being a measurement (issue #47).
    "mart": 6424,
    # Reads gold: a few hundred rows across seven tables, then writes CSV.
    "report": 300,
    # Reads gold/exemplars and classifies it. Hundreds of rows.
    "triage": 300,
}

STAGES = (
    "manifest",
    "mart",
    "triage",
    "report",
    "contract",
    "verify",
    "publish",
    "eligibility",
)


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


def arrow_memory() -> tuple[int | None, int | None, str]:
    """Arrow's live bytes, its high-water mark, and which allocator is in use.

    Both numbers, because one of them alone misled this project through four
    changes. ``total_allocated_bytes`` is what Arrow holds *at this instant*,
    so sampling it between slices -- when everything has been released -- reads
    0 however much passed through in between. A true answer to a question
    nobody was asking. ``max_memory`` is the pool's high-water mark, and is
    what "did Arrow use a lot of memory" actually means.

    The backend name rides along because the reason for reading these is to
    tell whether an allocator change took effect, and ARROW_DEFAULT_MEMORY_POOL
    is ignored silently when the backend is not compiled in -- so a variable
    that did nothing would otherwise look exactly like one that worked.
    """
    try:
        import pyarrow as pa

        pool = pa.default_memory_pool()
        mib = 1024 * 1024
        return (
            int(pa.total_allocated_bytes() / mib),
            int(pool.max_memory() / mib),
            str(pool.backend_name),
        )
    except Exception:
        return (None, None, "unknown")


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
    layers.append(manifest_check.SILVER_PAYER)
    layers.append(manifest_check.GOLD)

    # Stated up front rather than left to be inferred from which records turned
    # up. A run that checked two layers and a run that checked three both look
    # like success; the difference is only visible if the run says so.
    log("manifest_layers", layers=[layer.name for layer in layers], count=len(layers))

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

    # Recorded where stage 2 can read it. The mart builds gold from these same
    # layers, and gold built from drifted layers is full, plausible and wrong.
    from pipeline import gate

    verdict = {layer.name: layer.name not in failed for layer in layers}
    try:
        where = gate.write_status(
            location, verdict, build_sha=os.environ.get("RECKONER_BUILD_SHA", "")
        )
        log("manifest_status_written", path=where, ok=not failed)
    except Exception as exc:
        # Not fatal: the check itself succeeded and its result is in the log.
        # Failing the run because the note could not be left would turn a
        # bookkeeping problem into an outage.
        log("manifest_status_unwritable", error=f"{type(exc).__name__}: {exc}")

    if failed:
        log("manifest_failed", layers=failed)
        return 1
    return 0


def run_mart() -> int:
    """Stage 2: reconcile silver into gold.

    Sharded because it has to be: unsharded this peaked at 9,808 MiB against a
    4,096 MiB ceiling, and a container that exceeds its limit is killed with no
    traceback and nothing to distinguish it from a crash.
    """
    from pipeline import gate, mart
    from pipeline.memwatch import MemoryWatch
    from reconcile.gold import Reconciliation
    from storage import publish, resolve

    location = resolve()

    # The mart reads the layers stage 1 checks. If they drifted, gold built from
    # them is wrong in a way nothing downstream can detect -- the report, the
    # page and any human reading them would quote it. So a failed drift check
    # stops the run. An absent or stale verdict does not: that is "we do not
    # know", and refusing on it would make this impossible to deploy on a lake
    # where stage 1 has never run.
    verdict = gate.check(location)
    log(
        "mart_precheck",
        state=verdict.state,
        detail=verdict.detail,
        checked_at=verdict.checked_at,
        may_run=verdict.may_run,
    )
    if not verdict.may_run:
        return 1

    # Sampled every second for the length of the stage. The kernel high-water
    # cannot say when a peak happened, and a figure read once per slice cannot
    # see inside one -- two containers were killed having last reported a
    # comfortable number because the fatal moment fell between samples.
    watch = MemoryWatch().start()
    log("memwatch_started", available=watch.available, interval_seconds=1.0)

    def shard_done(spec: mart.SystemSpec, shard: str, left: int, right: int, pairs: int) -> None:
        arrow_live, arrow_peak, _ = arrow_memory()
        log(
            "mart_shard",
            # The largest RSS actually observed during this slice, as opposed to
            # the process high-water, which never falls and so says nothing
            # about which slice was expensive.
            rss_window_peak_mib=watch.reset(),
            system=spec.system,
            shard=shard,
            hospital_rates=left,
            payer_rates=right,
            pairs=pairs,
            peak_rss_mib=peak_rss_mib(),
            # Live and high-water side by side. Live alone reads 0 between
            # slices however much was allocated in between; high-water alone
            # cannot show whether any of it came back.
            arrow_live_mib=arrow_live,
            arrow_peak_mib=arrow_peak,
        )

    def system_done(run: Reconciliation) -> None:
        log(
            "mart_system",
            system=run.system,
            hospital_slug=run.hospital_slug,
            hospital_rates=run.hospital_rates,
            payer_rates=run.payer_rates,
            pairs_formed=run.pairs_formed,
            comparable_share=round(run.comparable_share, 6),
            material=run.material,
            unexplained_and_material=len(run.residual),
            systematic_offsets=len(run.offsets),
            facilities=len(run.facilities),
            # Recorded per system: without it two of the four reconcilable
            # systems refuse every candidate, so a reader comparing runs needs
            # to know which ones it was applied to.
            assumed_facility_when_unstated=run.assumed_facility_when_unstated,
            peak_rss_mib=peak_rss_mib(),
        )

    # One system per execution is how this fits. Each execution is a fresh
    # process, so nothing inherits the previous system's retained pages or its
    # accumulated state -- which is what the measured per-system peaks say the
    # problem was: 4,748 MiB for Mount Sinai alone against 6,746 by the time
    # Northwell ran after it in the same process.
    only = os.environ.get("RECKONER_SYSTEM", "").strip() or None
    log(
        "mart_selection", system=only or "all four", source="RECKONER_SYSTEM" if only else "default"
    )

    def shard_split(prefix: str, rows: int, children: int) -> None:
        log("mart_subsharded", prefix=prefix, payer_rows=rows, children=children)

    runs = mart.build(
        location,
        only=only,
        on_shard=shard_done,
        on_system=system_done,
        on_plan=shard_split,
    )
    if not runs:
        log("mart_no_systems", detail="nothing reconcilable; silver may be missing")
        return 1

    built = mart.tables(runs)
    written = mart.write(location, built)
    log("mart_written", systems=[run.hospital_slug for run in runs], **written)

    # Same manifest-and-verify shape as the two silver layers, so one stage-1
    # check covers all four without a special case.
    # The manifest describes the whole gold tree, because that is what stage 1
    # checks, but only the partitions this execution wrote were intended by it.
    # Naming them is the difference between a manifest that says "gold is
    # correct" and one that says "gold is correct and I am the reason for this
    # part of it" -- and after a per-system run, only the second is true.
    manifest = publish.build_manifest(
        location.child(*mart.GOLD_ROOT),
        [
            publish.PublishResult(
                subject=name,
                destination=name,
                rows_read=rows,
                rows_written=rows,
                partitions=len(runs),
            )
            for name, rows in written.items()
        ],
        layer="gold",
        group_key="hospital_slug",
        verify_only={run.hospital_slug for run in runs},
    )
    log("memwatch_peak", sampled_peak_rss_mib=watch.stop(), samples=watch.samples)
    manifest["systems_written"] = [run.hospital_slug for run in runs]
    manifest["complete"] = sorted(manifest["by_hospital_slug"]) == sorted(
        spec.slug for spec in mart.RECONCILABLE
    )
    where = publish.write_manifest(location, manifest, path=mart.GOLD_MANIFEST)
    log(
        "mart_manifest",
        path=where,
        files=manifest["files"],
        rows=manifest["rows"],
        megabytes=manifest["megabytes"],
        verified=manifest["verified"],
        systems_written=manifest["systems_written"],
        complete=manifest["complete"],
    )
    return 0 if manifest["verified"] else 1


def run_report() -> int:
    """Stage 3: publish gold as the summary dataset and the written report.

    Writes to ADLS and to the working tree. The committed copy is what the page
    reads -- it makes no network calls, which is the strongest form of "no
    credentials in the app" -- and ``run.json`` carries the vintages, so a stale
    snapshot cannot render as a current one.
    """
    from pathlib import Path

    from pipeline import report
    from storage import resolve

    location = resolve()

    # Provenance a reader cannot recover from the data: which gold partitions
    # came from a cloud execution and which from a local run. The tables look
    # identical either way and the difference is real, so it is stated.
    caveats = (
        "Gold for NYU Langone comes from a local run; Mount Sinai, Northwell and "
        "NewYork-Presbyterian come from cloud executions of reckoner-mart. NYU "
        "Langone exceeds the 8 GiB Consumption ceiling; tracked in issue #47.",
    )
    summary = report.build(
        location, build_sha=os.environ.get("RECKONER_BUILD_SHA", ""), caveats=caveats
    )
    if not summary.tables.get("coverage"):
        log("report_no_gold", detail="gold/coverage is empty; run --stage mart first")
        return 1

    remote = report.write_remote(summary, location)
    local = report.write_local(summary, Path("summary"))
    report_path = Path(*report.REPORT_PATH)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report.markdown(summary), encoding="utf-8")

    log(
        "report_written",
        rows=summary.rows(),
        remote=len(remote),
        local=[str(item) for item in local],
        markdown=str(report_path),
        systems=summary.metadata["systems"],
        caveats=len(summary.metadata["caveats"]),
    )
    return 0


def run_triage() -> int:
    """Stage: account for every residual finding with a deterministic rule.

    The A1 fallback and the baseline A1 has to beat. It reads gold's exemplars,
    classifies each with the first rule that fires, and writes the queue back to
    gold so the report and the page can read it like any other table.

    Nothing here calls a model. That is the point: an agent that cannot beat
    five arithmetic rules on a labelled set is not worth its per-call cost, and
    until this existed there was nothing to compare one against.
    """
    from pipeline import mart, triage
    from storage import resolve

    location = resolve()
    target = location.child(*mart.GOLD_ROOT, "exemplars")
    if not target.exists():
        log("triage_no_gold", detail="gold/exemplars is absent; run --stage mart first")
        return 1

    import pyarrow.dataset as ds

    rows = (
        ds.dataset(target.root, filesystem=target.filesystem, format="parquet", partitioning="hive")
        .to_table()
        .to_pylist()
    )
    if not rows:
        log("triage_no_findings", detail="gold/exemplars is empty")
        return 1

    ranked = triage.queue(rows)
    counted = triage.summarise(ranked)
    written = mart.write(location, {"triage_queue": ranked, "triage_summary": counted})

    log(
        "triage_written",
        **written,
        by_rule={row["triage_rule"]: row["findings"] for row in counted},
        unexplained=next(
            (row["findings"] for row in counted if row["triage_rule"] == "unexplained"), 0
        ),
    )
    return 0


def run(stage: str, *, dry_run: bool) -> int:
    started = time.monotonic()
    log("stage_start", stage=stage, dry_run=dry_run)
    code = 0
    if dry_run:
        log("stage_skipped", stage=stage, reason="dry run")
    elif stage == "manifest":
        code = run_manifest()
    elif stage == "mart":
        code = run_mart()
    elif stage == "report":
        code = run_report()
    elif stage == "triage":
        code = run_triage()
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
        # Baked into the image at build time. An execution that cannot say which
        # commit it is running cannot be told apart from one running a stale
        # image, and a stale image reports Succeeded while doing less.
        build_sha=os.environ.get("RECKONER_BUILD_SHA", "unknown"),
        # Which allocator pyarrow actually chose, so a variable that silently
        # did nothing is distinguishable from one that worked.
        arrow_allocator=arrow_memory()[2],
    )
    preflight(args.stage)
    code = run(args.stage, dry_run=args.dry_run)
    log("job_end", stage=args.stage, exit_code=code)
    return code


if __name__ == "__main__":
    sys.exit(main())
