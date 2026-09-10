"""The contract as a load-time gate.

Building the contract (#20) and running it are different things: until the
loader consults it, drift is found only when somebody remembers to look. This is
the loader consulting it.

Two properties decide whether the gate is any good.

*Affordable.* It defaults to the footer-only tier, 0.03s across 120 files
against roughly two minutes to read every column. A gate nobody can afford to
leave on is not a gate.

*Proportionate.* It quarantines on what breaks the read and no more. The
contract knows eighteen columns; the reader projects ten. Dropping a perfectly
readable file because it lacks a column nobody reads would cost real data to
enforce a preference -- and did, briefly: turning the gate on quarantined every
trimmed fixture in this suite until missing-but-unused was demoted to a warning.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from payer.contract import COLUMNS, ContractCheck
from payer.curated import NEEDED_COLUMNS, discover_payer_files, file_summary

REAL = Path(__file__).parent.parent / "fixtures" / "payer_parquet" / "Emblem_HIPHOSH00687.parquet"


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    pq.write_table(pq.read_table(REAL), tmp_path / "Emblem_HIPHOSH00687.parquet")
    return tmp_path


def drop(lake: Path, column: str, stem: str = "Emblem_Broken") -> None:
    pq.write_table(pq.read_table(REAL).drop_columns([column]), lake / f"{stem}.parquet")


class TestTheGateIsProportionate:
    def test_the_contract_and_the_reader_agree_on_what_is_required(self):
        """If these drift apart the gate is either over-strict or asleep."""
        required = {c.name for c in COLUMNS if c.required_for_read}

        assert required == set(NEEDED_COLUMNS)

    def test_a_file_missing_a_column_the_reader_projects_is_quarantined(self, lake):
        """Without the gate this file reaches to_table and raises there instead."""
        drop(lake, "billing_class")

        stems = {f.stem for f in discover_payer_files(lake)}

        assert "Emblem_Broken" not in stems
        assert "Emblem_HIPHOSH00687" in stems, "a good file must survive alongside a bad one"

    def test_a_file_missing_a_column_nobody_reads_is_kept(self, lake):
        """It is readable. Dropping it would cost data to enforce a preference."""
        drop(lake, "description")

        stems = {f.stem for f in discover_payer_files(lake)}

        assert "Emblem_Broken" in stems

    def test_a_wrong_type_is_quarantined(self, lake):
        table = pq.read_table(REAL)
        index = table.schema.get_field_index("negotiated_rate")
        wrong = table.set_column(
            index, "negotiated_rate", pa.array(["x"] * table.num_rows, type=pa.string())
        )
        pq.write_table(wrong, lake / "Emblem_Typed.parquet")

        assert "Emblem_Typed" not in {f.stem for f in discover_payer_files(lake)}

    def test_an_unreadable_file_is_quarantined_rather_than_raising(self, lake):
        (lake / "Emblem_Garbage.parquet").write_bytes(b"not parquet")

        stems = {f.stem for f in discover_payer_files(lake)}

        assert "Emblem_Garbage" not in stems
        assert "Emblem_HIPHOSH00687" in stems


class TestQuarantineIsVisible:
    def test_a_quarantined_file_is_reported_with_its_reason(self, lake):
        """Architecture rule 4: quarantine with a reason, never a silent drop."""
        drop(lake, "systems")

        rows = {r["stem"]: r for r in file_summary(lake)}

        assert rows["Emblem_Broken"]["read"] is False
        assert "contract" in rows["Emblem_Broken"]["skipped_reason"]
        assert rows["Emblem_Broken"]["contract_errors"]
        assert "missing_column" in rows["Emblem_Broken"]["contract_errors"][0]

    def test_a_healthy_file_carries_no_contract_errors(self, lake):
        rows = {r["stem"]: r for r in file_summary(lake)}

        assert rows["Emblem_HIPHOSH00687"]["read"] is True
        assert rows["Emblem_HIPHOSH00687"]["contract_errors"] == []

    def test_quarantined_files_can_be_asked_for_deliberately(self, lake):
        drop(lake, "payer")

        included = {f.stem for f in discover_payer_files(lake, include_quarantined=True)}

        assert "Emblem_Broken" in included
        assert next(
            f
            for f in discover_payer_files(lake, include_quarantined=True)
            if f.stem == "Emblem_Broken"
        ).is_quarantined


class TestDepth:
    def test_the_gate_can_be_turned_off(self, lake):
        drop(lake, "billing_class")

        stems = {f.stem for f in discover_payer_files(lake, validate=ContractCheck.NONE)}

        assert "Emblem_Broken" in stems

    def test_the_default_tier_reads_no_data_so_a_row_level_fault_passes(self, lake):
        """The cheap tier is a schema check, and this pins that down.

        A blank billing class is a real error the full contract catches, and the
        footer cannot see it. Stated as a test so the default is not mistaken for
        a complete check.
        """
        table = pq.read_table(REAL)
        classes = table.column("billing_class").to_pylist()
        classes[0] = ""
        index = table.schema.get_field_index("billing_class")
        pq.write_table(
            table.set_column(index, "billing_class", pa.array(classes, type=pa.string())),
            lake / "Emblem_Blank.parquet",
        )

        assert "Emblem_Blank" in {f.stem for f in discover_payer_files(lake)}
        assert "Emblem_Blank" not in {
            f.stem for f in discover_payer_files(lake, validate=ContractCheck.FULL)
        }
