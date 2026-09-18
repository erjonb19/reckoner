"""Whether the mart may run after the drift check.

The mart builds gold from the layers stage 1 checks. Gold built from drifted
layers is full, plausible and wrong, and it is what the report, the page and any
human reading them will quote. So a failed check has to stop the run.

The tests that matter are the two in the middle. `missing` and `stale` are both
"we do not know", which is a different thing from "the layers are fine" and a
different thing again from "the layers drifted". Collapsing those three into a
boolean is how a guard starts lying.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import storage
from pipeline.gate import STATUS_PATH, check, read_status, write_status


def lake(tmp_path, layers=None, *, checked_at=None):
    location = storage.local(tmp_path)
    if layers is None:
        return location
    target = tmp_path / STATUS_PATH[0]
    target.mkdir(parents=True, exist_ok=True)
    (target / STATUS_PATH[1]).write_text(
        json.dumps(
            {
                "checked_at": checked_at or datetime.now(UTC).isoformat(timespec="seconds"),
                "layers": layers,
                "ok": all(layers.values()),
            }
        ),
        encoding="utf-8",
    )
    return location


class TestItStopsOnlyOnFailure:
    def test_all_layers_matching_passes(self, tmp_path):
        verdict = check(lake(tmp_path, {"gold": True, "silver/payer_rates": True}))

        assert verdict.state == "passed"
        assert verdict.may_run is True

    def test_a_drifted_layer_stops_the_run(self, tmp_path):
        verdict = check(lake(tmp_path, {"gold": True, "silver/payer_rates": False}))

        assert verdict.state == "failed"
        assert verdict.may_run is False

    def test_the_failure_names_the_layer(self, tmp_path):
        """ "Something drifted" sends someone to the logs; naming it does not."""
        verdict = check(lake(tmp_path, {"bronze/payer_tic": False}))

        assert "bronze/payer_tic" in verdict.detail

    def test_a_missing_verdict_does_not_stop_the_run(self, tmp_path):
        """A lake where stage 1 has never run must still be usable."""
        verdict = check(lake(tmp_path))

        assert verdict.state == "missing"
        assert verdict.may_run is True
        assert verdict.is_known is False

    def test_a_stale_verdict_does_not_stop_the_run_but_says_so(self, tmp_path):
        old = (datetime.now(UTC) - timedelta(days=9)).isoformat(timespec="seconds")
        verdict = check(lake(tmp_path, {"gold": True}, checked_at=old))

        assert verdict.state == "stale"
        assert verdict.may_run is True
        assert "9 days ago" in verdict.detail

    def test_a_verdict_inside_the_window_is_not_stale(self, tmp_path):
        recent = (datetime.now(UTC) - timedelta(hours=6)).isoformat(timespec="seconds")

        assert check(lake(tmp_path, {"gold": True}, checked_at=recent)).state == "passed"

    def test_unknown_is_distinguishable_from_passed(self, tmp_path):
        """The distinction the design rests on: three states, not two."""
        assert check(lake(tmp_path)).is_known is False
        assert check(lake(tmp_path, {"gold": True})).is_known is True
        assert check(lake(tmp_path, {"gold": False})).is_known is True

    def test_an_empty_layer_map_is_unknown_not_passed(self, tmp_path):
        """`all([])` is True, which would have made no layers look like success."""
        verdict = check(lake(tmp_path, {}))

        assert verdict.state == "missing"
        assert verdict.may_run is True

    def test_an_unreadable_timestamp_is_not_treated_as_fresh(self, tmp_path):
        verdict = check(lake(tmp_path, {"gold": True}, checked_at="not a date"))

        assert verdict.state == "stale"


class TestWriting:
    def test_the_verdict_round_trips(self, tmp_path):
        location = storage.local(tmp_path)

        write_status(location, {"gold": True, "bronze/payer_tic": True}, build_sha="abc123")

        stored = read_status(location)
        assert stored is not None
        assert stored["ok"] is True
        assert stored["build_sha"] == "abc123"
        assert stored["layers"]["gold"] is True
        assert check(location).state == "passed"

    def test_a_failure_round_trips_as_a_failure(self, tmp_path):
        location = storage.local(tmp_path)

        write_status(location, {"gold": False})

        assert read_status(location)["ok"] is False
        assert check(location).may_run is False

    def test_no_layers_is_not_recorded_as_ok(self, tmp_path):
        location = storage.local(tmp_path)

        write_status(location, {})

        assert read_status(location)["ok"] is False

    def test_reading_a_lake_without_a_verdict_is_none(self, tmp_path):
        assert read_status(storage.local(tmp_path)) is None
