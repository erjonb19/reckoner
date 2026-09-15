"""Diffing a manifest against what is actually in the container.

A manifest records what a publish intended. Between that publish and this check,
a file can be deleted, republished, or truncated, and none of those announce
themselves -- they surface later as a number that is quietly wrong.

Two tests earn this module. ``test_row_drift_is_caught_when_bytes_match``:
checking sizes proves bytes arrived, only the Parquet footer proves data did.
And ``test_two_partitions_sharing_a_basename_are_told_apart``: silver names every
partition's first file ``part-0.parquet``, so identifying files by name would
collapse 73 partitions into one entry and hide a deleted partition entirely.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from pipeline.cap import OVER_QUOTA, RESPECT_QUOTA, CapProbe
from pipeline.manifest_check import (
    SILVER_HOSPITAL,
    bronze_payer,
    compare,
    latest_ingest_date,
    observe,
    read_manifest,
    telemetry,
)
from storage import local

DATE = "2026-09-13"
BRONZE = bronze_payer(DATE)


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


def silver(
    tmp_path: Path,
    partitions: dict[tuple[str, str], int],
    *,
    claim_rows: dict[tuple[str, str], int] | None = None,
    withhold: tuple[str, str] | None = None,
) -> Path:
    """A silver tree keyed slug/code_type/vintage, where every file is part-0."""
    root = tmp_path / "silver" / "hospital_rates"
    uploads = []
    for (slug, code_type), rows in partitions.items():
        relative = f"hospital_slug={slug}/code_type={code_type}/vintage=2026-04/part-0.parquet"
        size = write_parquet(root / relative, rows)
        if withhold == (slug, code_type):
            (root / relative).unlink()
        uploads.append(
            {
                "destination": f"silver/hospital_rates/{relative}",
                "hospital_slug": slug,
                "code_type": code_type,
                "vintage": "2026-04",
                "rows": (claim_rows or {}).get((slug, code_type), rows),
                "bytes": size,
            }
        )
    meta = tmp_path / "_meta" / "silver" / "hospital_rates"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "upload_manifest.json").write_text(
        json.dumps(
            {"group_key": "hospital_slug", "published_at": "2026-09-15", "uploads": uploads}
        ),
        encoding="utf-8",
    )
    return tmp_path


class TestAMatchingLake:
    def test_it_matches(self, tmp_path):
        root = lake(tmp_path, {"Aetna": 10, "UHC": 7})

        diff = compare(local(root), BRONZE)

        assert diff.matches
        assert diff.observed.rows == 17
        assert diff.observed.files == 2
        assert diff.missing == [] and diff.unexpected == []

    def test_every_carrier_gets_a_record_plus_one_summary(self, tmp_path):
        root = lake(tmp_path, {"Aetna": 10, "Cigna": 4, "UHC": 7})

        records = telemetry(compare(local(root), BRONZE), CapProbe(RESPECT_QUOTA))

        assert [r["event"] for r in records] == ["manifest_group"] * 3 + ["manifest_summary"]
        assert {r["group"] for r in records[:3]} == {"Aetna", "Cigna", "UHC"}
        assert {r["group_key"] for r in records} == {"carrier"}
        assert {r["layer"] for r in records} == {"bronze/payer_tic"}
        assert records[-1]["matches"] is True
        assert records[-1]["groups_mismatched"] == []


class TestDrift:
    def test_row_drift_is_caught_when_bytes_match(self, tmp_path):
        """The reason row counts come from the footer and not the blob listing.

        The manifest claims 999 rows for a file that holds 10. Its size is
        recorded correctly, so a check comparing only bytes would have passed.
        """
        root = lake(tmp_path, {"Aetna": 10}, claim_rows={"Aetna": 999})

        diff = compare(local(root), BRONZE)

        assert not diff.matches
        row = diff.per_group[0]
        assert row["expected_bytes"] == row["observed_bytes"], "the size check would have passed"
        assert (row["expected_rows"], row["observed_rows"]) == (999, 10)
        assert row["matches"] is False

    def test_a_deleted_file_is_named(self, tmp_path):
        root = lake(tmp_path, {"Aetna": 10, "UHC": 7}, withhold="UHC")

        diff = compare(local(root), BRONZE)

        assert not diff.matches
        assert diff.missing == ["carrier=UHC/uhc.parquet"]
        assert diff.observed.files == 1

    def test_an_unexpected_file_is_named(self, tmp_path):
        """Something published without going through the manifest."""
        root = lake(tmp_path, {"Aetna": 10}, extra_file="Rogue")

        diff = compare(local(root), BRONZE)

        assert not diff.matches
        assert diff.unexpected == ["carrier=Rogue/surprise.parquet"]

    def test_the_summary_names_which_carrier_drifted(self, tmp_path):
        root = lake(tmp_path, {"Aetna": 10, "UHC": 7}, claim_rows={"UHC": 1})

        summary = telemetry(compare(local(root), BRONZE), CapProbe(RESPECT_QUOTA))[-1]

        assert summary["matches"] is False
        assert summary["groups_mismatched"] == ["UHC"]


class TestSilver:
    def test_a_matching_silver_tree_matches(self, tmp_path):
        root = silver(tmp_path, {("crouse-health", "CPT"): 10, ("crouse-health", "MS-DRG"): 4})

        diff = compare(local(root), SILVER_HOSPITAL)

        assert diff.matches
        assert diff.group_key == "hospital_slug"
        assert diff.observed.rows == 14

    def test_two_partitions_sharing_a_basename_are_told_apart(self, tmp_path):
        """Every silver file is part-0.parquet; identity has to be the path.

        Keyed on basenames these two collapse to one entry, the counts still
        add up, and a deleted partition reports no drift at all.
        """
        root = silver(
            tmp_path,
            {("northwell-health", "CPT"): 10, ("northwell-health", "CDM"): 20},
            withhold=("northwell-health", "CDM"),
        )

        diff = compare(local(root), SILVER_HOSPITAL)

        assert not diff.matches
        assert diff.missing == [
            "hospital_slug=northwell-health/code_type=CDM/vintage=2026-04/part-0.parquet"
        ]

    def test_it_groups_by_hospital_not_carrier(self, tmp_path):
        root = silver(tmp_path, {("a-health", "CPT"): 3, ("b-health", "CPT"): 5})

        records = telemetry(compare(local(root), SILVER_HOSPITAL), CapProbe(RESPECT_QUOTA))

        assert [r["group"] for r in records[:-1]] == ["a-health", "b-health"]
        assert {r["group_key"] for r in records} == {"hospital_slug"}
        assert {r["layer"] for r in records} == {"silver/hospital_rates"}

    def test_one_hospitals_drift_does_not_implicate_another(self, tmp_path):
        root = silver(
            tmp_path,
            {("a-health", "CPT"): 3, ("b-health", "CPT"): 5},
            claim_rows={("b-health", "CPT"): 99},
        )

        summary = telemetry(compare(local(root), SILVER_HOSPITAL), CapProbe(RESPECT_QUOTA))[-1]

        assert summary["groups_mismatched"] == ["b-health"]


class TestTheCapSignal:
    def test_a_capped_day_is_flagged_on_the_summary(self, tmp_path):
        """Carried beside the result it might have truncated, not inferred later."""
        root = lake(tmp_path, {"Aetna": 10})

        summary = telemetry(compare(local(root), BRONZE), CapProbe(OVER_QUOTA))[-1]

        assert summary["log_ingestion_status"] == OVER_QUOTA
        assert summary["log_cap_hit"] is True

    def test_an_unknown_status_says_why(self, tmp_path):
        root = lake(tmp_path, {"Aetna": 10})

        summary = telemetry(compare(local(root), BRONZE), CapProbe(None, "http 403"))[-1]

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

    def test_the_silver_manifest_directory_is_not_read_as_a_date(self, tmp_path):
        """`_meta/silver/` sits beside the dated directories and is not one."""
        (tmp_path / "_meta" / "silver" / "hospital_rates").mkdir(parents=True)
        (tmp_path / "_meta" / "ingest_date=2026-09-13").mkdir(parents=True)

        assert latest_ingest_date(local(tmp_path)) == "2026-09-13"


class TestReadingTheManifest:
    def test_a_bom_does_not_break_it(self, tmp_path):
        """PowerShell has written these files, and it emits a BOM."""
        meta = tmp_path / "_meta" / f"ingest_date={DATE}"
        meta.mkdir(parents=True)
        (meta / "upload_manifest.json").write_bytes(
            b"\xef\xbb\xbf" + json.dumps({"uploads": []}).encode()
        )

        assert read_manifest(local(tmp_path), BRONZE) == {"uploads": []}

    def test_observation_counts_per_group(self, tmp_path):
        root = lake(tmp_path, {"Aetna": 10, "UHC": 7})

        total, detail = observe(local(root), BRONZE)

        assert total.rows == 17
        assert {c: o.rows for c, o in detail["per_group"].items()} == {"Aetna": 10, "UHC": 7}
