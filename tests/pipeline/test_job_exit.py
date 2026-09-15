"""The manifest stage's exit code.

A job that reports ``Succeeded`` with a bad diff in its logs is worse than one
that fails. The execution status is what gets noticed first; logs are read only
once something already looks wrong. So the exit code carries the verdict, and
these tests hold it to that.
"""

from __future__ import annotations

import pyarrow.fs as pafs
import pytest

import reckoner_job
from pipeline import cap, manifest_check
from pipeline.cap import RESPECT_QUOTA, CapProbe
from pipeline.manifest_check import CarrierObservation, ManifestDiff
from storage import Location


def diff(*, matches: bool) -> ManifestDiff:
    want = CarrierObservation(carrier="*", files=1, rows=10, bytes=100)
    got = CarrierObservation(carrier="*", files=1, rows=10 if matches else 9, bytes=100)
    return ManifestDiff(
        ingest_date="2026-09-13",
        expected=want,
        observed=got,
        per_carrier=[{"carrier": "Aetna", "matches": matches}],
    )


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, object]]]:
    """Run the stage without touching a network, capturing what it logs."""
    emitted: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        reckoner_job, "log", lambda event, **fields: emitted.append((event, fields))
    )
    monkeypatch.setattr(
        "storage.resolve", lambda *a, **k: Location(root="lake", filesystem=pafs.LocalFileSystem())
    )
    monkeypatch.setattr(manifest_check, "latest_ingest_date", lambda location: "2026-09-13")
    monkeypatch.setattr(cap, "ingestion_status", lambda *a, **k: CapProbe(RESPECT_QUOTA))
    return emitted


class TestTheExitCode:
    def test_a_matching_lake_exits_zero(self, stubbed, monkeypatch):
        monkeypatch.setattr(manifest_check, "compare", lambda *a: diff(matches=True))

        assert reckoner_job.run_manifest() == 0

    def test_a_mismatch_exits_non_zero(self, stubbed, monkeypatch):
        """The whole point: the execution shows Failed, not Succeeded."""
        monkeypatch.setattr(manifest_check, "compare", lambda *a: diff(matches=False))

        assert reckoner_job.run_manifest() == 1

    def test_no_baseline_at_all_exits_non_zero(self, stubbed, monkeypatch):
        """Nothing to compare against is a broken check, not a passing one."""
        monkeypatch.setattr(manifest_check, "latest_ingest_date", lambda location: None)

        assert reckoner_job.run_manifest() == 1
        assert stubbed[0][0] == "manifest_no_baseline"

    def test_the_stage_wrapper_propagates_it(self, stubbed, monkeypatch):
        monkeypatch.setattr(manifest_check, "compare", lambda *a: diff(matches=False))

        assert reckoner_job.run("manifest", dry_run=False) == 1
        stage_end = next(fields for event, fields in stubbed if event == "stage_end")
        assert stage_end["exit_code"] == 1


class TestWhatItEmits:
    def test_a_mismatch_still_reports_every_carrier(self, stubbed, monkeypatch):
        """A failing run must say which carrier drifted, not merely that one did."""
        monkeypatch.setattr(manifest_check, "compare", lambda *a: diff(matches=False))

        reckoner_job.run_manifest()

        events = [event for event, _ in stubbed]
        assert events == ["manifest_carrier", "manifest_summary"]
        assert dict(stubbed)["manifest_carrier"]["carrier"] == "Aetna"

    def test_a_dry_run_does_no_work(self, stubbed, monkeypatch):
        def fail(*args: object) -> None:
            raise AssertionError("a dry run must not read the container")

        monkeypatch.setattr(manifest_check, "compare", fail)

        assert reckoner_job.run("manifest", dry_run=True) == 0
