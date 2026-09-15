"""The manifest stage's exit code, across both layers.

A job that reports ``Succeeded`` with a bad diff in its logs is worse than one
that fails. The execution status is what gets noticed first; logs are read only
once something already looks wrong. So the exit code carries the verdict.

The second thing held here is that one layer's failure does not suppress the
other's report. "Bronze drifted" and "bronze and silver both drifted" are
different situations, and stopping at the first would tell you the same thing
about each.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Never

import pyarrow.fs as pafs
import pytest

import reckoner_job
from pipeline import cap, manifest_check
from pipeline.cap import RESPECT_QUOTA, CapProbe
from pipeline.manifest_check import Layer, ManifestDiff, Observation
from storage import Location


def diff(
    layer: str, *, matches: bool, group_key: str = "carrier", group: str = "Aetna"
) -> ManifestDiff:
    want = Observation(group="*", files=1, rows=10, bytes=100)
    got = Observation(group="*", files=1, rows=10 if matches else 9, bytes=100)
    return ManifestDiff(
        layer=layer,
        ingest_date="2026-09-13",
        group_key=group_key,
        expected=want,
        observed=got,
        per_group=[{group_key: group, "matches": matches}],
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


def both(
    bronze_ok: bool, silver_ok: bool, payer_ok: bool = True
) -> Callable[[object, Layer], ManifestDiff]:
    """Stand in for compare(), answering per layer."""

    def fake(location: object, layer: Layer) -> ManifestDiff:
        if layer.name.startswith("bronze"):
            return diff(layer.name, matches=bronze_ok)
        if layer.name == "silver/payer_rates":
            return diff(layer.name, matches=payer_ok, group_key="carrier", group="Emblem")
        return diff(layer.name, matches=silver_ok, group_key="hospital_slug", group="crouse-health")

    return fake


class TestTheExitCode:
    def test_both_layers_matching_exits_zero(self, stubbed, monkeypatch):
        monkeypatch.setattr(manifest_check, "compare", both(True, True))

        assert reckoner_job.run_manifest() == 0

    def test_a_bronze_mismatch_exits_non_zero(self, stubbed, monkeypatch):
        monkeypatch.setattr(manifest_check, "compare", both(False, True))

        assert reckoner_job.run_manifest() == 1

    def test_a_silver_mismatch_exits_non_zero(self, stubbed, monkeypatch):
        """Silver drifting must fail the run exactly as bronze drifting does."""
        monkeypatch.setattr(manifest_check, "compare", both(True, False))

        assert reckoner_job.run_manifest() == 1

    def test_a_payer_silver_mismatch_exits_non_zero(self, stubbed, monkeypatch):
        """The third layer is checked on equal terms with the other two."""
        monkeypatch.setattr(manifest_check, "compare", both(True, True, False))

        assert reckoner_job.run_manifest() == 1

    def test_no_baseline_at_all_exits_non_zero(self, stubbed, monkeypatch):
        """Nothing to compare against is a broken check, not a passing one."""
        monkeypatch.setattr(manifest_check, "latest_ingest_date", lambda location: None)

        assert reckoner_job.run_manifest() == 1
        assert stubbed[0][0] == "manifest_no_baseline"

    def test_an_unreadable_layer_fails_rather_than_reporting_no_drift(self, stubbed, monkeypatch):
        """A check that could not run has not found the data clean."""

        def boom(location: object, layer: Layer) -> Never:
            raise FileNotFoundError("no manifest")

        monkeypatch.setattr(manifest_check, "compare", boom)

        assert reckoner_job.run_manifest() == 1
        assert [e for e, _ in stubbed].count("manifest_unreadable") == 3

    def test_the_stage_wrapper_propagates_it(self, stubbed, monkeypatch):
        monkeypatch.setattr(manifest_check, "compare", both(False, True))

        assert reckoner_job.run("manifest", dry_run=False) == 1
        stage_end = next(fields for event, fields in stubbed if event == "stage_end")
        assert stage_end["exit_code"] == 1


class TestWhatItEmits:
    def test_both_layers_are_reported_in_one_execution(self, stubbed, monkeypatch):
        monkeypatch.setattr(manifest_check, "compare", both(True, True))

        reckoner_job.run_manifest()

        layers = [f.get("layer") for _, f in stubbed if f.get("event") != "stage_end"]
        assert "bronze/payer_tic" in layers
        assert "silver/hospital_rates" in layers
        assert "silver/payer_rates" in layers

    def test_a_failing_bronze_does_not_silence_silver(self, stubbed, monkeypatch):
        """The whole point of not stopping at the first failure."""
        monkeypatch.setattr(manifest_check, "compare", both(False, True))

        reckoner_job.run_manifest()

        summaries = {f["layer"]: f["matches"] for e, f in stubbed if e == "manifest_summary"}
        assert summaries == {
            "bronze/payer_tic": False,
            "silver/hospital_rates": True,
            "silver/payer_rates": True,
        }

    def test_a_failure_names_the_layers_that_failed(self, stubbed, monkeypatch):
        monkeypatch.setattr(manifest_check, "compare", both(False, False, False))

        reckoner_job.run_manifest()

        failed = next(f for e, f in stubbed if e == "manifest_failed")
        assert failed["layers"] == [
            "bronze/payer_tic",
            "silver/hospital_rates",
            "silver/payer_rates",
        ]

    def test_the_run_states_which_layers_it_covered(self, stubbed, monkeypatch):
        """A two-layer run and a three-layer run both look like success.

        The difference is only visible if the run says so. Written after a
        manual execution reported Succeeded having checked two of three layers,
        because it was running an image built before the third existed.
        """
        monkeypatch.setattr(manifest_check, "compare", both(True, True))

        reckoner_job.run_manifest()

        stated = next(f for e, f in stubbed if e == "manifest_layers")
        assert stated["count"] == 3
        assert stated["layers"] == [
            "bronze/payer_tic",
            "silver/hospital_rates",
            "silver/payer_rates",
        ]

    def test_a_dry_run_does_no_work(self, stubbed, monkeypatch):
        def fail(*args: object) -> None:
            raise AssertionError("a dry run must not read the container")

        monkeypatch.setattr(manifest_check, "compare", fail)

        assert reckoner_job.run("manifest", dry_run=True) == 0
