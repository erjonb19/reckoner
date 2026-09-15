"""Diffing the manifest against what is actually in the container.

The manifest records what a publish intended. Between that publish and this
check, a file can be deleted, republished, or truncated, and none of those
announce themselves -- they surface later as a number that is quietly wrong.

The test that earns this module is ``test_row_drift_is_caught_when_bytes_match``.
Checking sizes proves bytes arrived; only the Parquet footer proves data did.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from pipeline.cap import OVER_QUOTA, RESPECT_QUOTA, CapProbe
from pipeline.manifest_check import (
    compare,
    latest_ingest_date,
    observe,
    read_manifest,
    telemetry,
)
from storage import local

DATE = "2026-09-13"


def write_parquet(path: Path, rows: int) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"rate": pa.array(range(rows), pa.int64())}), path)
    return path.stat().st_size


def lake(
    tmp_path: Path,
    carriers: dict[str, int],
    *,
    claim_rows: dict[str, int] | None = None,
    extra_file: str | None = None,
    withhold: str | None = None,
) -> Path:
    """A bronze tree plus the manifest that claims to describe it."""
    bronze = tmp_path / "bronze" / "payer_tic" / f"ingest_date={DATE}"
    uploads = []
    for carrier, rows in carriers.items():
        name = f"{carrier.lower()}.parquet"
        destination = f"bronze/payer_tic/ingest_date={DATE}/carrier={carrier}/{name}"
        size = write_parquet(bronze / f"carrier={carrier}" / name, rows)
        if withhold == carrier:
            (bronze / f"carrier={carrier}" / name).unlink()
        uploads.append(
            {
                "carrier": carrier,
                "destination": destination,
                "rows": (claim_rows or {}).get(carrier, rows),
                "bytes": size,
            }
        )
    if extra_file:
        write_parquet(bronze / f"carrier={extra_file}" / "surprise.parquet", 3)

    meta = tmp_path / "_meta" / f"ingest_date={DATE}"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "upload_manifest.json").write_text(json.dumps({"uploads": uploads}), encoding="utf-8")
    return tmp_path


class TestAMatchingLake:
    def test_it_matches(self, tmp_path):
        root = lake(tmp_path, {"Aetna": 10, "UHC": 7})

        diff = compare(local(root), DATE)

        assert diff.matches
        assert diff.observed.rows == 17
        assert diff.observed.files == 2
        assert diff.missing == [] and diff.unexpected == []

    def test_every_carrier_gets_a_record_plus_one_summary(self, tmp_path):
        root = lake(tmp_path, {"Aetna": 10, "Cigna": 4, "UHC": 7})

        records = telemetry(compare(local(root), DATE), CapProbe(RESPECT_QUOTA))

        assert [r["event"] for r in records] == ["manifest_carrier"] * 3 + ["manifest_summary"]
        assert {r["carrier"] for r in records[:3]} == {"Aetna", "Cigna", "UHC"}
        assert records[-1]["matches"] is True
        assert records[-1]["carriers_mismatched"] == []


class TestDrift:
    def test_row_drift_is_caught_when_bytes_match(self, tmp_path):
        """The reason row counts come from the footer and not the blob listing.

        The manifest claims 999 rows for a file that holds 10. Its size is
        recorded correctly, so a check comparing only bytes would have passed.
        """
        root = lake(tmp_path, {"Aetna": 10}, claim_rows={"Aetna": 999})

        diff = compare(local(root), DATE)

        assert not diff.matches
        row = diff.per_carrier[0]
        assert row["expected_bytes"] == row["observed_bytes"], "the size check would have passed"
        assert (row["expected_rows"], row["observed_rows"]) == (999, 10)
        assert row["matches"] is False

    def test_a_deleted_file_is_named(self, tmp_path):
        root = lake(tmp_path, {"Aetna": 10, "UHC": 7}, withhold="UHC")

        diff = compare(local(root), DATE)

        assert not diff.matches
        assert diff.missing == ["uhc.parquet"]
        assert diff.observed.files == 1

    def test_an_unexpected_file_is_named(self, tmp_path):
        """Something published without going through the manifest."""
        root = lake(tmp_path, {"Aetna": 10}, extra_file="Rogue")

        diff = compare(local(root), DATE)

        assert not diff.matches
        assert diff.unexpected == ["surprise.parquet"]

    def test_the_summary_names_which_carrier_drifted(self, tmp_path):
        root = lake(tmp_path, {"Aetna": 10, "UHC": 7}, claim_rows={"UHC": 1})

        summary = telemetry(compare(local(root), DATE), CapProbe(RESPECT_QUOTA))[-1]

        assert summary["matches"] is False
        assert summary["carriers_mismatched"] == ["UHC"]


class TestTheCapSignal:
    def test_a_capped_day_is_flagged_on_the_summary(self, tmp_path):
        """Carried beside the result it might have truncated, not inferred later."""
        root = lake(tmp_path, {"Aetna": 10})

        summary = telemetry(compare(local(root), DATE), CapProbe(OVER_QUOTA))[-1]

        assert summary["log_ingestion_status"] == OVER_QUOTA
        assert summary["log_cap_hit"] is True

    def test_an_unknown_status_says_why(self, tmp_path):
        root = lake(tmp_path, {"Aetna": 10})

        summary = telemetry(compare(local(root), DATE), CapProbe(None, "http 403"))[-1]

        assert summary["log_ingestion_status"] == "unknown"
        assert summary["log_cap_hit"] is False
        assert summary["log_probe_detail"] == "http 403"


class TestFindingTheBaseline:
    def test_the_newest_ingest_date_wins(self, tmp_path):
        for date in ("2026-08-01", "2026-09-13", "2026-07-30"):
            (tmp_path / "_meta" / f"ingest_date={date}").mkdir(parents=True)

        assert latest_ingest_date(local(tmp_path)) == "2026-09-13"

    def test_no_meta_at_all_is_none_not_a_crash(self, tmp_path):
        assert latest_ingest_date(local(tmp_path)) is None


class TestReadingTheManifest:
    def test_a_bom_does_not_break_it(self, tmp_path):
        """PowerShell has written these files, and it emits a BOM."""
        meta = tmp_path / "_meta" / f"ingest_date={DATE}"
        meta.mkdir(parents=True)
        (meta / "upload_manifest.json").write_bytes(
            b"\xef\xbb\xbf" + json.dumps({"uploads": []}).encode()
        )

        assert read_manifest(local(tmp_path), DATE) == {"uploads": []}

    def test_observation_counts_per_carrier(self, tmp_path):
        root = lake(tmp_path, {"Aetna": 10, "UHC": 7})

        total, detail = observe(local(root), DATE)

        assert total.rows == 17
        assert {c: o.rows for c, o in detail["per_carrier"].items()} == {"Aetna": 10, "UHC": 7}
