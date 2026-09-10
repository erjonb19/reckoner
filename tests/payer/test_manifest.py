"""Manifest tests.

A manifest's whole job is to notice that the payer directory moved, so the tests
that matter are the ones where something changes and the diff has to name it.
The base file is ``Emblem_HIPHOSH00687``, twenty rows copied verbatim from real
``mrf_pipeline`` output.

The limitation is tested too: a file that parsed and matched nothing leaves no
Parquet, and nothing at this boundary can tell that apart from never having been
parsed. The manifest is not allowed to invent a state for it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from payer.contract import CONTRACT_VERSION
from payer.manifest import (
    MANIFEST_VERSION,
    FileState,
    Manifest,
    build,
    diff,
)

REAL = Path(__file__).parent.parent / "fixtures" / "payer_parquet" / "Emblem_HIPHOSH00687.parquet"


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    """A payer directory holding one real file."""
    table = pq.read_table(REAL)
    pq.write_table(table, tmp_path / "Emblem_HIPHOSH00687.parquet")
    return tmp_path


class TestSnapshot:
    def test_a_real_file_is_described_from_its_footer(self, lake):
        entry = build(lake).by_stem()["Emblem_HIPHOSH00687"]

        assert entry.state is FileState.READ
        assert entry.rows == 20
        assert entry.vintage == "2026-09-04"
        assert entry.carrier == "Emblem"
        assert entry.network == "HIPHOSH00687"
        assert entry.columns == 18
        assert entry.bytes > 0
        assert entry.sha256 is None, "hashing must be opt-in"

    def test_a_duplicate_is_recorded_but_not_counted_as_read(self, lake):
        """Cigna publishes two files that are row-for-row identical."""
        pq.write_table(pq.read_table(REAL), lake / "Cigna_PathwellOAP.parquet")

        manifest = build(lake)

        assert manifest.by_stem()["Cigna_PathwellOAP"].state is FileState.DUPLICATE
        assert len(manifest.read) == 1
        assert manifest.rows == 20

    def test_an_in_flight_part_is_listed_without_being_opened(self, lake):
        """A .part is an open writer handle; reading it raises."""
        (lake / "Aetna_Whatever.parquet.part").write_bytes(b"not parquet yet")

        entry = build(lake).by_stem()["Aetna_Whatever"]

        assert entry.state is FileState.IN_FLIGHT
        assert entry.rows == 0

    def test_an_unreadable_file_is_recorded_not_raised(self, lake):
        (lake / "Emblem_Broken.parquet").write_bytes(b"garbage")

        entry = build(lake).by_stem()["Emblem_Broken"]

        assert entry.state is FileState.UNREADABLE

    def test_superseded_subdirectories_are_not_part_of_the_dataset(self, lake):
        old = lake / "old_npi_only"
        old.mkdir()
        pq.write_table(pq.read_table(REAL), old / "Emblem_HIPHOSH00687.parquet")

        assert len(build(lake).entries) == 1

    def test_a_missing_root_raises_rather_than_returning_an_empty_snapshot(self, tmp_path):
        """An empty manifest would read as "the boundary is empty", which is a lie."""
        with pytest.raises(FileNotFoundError):
            build(tmp_path / "nope")

    def test_hashing_is_opt_in_and_stable(self, lake):
        first = build(lake, with_hash=True).by_stem()["Emblem_HIPHOSH00687"]
        second = build(lake, with_hash=True).by_stem()["Emblem_HIPHOSH00687"]

        assert first.sha256 and first.sha256 == second.sha256


class TestRoundTrip:
    def test_a_snapshot_survives_json(self, lake, tmp_path):
        original = build(lake)
        path = tmp_path / "out" / "manifest.json"
        original.write(path)

        restored = Manifest.read_json(path)

        assert restored.entries == original.entries
        assert restored.contract_version == CONTRACT_VERSION
        assert diff(original, restored).unchanged

    def test_a_snapshot_from_a_different_version_refuses_to_compare(self, lake, tmp_path):
        """Comparing across rule changes would report differences that are not there."""
        path = tmp_path / "old.json"
        raw = json.loads(build(lake).to_json())
        raw["manifest_version"] = MANIFEST_VERSION + 1
        path.write_text(json.dumps(raw), encoding="utf-8")

        with pytest.raises(ValueError, match="manifest version"):
            Manifest.read_json(path)


class TestDiff:
    def test_an_unchanged_directory_diffs_clean(self, lake):
        assert diff(build(lake), build(lake)).unchanged

    def test_a_new_file_is_named(self, lake):
        before = build(lake)
        pq.write_table(pq.read_table(REAL), lake / "Emblem_NEW0001.parquet")

        changes = diff(before, build(lake))

        assert changes.added == ["Emblem_NEW0001"]
        assert not changes.unchanged

    def test_a_vanished_file_is_named(self, lake):
        """The case the manifest exists for: absence is otherwise silent."""
        before = build(lake)
        (lake / "Emblem_HIPHOSH00687.parquet").unlink()

        changes = diff(before, build(lake))

        assert changes.removed == ["Emblem_HIPHOSH00687"]

    def test_a_re_parse_at_a_new_vintage_is_named(self, lake):
        """The change most likely to go unnoticed: same file, newer data."""
        before = build(lake)
        table = pq.read_table(REAL)
        index = table.schema.get_field_index("last_updated_on")
        moved = table.set_column(
            index, "last_updated_on", pa.array(["2026-10-01"] * table.num_rows, type=pa.string())
        )
        pq.write_table(moved, lake / "Emblem_HIPHOSH00687.parquet")

        changes = diff(before, build(lake))

        assert "Emblem_HIPHOSH00687" in changes.changed
        assert any("vintage" in f for f in changes.changed["Emblem_HIPHOSH00687"])

    def test_a_row_count_change_is_named(self, lake):
        before = build(lake)
        pq.write_table(pq.read_table(REAL).slice(0, 5), lake / "Emblem_HIPHOSH00687.parquet")

        changes = diff(before, build(lake))

        assert any("rows: 20 -> 5" in f for f in changes.changed["Emblem_HIPHOSH00687"])

    def test_a_hash_on_only_one_side_is_not_a_change(self, lake):
        """A cheap snapshot compared against an expensive one is still comparable."""
        cheap = build(lake)
        hashed = build(lake, with_hash=True)

        assert diff(cheap, hashed).unchanged


class TestWhatItRefusesToClaim:
    def test_absence_is_never_reported_as_a_state(self, lake):
        """A file that parsed and matched nothing looks exactly like one never parsed.

        182 of ~280 Emblem files are in that state, and the fact that distinguishes
        them lives upstream. Every state the manifest defines describes something
        it can actually see on disk.
        """
        states = {e.state for e in build(lake).entries}

        assert states <= set(FileState)
        assert not any("absent" in str(s) or "expected" in str(s) for s in FileState)

    def test_a_never_seen_file_is_indistinguishable_from_a_vanished_one(self, lake):
        """Stated as a test so the limitation is not quietly forgotten.

        Both show up as "not in this snapshot". Only a previous snapshot tells
        them apart, which is exactly why one is worth keeping.
        """
        manifest = build(lake)

        assert "Emblem_NEVER_EXISTED" not in manifest.by_stem()
        assert "Emblem_PARSED_BUT_EMPTY" not in manifest.by_stem()
