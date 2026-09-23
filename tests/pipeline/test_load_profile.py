"""The OOM profile mode: one shard's hospital load, measured, nothing written.

It runs in the cloud against the real lake, so what is tested here is the
contract around it -- that an unknown variant is refused rather than run as
the baseline, that each variant reaches ``hospital_shard`` with the settings
its name promises, and that every step is logged.
"""

from __future__ import annotations

from typing import Any

import pytest

import reckoner_job


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"events": [], "calls": []}
    monkeypatch.setattr(
        reckoner_job, "log", lambda event, **fields: state["events"].append((event, fields))
    )
    monkeypatch.setattr("storage.resolve", lambda *a, **k: object())
    monkeypatch.setattr("reconcile.silver.open_hospital_silver", lambda lake: "dataset")

    def fake_shard(
        dataset: object,
        hospital: str,
        code_types: object,
        shard: str,
        **kw: Any,  # noqa: ANN401 - forwarded as-is
    ) -> list[str]:
        state["calls"].append({"hospital": hospital, "shard": shard, **kw})
        for step in ("start", "scanned", "aggregated", "scan_released", "converted"):
            kw["on_step"](step)
        return ["rate"] * 3

    monkeypatch.setattr("reconcile.silver.hospital_shard", fake_shard)
    monkeypatch.setenv("RECKONER_SYSTEM", "nyu-langone-health")
    monkeypatch.setenv("RECKONER_SHARD", "1")
    return state


class TestVariants:
    def test_an_unknown_variant_is_refused_not_run_as_the_baseline(self, captured, monkeypatch):
        monkeypatch.setenv("RECKONER_PROFILE_LOAD", "prunned")

        assert reckoner_job.run_load_profile() == 1
        assert captured["calls"] == []
        assert captured["events"][0][0] == "load_profile_refused"

    @pytest.mark.parametrize(
        ("variant", "slug", "readahead"),
        [
            ("baseline", None, True),
            ("no-readahead", None, False),
            ("pruned", "nyu-langone-health", True),
            ("pruned-no-readahead", "nyu-langone-health", False),
        ],
    )
    def test_each_variant_passes_the_settings_its_name_promises(
        self, captured, monkeypatch, variant, slug, readahead
    ):
        monkeypatch.setenv("RECKONER_PROFILE_LOAD", variant)

        assert reckoner_job.run_load_profile() == 0

        call = captured["calls"][0]
        assert (call["slug"], call["readahead"]) == (slug, readahead)
        assert call["hospital"] == "NYU Langone Health"
        assert call["shard"] == "1"

    def test_it_needs_exactly_one_system(self, captured, monkeypatch):
        monkeypatch.setenv("RECKONER_PROFILE_LOAD", "baseline")
        monkeypatch.setenv("RECKONER_SYSTEM", "")

        assert reckoner_job.run_load_profile() == 1
        assert captured["calls"] == []


class TestWhatItLogs:
    def test_every_step_is_logged_with_memory_fields(self, captured, monkeypatch):
        monkeypatch.setenv("RECKONER_PROFILE_LOAD", "pruned")

        reckoner_job.run_load_profile()

        steps = [f["step"] for e, f in captured["events"] if e == "load_profile"]
        assert steps == [
            "process_start",
            "dataset_open",
            "start",
            "scanned",
            "aggregated",
            "scan_released",
            "converted",
            "done",
        ]
        first = next(f for e, f in captured["events"] if e == "load_profile")
        assert {"rss_mib", "hwm_mib", "arrow_live_mib", "arrow_max_mib", "variant"} <= set(first)

    def test_the_mart_stage_hands_over_before_touching_gold(self, captured, monkeypatch):
        """Profile mode must not run the gate, the mart, or a write."""
        monkeypatch.setenv("RECKONER_PROFILE_LOAD", "baseline")
        monkeypatch.setattr(
            "pipeline.gate.check", lambda *a, **k: pytest.fail("the gate ran in profile mode")
        )

        assert reckoner_job.run_mart() == 0
