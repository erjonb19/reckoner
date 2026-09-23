"""The monthly release files, and the scheduled run publishing what it built.

Two failures these hold down. A release whose recorded checksum does not match
its bytes would make the page refuse good data, or accept bad. And a scheduled
mart that does not run the report left the page showing last month's numbers as
current, which is silent failure #14.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import reckoner_job
import storage
from pipeline.report import build, release_files, release_manifest, write_local, write_release


def gold(root: Path) -> Path:
    def put(name: str, slug: str, table: pa.Table) -> None:
        target = root / "gold" / name / f"hospital_slug={slug}"
        target.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, target / "part-0.parquet")

    put(
        "coverage",
        "ms",
        pa.table({"system": ["Mount Sinai"], "pairs_formed": [2], "candidates": [4]}),
    )
    put(
        "rates",
        "ms",
        pa.table(
            {
                "system": ["Mount Sinai"] * 2,
                "facility": ["Mount Sinai Queens"] * 2,
                "carrier": ["Aetna"] * 2,
                "code": ["70450", "99213"],
                "hospital_rate": [1210.0, 95.0],
            }
        ),
    )
    for slug, text, rows in (("ms", "CT HEAD W/O", 40), ("wp", "CT HEAD WO CONTRAST", 90)):
        put(
            "codes",
            slug,
            pa.table(
                {"code_type": ["CPT"], "code": ["70450"], "description": [text], "rows": [rows]}
            ),
        )
    return root


class TestTheRelease:
    def test_the_recorded_checksum_is_the_checksum_of_the_bytes(self):
        tables = {"rates": pa.table({"x": [1, 2, 3]})}
        files = release_files(tables)

        manifest = release_manifest(files, tables, "data-2026-10-01")

        entry = manifest["files"]["rates.parquet"]
        assert entry["sha256"] == hashlib.sha256(files["rates.parquet"]).hexdigest()
        assert (entry["bytes"], entry["rows"]) == (len(files["rates.parquet"]), 3)

    def test_build_records_the_release_in_run_json(self, tmp_path):
        summary = build(storage.local(gold(tmp_path)))

        release = summary.metadata["release"]
        assert release["tag"].startswith("data-")
        assert set(release["files"]) == {"rates.parquet", "codes.parquet"}
        assert release["files"]["rates.parquet"]["rows"] == 2

    def test_codes_are_merged_to_the_most_common_description_anywhere(self, tmp_path):
        summary = build(storage.local(gold(tmp_path)))

        codes = pq.read_table(pa.BufferReader(summary.release["codes.parquet"])).to_pylist()
        assert codes == [
            {"code_type": "CPT", "code": "70450", "description": "CT HEAD WO CONTRAST"}
        ]

    def test_parquet_never_lands_in_the_committed_summary(self, tmp_path):
        summary = build(storage.local(gold(tmp_path)))

        committed = write_local(summary, tmp_path / "summary")
        released = write_release(summary, tmp_path / "release")

        assert not [p for p in committed if p.suffix == ".parquet"]
        assert {p.name for p in released} == {"rates.parquet", "codes.parquet"}
        assert (
            "rates"
            not in json.loads((tmp_path / "summary" / "run.json").read_text())["rows_per_table"]
        ), "rates is a release file, not a CSV table"


@pytest.fixture
def stages(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    ran: list[str] = []
    monkeypatch.setattr(reckoner_job, "log", lambda *a, **k: None)
    for name in ("run_mart", "run_triage", "run_report"):
        monkeypatch.setattr(
            reckoner_job, name, lambda name=name: ran.append(name.removeprefix("run_")) or 0
        )
    monkeypatch.delenv("RECKONER_SYSTEM", raising=False)
    return ran


class TestTheScheduledRunPublishes:
    def test_a_full_mart_chains_triage_then_report(self, stages):
        assert reckoner_job.run("mart", dry_run=False) == 0
        assert stages == ["mart", "triage", "report"]

    def test_a_single_system_run_does_not_publish(self, stages, monkeypatch):
        """A report after one system would mix fresh and stale systems."""
        monkeypatch.setenv("RECKONER_SYSTEM", "white-plains-hospital")

        reckoner_job.run("mart", dry_run=False)

        assert stages == ["mart"]

    def test_a_failed_mart_publishes_nothing(self, stages, monkeypatch):
        monkeypatch.setattr(reckoner_job, "run_mart", lambda: stages.append("mart") or 1)

        assert reckoner_job.run("mart", dry_run=False) == 1
        assert stages == ["mart"]

    def test_a_failed_triage_stops_before_the_report(self, stages, monkeypatch):
        monkeypatch.setattr(reckoner_job, "run_triage", lambda: stages.append("triage") or 1)

        assert reckoner_job.run("mart", dry_run=False) == 1
        assert stages == ["mart", "triage"]
