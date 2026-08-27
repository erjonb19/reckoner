import json

import httpx
import pyarrow.parquet as pq
import pytest
import respx

from hospital.ingest_cli import ingest_one, slugify
from hospital.landing import Landing

from .test_parser import JSON_MRF

URL = "https://example.org/mrf.json"

DIRTY_MRF = json.dumps(
    {
        "hospital_name": "Example Hospital",
        "last_updated_on": "2026-04-01",
        "standard_charge_information": [
            {
                "description": "CT Scan",
                "code_information": [{"code": "70450", "type": "CPT"}],
                "standard_charges": [
                    {
                        "setting": "outpatient",
                        "payers_information": [
                            {
                                "payer_name": "Aetna",
                                "plan_name": "Commercial",
                                "standard_charge_dollar": 412.55,
                            },
                            {
                                "payer_name": "Broken",
                                "plan_name": "Commercial",
                                "standard_charge_dollar": "not-a-number",
                            },
                            {
                                "payer_name": "Self-Pay",
                                "plan_name": "Self-Pay",
                                "standard_charge_dollar": 100,
                            },
                            {"plan_name": "Orphan", "standard_charge_dollar": 5},
                        ],
                    }
                ],
            }
        ],
    }
)


@pytest.fixture
def landing(tmp_path):
    return Landing(tmp_path)


def _client() -> httpx.Client:
    return httpx.Client()


def test_slugify():
    assert slugify("NYC Health + Hospitals") == "nyc-health-hospitals"
    assert slugify("St. Peter's") == "st-peter-s"
    assert slugify("!!!") == "unknown"


@respx.mock
def test_successful_load_writes_curated_parquet_and_audit(landing):
    respx.get(URL).mock(return_value=httpx.Response(200, text=JSON_MRF))

    with _client() as client:
        audit = ingest_one(client, landing, "Example Hospital", URL)

    assert audit.status == "ok"
    assert audit.rows_in == 2
    assert audit.rows_out == 2
    assert audit.file_vintage == "2026-04-01"
    assert audit.checksum and len(audit.checksum) == 64
    assert audit.bytes_read > 0

    files = list(landing.curated.rglob("*.parquet"))
    assert files, "expected curated parquet output"
    table = pq.read_table(next(f for f in files if "rates" in str(f)))
    assert table.num_rows == 2
    assert set(table.column("payer_name_raw").to_pylist()) == {"Aetna", "Humana"}

    # Partitioned by hospital and vintage.
    assert any("hospital_slug=example-hospital" in str(f) for f in files)
    assert any("vintage=2026-04" in str(f) for f in files)

    audits = landing.read_audit()
    assert len(audits) == 1 and audits[0]["status"] == "ok"


@respx.mock
def test_bad_rows_are_quarantined_not_fatal(landing):
    respx.get(URL).mock(return_value=httpx.Response(200, text=DIRTY_MRF))

    with _client() as client:
        audit = ingest_one(client, landing, "Example Hospital", URL)

    assert audit.status == "ok"
    assert audit.rows_in == 4
    assert audit.rows_out == 1
    assert audit.rows_rejected == 3
    assert audit.reject_reasons == {
        "non_numeric_rate": 1,
        "non_payer_row": 1,
        "missing_payer": 1,
    }

    rejects = [f for f in landing.curated.rglob("*.parquet") if "rejects" in str(f)]
    assert rejects, "rejects must be landed, not dropped"
    table = pq.read_table(rejects[0])
    assert table.num_rows == 3
    assert "non_numeric_rate" in table.column("reason").to_pylist()


@respx.mock
def test_reloading_an_unchanged_file_is_idempotent(landing):
    respx.get(URL).mock(return_value=httpx.Response(200, text=JSON_MRF))

    with _client() as client:
        first = ingest_one(client, landing, "Example Hospital", URL)
        second = ingest_one(client, landing, "Example Hospital", URL)

    assert first.status == "ok"
    assert second.status == "duplicate"
    assert second.checksum == first.checksum
    rate_files = [f for f in landing.curated.rglob("*.parquet") if "rates" in str(f)]
    assert len(rate_files) == 1, "a duplicate load must not add a second partition"


@respx.mock
def test_a_changed_file_supersedes_the_previous_load(landing):
    respx.get(URL).mock(return_value=httpx.Response(200, text=JSON_MRF))
    with _client() as client:
        ingest_one(client, landing, "Example Hospital", URL)

    changed = JSON_MRF.replace("412.55", "999.99")
    respx.get(URL).mock(return_value=httpx.Response(200, text=changed))
    with _client() as client:
        second = ingest_one(client, landing, "Example Hospital", URL)

    assert second.status == "ok"
    rate_files = [f for f in landing.curated.rglob("*.parquet") if "rates" in str(f)]
    assert len(rate_files) == 1, "a republished file supersedes its own prior load"
    values = pq.read_table(rate_files[0]).column("rate_dollar").to_pylist()
    assert 999.99 in values and 412.55 not in values
    assert [a["status"] for a in landing.read_audit()] == ["ok", "ok"]


@respx.mock
def test_interrupted_download_leaves_nothing_curated(landing):
    """A partial load must not look like a complete one."""
    respx.get(URL).mock(side_effect=httpx.ReadError("connection reset"))

    with _client() as client:
        audit = ingest_one(client, landing, "Example Hospital", URL)

    assert audit.status == "failed"
    assert "ReadError" in (audit.error or "")
    assert not list(landing.curated.rglob("*.parquet"))
    assert not list(landing.staging.rglob("*.parquet"))
    assert landing.read_audit()[0]["status"] == "failed"


@respx.mock
def test_http_error_is_audited(landing):
    respx.get(URL).mock(return_value=httpx.Response(404))

    with _client() as client:
        audit = ingest_one(client, landing, "Example Hospital", URL)

    assert audit.status == "failed"
    assert not list(landing.curated.rglob("*.parquet"))


@respx.mock
def test_file_with_no_payer_rates_is_flagged_empty(landing):
    """Mount Sinai Brooklyn publishes gross/cash only -- no negotiated rates."""
    body = json.dumps(
        {
            "hospital_name": "Gross Only",
            "last_updated_on": "2026-04-01",
            "standard_charge_information": [
                {
                    "description": "CT",
                    "code_information": [{"code": "70450", "type": "CPT"}],
                    "standard_charges": [{"gross_charge": 100, "discounted_cash": 90}],
                }
            ],
        }
    )
    respx.get(URL).mock(return_value=httpx.Response(200, text=body))

    with _client() as client:
        audit = ingest_one(client, landing, "Gross Only", URL)

    assert audit.status == "empty"
    assert audit.rows_in == 0
    assert not list(landing.curated.rglob("*.parquet"))


CODE_YAML = """
inpatient:
  - code: "470"
    label: Joint replacement
outpatient:
  - code: "70450"
    label: CT head
"""


def test_codeset_loads_and_normalises(tmp_path):
    from hospital.codeset import CodeSet

    path = tmp_path / "codes.yml"
    path.write_text(CODE_YAML, encoding="utf-8")
    codes = CodeSet.from_yaml(path)

    assert len(codes) == 2
    assert "70450" in codes
    assert "470" in codes
    assert "0470" in codes, "zero-padded DRGs must match"
    assert "99213" not in codes
    assert codes.label_for("470") == "Joint replacement"


def test_everything_codeset_matches_all():
    from hospital.codeset import CodeSet

    assert "anything" in CodeSet.everything()


@respx.mock
def test_out_of_scope_codes_are_filtered_not_rejected(landing, tmp_path):
    """Filtering is scope, not failure: it must not inflate the reject rate."""
    from hospital.codeset import CodeSet

    path = tmp_path / "codes.yml"
    path.write_text('outpatient:\n  - code: "99999"\n', encoding="utf-8")
    respx.get(URL).mock(return_value=httpx.Response(200, text=JSON_MRF))

    with _client() as client:
        audit = ingest_one(client, landing, "Example Hospital", URL, codes=CodeSet.from_yaml(path))

    assert audit.rows_seen == 2
    assert audit.rows_filtered == 2
    assert audit.rows_in == 0
    assert audit.rows_rejected == 0
    assert audit.reject_rate == 0.0
    assert audit.status == "empty"


@respx.mock
def test_in_scope_codes_are_kept(landing, tmp_path):
    from hospital.codeset import CodeSet

    path = tmp_path / "codes.yml"
    path.write_text('outpatient:\n  - code: "70450"\n', encoding="utf-8")
    respx.get(URL).mock(return_value=httpx.Response(200, text=JSON_MRF))

    with _client() as client:
        audit = ingest_one(client, landing, "Example Hospital", URL, codes=CodeSet.from_yaml(path))

    assert audit.rows_seen == 2
    assert audit.rows_filtered == 0
    assert audit.rows_out == 2
    assert audit.status == "ok"


@respx.mock
def test_curated_dataset_is_readable_as_one_table(landing):
    """A partition key sharing a column name makes the dataset unreadable."""
    respx.get(URL).mock(return_value=httpx.Response(200, text=JSON_MRF))
    other = "https://example.org/other.json"
    respx.get(other).mock(return_value=httpx.Response(200, text=JSON_MRF))

    with _client() as client:
        ingest_one(client, landing, "Hospital A", URL)
        ingest_one(client, landing, "Hospital B", other)

    rate_files = [f for f in landing.curated.rglob("*.parquet") if "rates" in str(f)]
    assert len(rate_files) == 2

    table = pq.read_table(rate_files)
    assert table.num_rows == 4
    assert set(table.column("hospital").to_pylist()) == {"Hospital A", "Hospital B"}


def test_sweep_staging_removes_orphans_from_a_killed_run(landing):
    orphan = landing.staging / "20260101T000000000000-deadbeef"
    orphan.mkdir(parents=True)
    (orphan / "rates.parquet").write_bytes(b"partial")

    swept = landing.sweep_staging()

    assert swept == ["20260101T000000000000-deadbeef"]
    assert not list(landing.staging.iterdir())


def test_sweep_staging_is_safe_when_nothing_is_staged(landing):
    assert landing.sweep_staging() == []


def test_summary_counts_only_rows_that_landed(capsys):
    """A failed batch is discarded; its rows must not be reported as curated."""
    from hospital.ingest_cli import _summarise
    from hospital.landing import LoadAudit

    _summarise(
        [
            LoadAudit("b1", "u1", "H", "t", status="ok", rows_seen=100, rows_in=10, rows_out=10),
            LoadAudit("b2", "u2", "H", "t", status="failed", rows_seen=50, rows_in=5, rows_out=5),
        ]
    )

    out = capsys.readouterr().out
    assert "rows landed in the curated tree: 10" in out
    assert "5 curated rows discarded with 1 failed batches" in out


def test_is_transient_classifies_errors():
    from hospital.ingest_cli import is_transient

    assert is_transient("RemoteProtocolError: peer closed connection")
    assert is_transient("ReadTimeout: timed out")
    assert not is_transient("HTTPStatusError: 404")
    assert not is_transient("ArrowTypeError: bad schema")
    assert not is_transient(None)


@respx.mock
def test_transient_failure_is_retried_and_succeeds(landing):
    """NYC H+H's CDN truncates large responses; one flake must not lose a batch."""
    route = respx.get(URL).mock(
        side_effect=[
            httpx.RemoteProtocolError("peer closed connection"),
            httpx.Response(200, text=JSON_MRF),
        ]
    )

    with _client() as client:
        audit = ingest_one(client, landing, "Example Hospital", URL, backoff=0)

    assert route.call_count == 2
    assert audit.status == "ok"
    assert audit.attempts == 2
    assert audit.rows_out == 2


@respx.mock
def test_retries_are_bounded(landing):
    route = respx.get(URL).mock(side_effect=httpx.RemoteProtocolError("boom"))

    with _client() as client:
        audit = ingest_one(client, landing, "Example Hospital", URL, max_attempts=3, backoff=0)

    assert route.call_count == 3
    assert audit.status == "failed"
    assert audit.attempts == 3


@respx.mock
def test_a_404_is_not_retried(landing):
    """Permanent faults do not get better by asking again."""
    route = respx.get(URL).mock(return_value=httpx.Response(404))

    with _client() as client:
        audit = ingest_one(client, landing, "Example Hospital", URL, backoff=0)

    assert route.call_count == 1
    assert audit.attempts == 1


@respx.mock
def test_one_audit_row_per_file_regardless_of_retries(landing):
    respx.get(URL).mock(side_effect=[httpx.ReadTimeout("t"), httpx.Response(200, text=JSON_MRF)])

    with _client() as client:
        ingest_one(client, landing, "Example Hospital", URL, backoff=0)

    assert len(landing.read_audit()) == 1
