"""The spool changes where the mart's bytes come from, and nothing else.

The hard rule: gold is byte-identical with and without it. These build a small
silver lake in the real silver schema, run the real mart over it both ways, and
compare every gold file by sha256. The real-system proof is White Plains on the
2026-10-01 run, against the checksums recorded in
``docs/measurements/white-plains-gold-2026-09-23.json``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

import storage
from pipeline import mart, spool

SLUG, HOSPITAL = "mount-sinai-health-system", "Mount Sinai Health System"
HOSPITAL_SCHEMA = pa.schema(
    [
        (name, pa.string())
        for name in (
            "batch_id",
            "source_url",
            "hospital",
            "location_name",
            "file_vintage",
            "code",
            "revenue_code",
            "procedure_code",
            "drg_code",
            "all_codes",
            "description",
            "setting",
            "billing_class",
            "payer_name_raw",
            "plan_name_raw",
            "payer_key",
            "plan_key",
            "product_class",
            "rate_kind",
        )
    ]
    + [("rate_dollar", pa.float64()), ("rate_percentage", pa.float64())]
    + [("rate_algorithm", pa.string()), ("methodology", pa.string())]
    + [("gross_charge", pa.float64()), ("discounted_cash", pa.float64()), ("row_hash", pa.string())]
)


def hospital_rows(hospital: str, facility: str, codes: range, scale: float) -> pa.Table:
    rows: list[dict[str, Any]] = []
    for i, code in enumerate(codes):
        for payer, plan in (("Aetna", "Aetna PPO"), ("UnitedHealthcare", "UHC Choice Plus")):
            rows.append(
                {
                    "batch_id": "b",
                    "source_url": "https://example.org/mrf.json",
                    "hospital": hospital,
                    "location_name": facility,
                    "file_vintage": "2026-04-01",
                    "code": str(code),
                    "revenue_code": None,
                    "procedure_code": str(code),
                    "drg_code": None,
                    "all_codes": f'[["{code}","CPT"]]',
                    "description": f"SERVICE {code}",
                    "setting": "outpatient",
                    "billing_class": "facility",
                    "payer_name_raw": payer,
                    "plan_name_raw": plan,
                    "payer_key": payer.lower(),
                    "plan_key": plan.lower(),
                    "product_class": "commercial",
                    "rate_kind": "dollar",
                    "rate_dollar": round(scale * (100 + 7 * i), 2),
                    "rate_percentage": None,
                    "rate_algorithm": None,
                    "methodology": "fee schedule",
                    "gross_charge": None,
                    "discounted_cash": None,
                    "row_hash": f"{facility}{code}{payer}",
                }
            )
    return pa.Table.from_pylist(rows, schema=HOSPITAL_SCHEMA)


def payer_rows(stem: str, codes: range, scale: float) -> pa.Table:
    n = len(codes)
    return pa.table(
        {
            "billing_code": [str(c) for c in codes],
            "code_type": ["CPT"] * n,
            "description": ["x"] * n,
            "negotiated_rate": [round(scale * (100 + 7 * i), 2) for i in range(n)],
            "rate_type": ["negotiated"] * n,
            "billing_class": ["institutional"] * n,
            "service_codes": [""] * n,
            "expiration_date": [""] * n,
            "matched_npis": [""] * n,
            "matched_tins": [""] * n,
            "group_tins": pa.array([0] * n, pa.int64()),
            "network_names": [""] * n,
            "payer": [stem] * n,
            "reporting_entity_name": ["r"] * n,
            "last_updated_on": ["2026-08-01"] * n,
            "schema_version": ["1.0"] * n,
            "systems": ["Mount Sinai"] * n,
            "system_count": pa.array([1] * n, pa.int64()),
        }
    )


def put(root: Path, parts: tuple[str, ...], table: pa.Table, name: str = "part-0.parquet") -> None:
    target = root.joinpath(*parts)
    target.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, target / name)


def a_lake(tmp_path: Path) -> Path:
    """Two systems, several partitions and files, disagreements to find."""
    lake = tmp_path / "lake"
    hospital = lake / "silver" / "hospital_rates"
    put(
        hospital,
        (f"hospital_slug={SLUG}", "code_type=CPT", "vintage=2026-04"),
        hospital_rows(HOSPITAL, "Mount Sinai Queens", range(10000, 10030), 1.0),
    )
    put(
        hospital,
        (f"hospital_slug={SLUG}", "code_type=CPT", "vintage=2026-04"),
        hospital_rows(HOSPITAL, "Mount Sinai Brooklyn", range(20000, 20020), 1.4),
        "part-1.parquet",
    )
    put(
        hospital,
        ("hospital_slug=white-plains-hospital", "code_type=CPT", "vintage=2026-04"),
        hospital_rows("White Plains Hospital", "White Plains Hospital", range(10000, 10010), 2.0),
    )
    payer = lake / "silver" / "payer_rates"
    put(
        payer,
        ("carrier=Aetna", "vintage=2026-08"),
        payer_rows("Aetna_Ppo", range(10000, 10030), 1.3),
    )
    put(
        payer,
        ("carrier=UHC", "vintage=2026-08"),
        payer_rows("UHC_NY_Choice", range(20000, 20020), 0.9),
    )
    return lake


def gold(
    lake: Path, out: Path, spool_dir: Path | None, events: list[str] | None = None
) -> dict[str, str]:
    hook = (lambda event, **_: events.append(event)) if events is not None else None
    runs = mart.build(storage.local(lake), only=SLUG, spool_dir=spool_dir, on_spool=hook)
    mart.write(storage.local(out), mart.tables(runs), systems={SLUG})
    return {
        p.relative_to(out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(out.rglob("*.parquet"))
    }


class TestByteIdentical:
    def test_gold_is_the_same_bytes_with_and_without_the_spool(self, tmp_path):
        lake = a_lake(tmp_path)
        events: list[str] = []

        direct = gold(lake, tmp_path / "direct", None)
        spooled = gold(lake, tmp_path / "spooled", tmp_path / "spool", events)

        assert direct and direct == spooled
        assert events == ["spooled", "spooled"], "payer silver, then the system's partition"

    def test_the_comparison_is_not_vacuous(self, tmp_path):
        """Byte-identical empty tables would prove nothing."""
        lake = a_lake(tmp_path)
        runs = mart.build(storage.local(lake), only=SLUG, spool_dir=tmp_path / "spool")

        (run,) = runs
        assert run.coverage_row()["pairs_formed"] > 0
        assert run.rate_table().num_rows > 0

    def test_nothing_is_left_on_disk(self, tmp_path):
        lake = a_lake(tmp_path)

        gold(lake, tmp_path / "out", tmp_path / "spool")

        assert not any((tmp_path / "spool").rglob("*.parquet"))


class TestReproducible:
    def test_the_same_input_writes_the_same_bytes_every_time(self, tmp_path):
        """``rates`` once came out in two row orders across six runs."""
        lake = a_lake(tmp_path)

        runs = [gold(lake, tmp_path / f"run{i}", None) for i in range(4)]

        assert all(run == runs[0] for run in runs)


class TestFallingBack:
    def test_no_room_means_direct_reads_and_the_same_gold(self, tmp_path, monkeypatch):
        lake = a_lake(tmp_path)
        direct = gold(lake, tmp_path / "direct", None)
        monkeypatch.setattr(spool.shutil, "disk_usage", lambda _: type("U", (), {"free": 10})())
        events: list[str] = []

        spooled = gold(lake, tmp_path / "spooled", tmp_path / "spool", events)

        assert spooled == direct
        assert events == ["spool_skipped", "spool_skipped"]

    def test_a_system_with_rows_outside_its_partition_is_not_spooled(self, tmp_path):
        """Copying one partition would lose them; the direct read keeps them."""
        lake = a_lake(tmp_path)
        put(
            lake / "silver" / "hospital_rates",
            ("hospital_slug=stray", "code_type=CPT", "vintage=2026-04"),
            hospital_rows(HOSPITAL, "Mount Sinai Queens", range(10000, 10005), 3.0),
        )
        direct = gold(lake, tmp_path / "direct", None)
        events: list[str] = []

        spooled = gold(lake, tmp_path / "spooled", tmp_path / "spool", events)

        assert spooled == direct
        assert events == ["spooled"], "payer silver only"

    def test_a_failed_copy_leaves_nothing_behind(self, tmp_path, monkeypatch):
        lake = a_lake(tmp_path)

        def broken(*_: object, **__: object) -> None:
            (tmp_path / "spool" / "payer_rates" / "half.parquet").write_bytes(b"half")
            raise OSError("connection reset")

        monkeypatch.setattr(spool.pafs, "copy_files", broken)
        events: list[str] = []

        ok = spool.copy_down(
            storage.local(lake).child("silver", "payer_rates"),
            tmp_path / "spool" / "payer_rates",
            on_event=lambda e, **_: events.append(e),
        )

        assert ok is False and events == ["spool_failed"]
        assert not (tmp_path / "spool" / "payer_rates").exists()


def test_the_recorded_baseline_names_every_mart_table():
    """The October 1 comparison needs a checksum for each table the mart writes."""
    import json

    baseline = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "docs"
            / "measurements"
            / "white-plains-gold-2026-09-23.json"
        ).read_text(encoding="utf-8")
    )
    tables = {name.split("/")[0] for name in baseline["files"]}

    assert set(mart.TABLES) <= tables
    assert all(len(f["sha256"]) == 64 for f in baseline["files"].values())


class TestTheJob:
    def test_it_spools_by_default_and_can_be_turned_off(self):
        import reckoner_job

        assert reckoner_job.spool_dir({}) is not None
        assert reckoner_job.spool_dir({"RECKONER_SPOOL_DIR": "off"}) is None
        assert reckoner_job.spool_dir({"RECKONER_SPOOL_DIR": "/mnt/x"}) == Path("/mnt/x")
