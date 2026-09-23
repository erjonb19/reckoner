"""The analyst page's logic, held to what the page claims.

Three promises carry the most weight. Every reason the pipeline can give reaches
the reader in words, never as a code. A release file that does not match the
checksum run.json recorded is refused, so code lookup cannot show one month's
rates beside another month's summary. And the default ranking counts
unexplained rates, because neither file publishes how often a service is billed
and a dollar sum invites being read as spend.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from pipeline import analyst_view as view
from reconcile.comparability import NotComparable
from reconcile.variance import NO_COUNTERPART, Explanation

ROOT = Path(__file__).resolve().parents[2]


def dataset() -> view.Dataset:
    pairs = [
        {
            "system": "WP",
            "facility": "White Plains",
            "carrier": "Aetna",
            "hospital_rates": 100,
            "compared": 60,
            "unexplained_material": 5,
            "summed_abs_gap_usd": 90_000.0,
            "median_signed_gap": 0.1,
        },
        {
            "system": "WP",
            "facility": "White Plains",
            "carrier": "Cigna",
            "hospital_rates": 80,
            "compared": 40,
            "unexplained_material": 30,
            "summed_abs_gap_usd": 1_000.0,
            "median_signed_gap": -0.2,
        },
        {
            "system": "MS",
            "facility": "Mount Sinai Queens",
            "carrier": "Aetna",
            "hospital_rates": 10,
            "compared": 5,
            "unexplained_material": 0,
            "summed_abs_gap_usd": 0.0,
            "median_signed_gap": 0.0,
        },
    ]
    coverage = [
        {"system": "WP", "candidates": 180, "pairs_formed": 100, "unexplained_and_material": 35},
        {"system": "MS", "candidates": 10, "pairs_formed": 5, "unexplained_and_material": 0},
    ]
    outcomes = [
        {
            "system": "WP",
            "facility": "White Plains",
            "carrier": "Aetna",
            "code_type": "CPT",
            "explanation": "within_payer_range",
            "pairs": 40,
        },
        {
            "system": "WP",
            "facility": "White Plains",
            "carrier": "Aetna",
            "code_type": "HCPCS",
            "explanation": "unexplained",
            "pairs": 5,
        },
        {
            "system": "WP",
            "facility": "White Plains",
            "carrier": "Aetna",
            "code_type": "CPT",
            "explanation": "unexplained",
            "pairs": 15,
        },
    ]
    refusals = [
        {
            "system": "WP",
            "facility": "White Plains",
            "carrier": "Aetna",
            "code_type": "CPT",
            "reason": "tic_exempt_product",
            "candidates": 30,
        },
        {
            "system": "WP",
            "facility": "White Plains",
            "carrier": "Aetna",
            "code_type": "CPT",
            "reason": NO_COUNTERPART,
            "candidates": 10,
        },
        {
            "system": "MS",
            "facility": "",
            "carrier": "Aetna",
            "code_type": "CPT",
            "reason": "zero_rate",
            "candidates": 7,
        },
    ]
    residual = [
        {
            "system": "WP",
            "facility": "White Plains",
            "carrier": "Aetna",
            "code": "1",
            "difference": -50.0,
        },
        {
            "system": "WP",
            "facility": "White Plains",
            "carrier": "Aetna",
            "code": "2",
            "difference": 400.0,
        },
    ]
    return view.Dataset(
        tables={
            "pairs": pairs,
            "coverage": coverage,
            "outcomes": outcomes,
            "refusals": refusals,
            "residual": residual,
        },
    )


class TestWords:
    @pytest.mark.parametrize("reason", [*NotComparable, NO_COUNTERPART])
    def test_every_reason_the_pipeline_emits_has_words(self, reason):
        label, sentence = view.REASONS[str(reason)]

        assert label and sentence
        assert "_" not in label

    @pytest.mark.parametrize("explanation", list(Explanation))
    def test_every_explanation_has_words(self, explanation):
        label, sentence = view.EXPLANATIONS[str(explanation)]

        assert label and sentence

    def test_an_unknown_reason_still_reads_as_words(self):
        assert view.reason_label("some_new_reason") == "Some new reason"


class TestFilters:
    def test_they_round_trip_through_the_url(self):
        filters = view.Filters(system="WP", carrier="Aetna")

        assert view.Filters.from_query(filters.to_query()) == filters
        assert filters.to_query() == {"system": "WP", "carrier": "Aetna"}

    def test_unrelated_parameters_are_ignored(self):
        assert view.Filters.from_query({"code": "70450", "system": "WP"}) == view.Filters(
            system="WP"
        )

    def test_a_row_without_the_column_is_not_filtered_out(self):
        """Pairs carry no code type; a code-type filter must not empty them."""
        rows = view.apply(dataset().table("pairs"), view.Filters(code_type="CPT"))

        assert len(rows) == 3

    def test_facility_choices_narrow_to_the_chosen_system(self):
        choices = view.options(dataset(), view.Filters(system="MS"))

        assert choices["facility"] == [view.ALL, "Mount Sinai Queens"]


class TestRankings:
    def test_the_default_ranks_by_unexplained_count(self):
        rows = view.rankings(dataset(), view.Filters())

        assert [r["carrier"] for r in rows[:2]] == ["Cigna", "Aetna"]
        assert [r["rank"] for r in rows] == [1, 2, 3]

    def test_the_gap_toggle_ranks_by_dollars(self):
        rows = view.rankings(dataset(), view.Filters(), by="gap")

        assert rows[0]["carrier"] == "Aetna"

    def test_the_headline_sums_the_filtered_systems(self):
        numbers = view.headline(dataset(), view.Filters(system="WP"))

        assert numbers == {"systems": 1, "hospital_rates": 180, "compared": 100, "unexplained": 35}


class TestPairDetail:
    def test_explanations_sum_across_code_types(self):
        detail = view.pair_detail(dataset(), "White Plains", "Aetna")
        assert detail is not None

        counts = {r["explanation"]: r["rates"] for r in detail.explanations}
        assert counts == {"within_payer_range": 40, "unexplained": 20}
        assert sum(r["share"] for r in detail.explanations) == pytest.approx(1.0)

    def test_refusals_are_in_words_and_largest_first(self):
        detail = view.pair_detail(dataset(), "White Plains", "Aetna")
        assert detail is not None

        assert [r["label"] for r in detail.refusals] == [
            "Exempt from the insurer rule",
            "No insurer rate",
        ]

    def test_residual_is_ordered_by_dollar_size_either_side(self):
        detail = view.pair_detail(dataset(), "White Plains", "Aetna")
        assert detail is not None

        assert [r["code"] for r in detail.residual] == ["2", "1"]

    def test_an_unknown_pair_is_none(self):
        assert view.pair_detail(dataset(), "Nowhere", "Aetna") is None


def rates() -> pa.Table:
    return pa.table(
        {
            "system": ["WP", "WP", "WP"],
            "facility": ["White Plains"] * 3,
            "carrier": ["Aetna", "Cigna", "Aetna"],
            "code": ["70450", "70450", "99213"],
            "code_type": ["CPT"] * 3,
            "hospital_rate": [1000.0, 800.0, 90.0],
            "compared": [1, 0, 1],
            "payer_min": [900.0, None, 80.0],
            "payer_median": [1200.0, None, 90.0],
            "payer_max": [1500.0, None, 100.0],
            "explanation_before_offsets": ["unexplained", "", "within_payer_range"],
            "refusal": ["", "tic_exempt_product", ""],
        }
    )


class TestCodeLookup:
    def test_it_shows_compared_and_refused_rows_in_words(self):
        rows = view.code_lookup(rates(), " 70450 ", view.Filters())

        assert [(r["carrier"], r["why"]) for r in rows] == [
            ("Aetna", "Unexplained"),
            ("Cigna", "Exempt from the insurer rule"),
        ]
        assert rows[0]["gap"] == pytest.approx(0.2)
        assert rows[1]["gap"] is None

    def test_filters_apply(self):
        rows = view.code_lookup(rates(), "70450", view.Filters(carrier="Cigna"))

        assert [r["carrier"] for r in rows] == ["Cigna"]

    def test_the_summary_counts_what_was_compared(self):
        summary = view.lookup_summary(view.code_lookup(rates(), "70450", view.Filters()))

        assert (summary["facilities"], summary["carriers"], summary["compared"]) == (1, 2, 1)
        assert summary["hospital_range"] == (800.0, 1000.0)

    def test_suggestions_match_code_prefix_or_description(self):
        codes = pa.table(
            {
                "code_type": ["CPT", "CPT"],
                "code": ["70450", "99213"],
                "description": ["CT HEAD W/O CONTRAST", "OFFICE VISIT"],
            }
        )

        assert [c for c, _, _ in view.suggest(codes, "704")] == ["70450"]
        assert [c for c, _, _ in view.suggest(codes, "office")] == ["99213"]
        assert view.describe(codes, "99213") == "OFFICE VISIT"


def published(tmp_path: Path) -> tuple[dict[str, object], bytes]:
    sink = io.BytesIO()
    pq.write_table(rates(), sink)
    data = sink.getvalue()
    metadata = {
        "release": {
            "tag": "data-2026-10-01",
            "files": {"rates.parquet": {"sha256": hashlib.sha256(data).hexdigest()}},
        }
    }
    return metadata, data


class TestTheRelease:
    def test_a_verified_file_is_downloaded_once_and_then_cached(self, tmp_path):
        metadata, data = published(tmp_path)
        calls: list[str] = []

        def opener(url: str) -> io.BytesIO:
            calls.append(url)
            return io.BytesIO(data)

        first = view.fetch_release_file(metadata, "rates.parquet", tmp_path, opener)
        second = view.fetch_release_file(metadata, "rates.parquet", tmp_path, opener)

        assert first.available and second.available
        assert len(calls) == 1
        assert calls[0].endswith("/releases/download/data-2026-10-01/rates.parquet")
        assert view.read_rates(first.path).num_rows == 3  # type: ignore[arg-type]

    def test_a_file_that_does_not_match_its_checksum_is_refused(self, tmp_path):
        metadata, _ = published(tmp_path)

        got = view.fetch_release_file(
            metadata, "rates.parquet", tmp_path, lambda url: io.BytesIO(b"not it")
        )

        assert not got.available
        assert "checksum" in got.reason
        assert not list(tmp_path.rglob("*.parquet*")), "nothing unverified is left behind"

    def test_a_network_failure_says_so(self, tmp_path):
        metadata, _ = published(tmp_path)

        def opener(url: str) -> io.BytesIO:
            raise OSError("offline")

        got = view.fetch_release_file(metadata, "rates.parquet", tmp_path, opener)

        assert not got.available
        assert "could not download" in got.reason

    def test_a_summary_without_a_release_says_so(self, tmp_path):
        got = view.fetch_release_file({}, "rates.parquet", tmp_path)

        assert not got.available
        assert "no release" in got.reason


class TestTheCommittedDataset:
    @pytest.mark.skipif(not (ROOT / "summary" / "pairs.csv").exists(), reason="no dataset")
    def test_every_reason_in_it_has_words(self):
        data = view.load(ROOT / "summary")

        for row in data.table("refusals"):
            assert row["reason"] in view.REASONS
        for row in data.table("outcomes"):
            assert row["explanation"] in view.EXPLANATIONS

    @pytest.mark.skipif(not (ROOT / "summary" / "pairs.csv").exists(), reason="no dataset")
    def test_it_names_a_release_with_checksums(self):
        release = view.load(ROOT / "summary").metadata["release"]

        assert set(release["files"]) == {"rates.parquet", "codes.parquet"}
        assert all(len(f["sha256"]) == 64 for f in release["files"].values())

    def test_to_csv_keeps_the_rows_on_screen(self):
        text = view.to_csv([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]).decode()

        assert text.splitlines() == ["a,b", "1,x", "2,y"]
