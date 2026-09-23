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
from pathlib import Path
from typing import Any

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


#: The load profiler's variants: which filter and scanner options
#: ``hospital_shard`` runs with. One per execution, because the process
#: high-water never falls and so cannot be compared within one process.
LOAD_VARIANTS: dict[str, dict[str, bool]] = {
    "baseline": {"pruned": False, "readahead": True},
    "no-readahead": {"pruned": False, "readahead": False},
    "pruned": {"pruned": True, "readahead": True},
    "pruned-no-readahead": {"pruned": True, "readahead": False},
}


def proc_status_mib(field: str) -> int | None:
    """One ``/proc/self/status`` memory field in MiB: VmRSS now, VmHWM ever."""
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{field}:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def run_load_profile() -> int:
    """Profile one shard's hospital-side load, and write nothing.

    Set ``RECKONER_PROFILE_LOAD`` to a variant from :data:`LOAD_VARIANTS`, with
    ``RECKONER_SYSTEM`` and ``RECKONER_SHARD``. Each step of the load logs the
    current RSS, the process high-water and Arrow's pool, so the step where
    memory appears -- and whether Arrow can see it -- is in the logs by name.

    Exists because the memory in question is invisible from outside. NYU
    Langone's shard 1 peaked near 8 GB with Arrow's pool at 839 MiB, and a
    watcher sampling the whole slice cannot say which call in it was expensive.
    """
    import pyarrow as pa

    from pipeline import mart
    from reconcile.silver import hospital_shard, open_hospital_silver
    from storage import resolve

    variant = os.environ.get("RECKONER_PROFILE_LOAD", "").strip()
    settings = LOAD_VARIANTS.get(variant)
    if settings is None:
        log("load_profile_refused", variant=variant, known=sorted(LOAD_VARIANTS))
        return 1
    chosen = mart.select(os.environ.get("RECKONER_SYSTEM"))
    if len(chosen) != 1:
        log("load_profile_refused", detail="set RECKONER_SYSTEM to exactly one system")
        return 1
    spec = chosen[0]
    shard = os.environ.get("RECKONER_SHARD", "1").strip()
    pool = pa.default_memory_pool()
    started = time.monotonic()

    def step(name: str, **fields: object) -> None:
        log(
            "load_profile",
            variant=variant,
            system=spec.slug,
            shard=shard,
            step=name,
            seconds=round(time.monotonic() - started, 1),
            rss_mib=proc_status_mib("VmRSS"),
            hwm_mib=proc_status_mib("VmHWM"),
            arrow_live_mib=round(pool.bytes_allocated() / 2**20, 1),
            arrow_max_mib=round(pool.max_memory() / 2**20, 1),
            arrow_backend=pool.backend_name,
            **fields,
        )

    step("process_start")
    dataset = open_hospital_silver(resolve())
    step("dataset_open")
    rates = hospital_shard(
        dataset,
        spec.hospital,
        mart.SHARED_CODE_TYPES,
        shard,
        slug=spec.slug if settings["pruned"] else None,
        readahead=settings["readahead"],
        on_step=step,
    )
    step("done", rates=len(rates))
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

    if os.environ.get("RECKONER_PROFILE_LOAD", "").strip():
        return run_load_profile()

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
    written = mart.write(location, built, systems={run.hospital_slug for run in runs})
    log("mart_written", systems=[run.hospital_slug for run in runs], **written)

    # Same manifest-and-verify shape as the two silver layers, so one stage-1
    # check covers all four without a special case.
    # The manifest describes the whole gold tree, because that is what stage 1
    # checks, but only the partitions this execution wrote were intended by it.
    # Naming them is the difference between a manifest that says "gold is
    # correct" and one that says "gold is correct and I am the reason for this
    # part of it" -- and after a per-system run, only the second is true.
    manifest = mart.gold_manifest(location, written, {run.hospital_slug for run in runs})
    log("memwatch_peak", sampled_peak_rss_mib=watch.stop(), samples=watch.samples)
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

    # Provenance a reader cannot recover from the data. This once said NYU
    # Langone's gold came from a local run, and it stayed true in run.json for
    # a day after it stopped being true: a caveat is a claim, and a hard-coded
    # one does not notice when the world changes. Every system's gold now comes
    # from reckoner-mart, and there is no caveat to add.
    caveats: tuple[str, ...] = ()
    summary = report.build(
        location, build_sha=os.environ.get("RECKONER_BUILD_SHA", ""), caveats=caveats
    )
    if not summary.tables.get("coverage"):
        log("report_no_gold", detail="gold/coverage is empty; run --stage mart first")
        return 1

    remote = report.write_remote(summary, location)
    local = report.write_local(summary, Path("summary"))
    # The release files go beside the repository's data, never into summary/,
    # which is committed. The summary workflow uploads them as a GitHub Release.
    release = report.write_release(summary, Path("release"))
    report_path = Path(*report.REPORT_PATH)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report.markdown(summary), encoding="utf-8")

    log(
        "report_written",
        rows=summary.rows(),
        remote=len(remote),
        local=[str(item) for item in local],
        release=summary.metadata.get("release", {}),
        release_local=[str(item) for item in release],
        markdown=str(report_path),
        systems=summary.metadata["systems"],
        caveats=len(summary.metadata["caveats"]),
    )
    return 0


def _sum_by(rows: list[dict[str, Any]], key: str, value: str) -> dict[str, int]:
    totals: dict[str, int] = {}
    for row in rows:
        totals[str(row[key])] = totals.get(str(row[key]), 0) + int(row[value])
    return totals


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
    counted = triage.summarise_by_system(ranked)
    # Every system gold covers, not only those with findings: a system whose
    # residual is now empty must lose its old queue, not keep it.
    covered = location.child(*mart.GOLD_ROOT, "coverage")
    systems = {
        str(row.get("hospital_slug"))
        for row in ds.dataset(
            covered.root, filesystem=covered.filesystem, format="parquet", partitioning="hive"
        )
        .to_table(columns=["hospital_slug"])
        .to_pylist()
    }
    written = mart.write(
        location, {"triage_queue": ranked, "triage_summary": counted}, systems=systems
    )
    # Triage writes into gold, so it rewrites gold's manifest -- otherwise the
    # next stage-1 check finds files no manifest describes and fails on them.
    from storage import publish

    manifest = mart.gold_manifest(location, written, systems)
    publish.write_manifest(location, manifest, path=mart.GOLD_MANIFEST)
    log(
        "triage_manifest",
        verified=manifest["verified"],
        rows_checked=manifest["rows_checked"],
        systems_written=manifest["systems_written"],
    )
    if not manifest["verified"]:
        return 1

    log(
        "triage_written",
        **written,
        # Summed across systems: the summary is per system now, and a dict built
        # from its rows kept only the last system's count for each rule.
        by_rule=_sum_by(counted, "triage_rule", "findings"),
        unexplained=_sum_by(counted, "triage_rule", "findings").get("unexplained", 0),
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
        # The scheduled run publishes what it built. Without this, nothing
        # scheduled wrote lake/summary: the mart rebuilt gold on the 1st, the
        # summary workflow copied a stale lake/summary on the 2nd, and the page
        # showed last month's numbers as current (silent failure #14). A
        # single-system run does not chain, because a report after one system
        # would publish a mix of fresh and stale systems as one dataset.
        if code == 0 and not os.environ.get("RECKONER_SYSTEM", "").strip():
            for then in ("triage", "report"):
                log("stage_chained", after="mart", stage=then)
                code = run_triage() if then == "triage" else run_report()
                if code != 0:
                    break
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
