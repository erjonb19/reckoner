"""Payer curated-loader tests, built from real Parquet slices.

Every fixture under ``tests/fixtures/payer_parquet`` is rows copied verbatim out
of the real ``mrf_pipeline/payer_parquet`` output, so the sentinels here are the
ones the payer files actually contain -- ``CSTM-00`` place-of-service, ``0.0``
placeholder rates, empty ``matched_npis``, and whole duplicate rows -- rather
than values invented from the schema document.

The tests are written as "this must not silently produce a number", because
every hazard in this loader fails quietly: a duplicate file double-weights a
median, a percentage row averaged with dollars drags a summary down, and a
multi-system row credited to one system is the bug that already shipped once
upstream.
"""

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from payer.curated import (
    DUPLICATE_PAYER_FILES,
    PAYER_SOURCE_VINTAGES,
    PayerFilter,
    SystemAttribution,
    aggregate_rates,
    discover_payer_files,
    distinct_systems,
    file_summary,
    load_comparable_rates,
    open_payer_dataset,
    to_comparable_rates,
)
from reconcile.comparability import ComparableRate, NotComparable, can_compare

FIXTURES = Path(__file__).parent.parent / "fixtures" / "payer_parquet"

#: The facility names NYU Langone publishes on the hospital side.
NYU_FACILITIES = {
    "NYU Langone": [
        "NYU Langone|Brooklyn",
        "NYU Langone|Long Island",
        "NYU Langone|Tisch Hospital",
    ]
}


@pytest.fixture
def files():
    return discover_payer_files(FIXTURES)


@pytest.fixture
def table(files):
    return aggregate_rates(open_payer_dataset(files), PayerFilter())


class TestDiscovery:
    def test_in_flight_part_file_is_never_opened(self, files):
        """A ``.part`` is an open writer handle with no footer, not a short file.

        The fixture is a headerless stub, so a loader that globbed it would
        raise rather than return fewer rows.
        """
        assert (FIXTURES / "AetnaALIC_Epo.parquet.part").exists()
        assert "AetnaALIC_Epo" not in {f.stem for f in files}

        # And it is genuinely unreadable, so this is not a vacuous assertion.
        with pytest.raises(pa.ArrowInvalid, match="magic bytes not found"):
            pq.read_table(FIXTURES / "AetnaALIC_Epo.parquet.part")

    def test_superseded_output_directory_is_not_read(self, files):
        """``old_npi_only/`` holds earlier runs and is outside the contract."""
        assert (FIXTURES / "old_npi_only" / "Aetna_NY.parquet").exists()
        assert [f.stem for f in files].count("Aetna_NY") == 1

    def test_duplicate_cigna_file_is_dropped_by_default(self, files):
        """Pathwell is row-for-row identical to National; keeping both doubles it."""
        stems = {f.stem for f in files}
        assert "Cigna_NationalOAP" in stems
        assert "Cigna_PathwellOAP" not in stems
        assert {"Cigna_PathwellOAP", "Cigna_PathwellPPO"} == DUPLICATE_PAYER_FILES

    def test_duplicate_can_be_included_deliberately(self):
        stems = {f.stem for f in discover_payer_files(FIXTURES, include_duplicates=True)}
        assert "Cigna_PathwellOAP" in stems

    def test_config_label_splits_into_carrier_and_network(self, files):
        by_stem = {f.stem: f for f in files}
        assert by_stem["AetnaALIC_Hmo"].carrier == "AetnaALIC"
        assert by_stem["AetnaALIC_Hmo"].network == "Hmo"
        assert by_stem["Cigna_NationalOAP"].network == "NationalOAP"

    def test_vintage_comes_from_the_declared_table_not_the_file_mtime(self, files):
        """The Parquet has no as-of date; mtime would date it to the parse run."""
        by_stem = {f.stem: f for f in files}
        assert by_stem["Aetna_NY"].vintage == "2026-06-05"
        assert by_stem["AetnaALIC_Hmo"].vintage == "2026-08-05"
        assert by_stem["Cigna_NationalOAP"].vintage == "2026-08-01"
        assert PAYER_SOURCE_VINTAGES["Aetna_NY"] != PAYER_SOURCE_VINTAGES["AetnaALIC_Hmo"]

    def test_summary_names_what_was_skipped_and_why(self):
        rows = {r["stem"]: r for r in file_summary(FIXTURES)}
        assert rows["AetnaALIC_Epo"]["read"] is False
        assert "in flight" in rows["AetnaALIC_Epo"]["skipped_reason"]
        assert rows["Cigna_PathwellOAP"]["read"] is False
        assert "duplicate" in rows["Cigna_PathwellOAP"]["skipped_reason"]
        assert rows["Aetna_NY"]["read"] is True


class TestAggregation:
    def test_exact_duplicate_rows_are_removed_before_counting(self, files):
        """The fixture repeats 12 whole rows, as the real Aetna_NY repeats 17.8%."""
        raw = open_payer_dataset(files).to_table()
        assert raw.num_rows == 278  # 198 Aetna_NY + 40 ALIC + 40 Cigna

        table = aggregate_rates(
            open_payer_dataset(files),
            PayerFilter(drop_untranslatable_codes=False, attribution=SystemAttribution.EXPLODE),
        )
        counted = sum(table.column("negotiated_rate_count").to_pylist())
        assert counted < raw.num_rows, "duplicate rows survived into the counts"

    def test_several_rates_on_one_key_collapse_to_a_median(self, files):
        """45% of upstream keys carry more than one distinct rate."""
        table = aggregate_rates(open_payer_dataset(files), PayerFilter())
        counts = table.column("negotiated_rate_count").to_pylist()
        assert max(counts) > 1
        assert table.num_rows < 278

    def test_empty_result_is_an_empty_table_not_an_error(self, files):
        table = aggregate_rates(open_payer_dataset(files), PayerFilter(codes=("nope",)))
        assert table.num_rows == 0
        assert to_comparable_rates(table, files) == []


class TestFilters:
    def test_untranslatable_code_systems_are_dropped_by_default(self, files):
        """Aetna's LOCAL codes have no meaning in a hospital file."""
        table = aggregate_rates(open_payer_dataset(files), PayerFilter())
        assert "LOCAL" not in set(table.column("code_type").to_pylist())

    def test_untranslatable_codes_can_be_kept(self, files):
        table = aggregate_rates(
            open_payer_dataset(files), PayerFilter(drop_untranslatable_codes=False)
        )
        assert "LOCAL" in set(table.column("code_type").to_pylist())

    def test_placeholder_rates_survive_by_default_so_they_can_be_refused(self, files):
        """A default that dropped them would flatter the comparable share."""
        table = aggregate_rates(open_payer_dataset(files), PayerFilter())
        assert min(table.column("negotiated_rate_approximate_median").to_pylist()) == 0.0

    def test_min_rate_removes_the_placeholders(self, files):
        table = aggregate_rates(open_payer_dataset(files), PayerFilter(min_rate=1.0))
        assert min(table.column("negotiated_rate_approximate_median").to_pylist()) >= 1.0

    def test_group_tins_bounds_the_network_wide_fee_schedules(self, files):
        """A rate shared with 23,875 tax IDs is not this system's contract."""
        wide = aggregate_rates(open_payer_dataset(files), PayerFilter())
        narrow = aggregate_rates(open_payer_dataset(files), PayerFilter(max_group_tins=10))
        assert max(wide.column("group_tins_min").to_pylist()) > 1000
        assert max(narrow.column("group_tins_min").to_pylist()) <= 10

    def test_code_prefix_keeps_only_that_shard(self, files):
        table = aggregate_rates(open_payer_dataset(files), PayerFilter(code_prefix="J"))
        codes = table.column("billing_code").to_pylist()
        assert codes, "fixture must contain J codes for this to mean anything"
        assert all(c.startswith("J") for c in codes)

    def test_the_shards_partition_the_data_exactly(self, files):
        """The claim the memory fix rests on: sharding loses and duplicates nothing.

        ``aggregate_rates`` holds the whole filtered table plus a distinct over
        every column, so a large system has to be run in slices. That is only
        sound if the slices reassemble into the same set of rows.
        """
        whole = aggregate_rates(open_payer_dataset(files), PayerFilter())
        prefixes = {c[0] for c in whole.column("billing_code").to_pylist() if c}
        assert len(prefixes) > 1, "fixture must span several shards"

        swept: list[str] = []
        for prefix in sorted(prefixes):
            shard = aggregate_rates(open_payer_dataset(files), PayerFilter(code_prefix=prefix))
            swept.extend(shard.column("billing_code").to_pylist())

        assert sorted(swept) == sorted(whole.column("billing_code").to_pylist())

    def test_system_filter_matches_inside_the_comma_joined_list(self, files):
        exploded = aggregate_rates(
            open_payer_dataset(files),
            PayerFilter(systems=("NYU Langone",), attribution=SystemAttribution.EXPLODE),
        )
        systems = set(exploded.column("systems").to_pylist())
        assert any("," in s for s in systems), "shared rows should be reachable"
        assert all("NYU Langone" in s for s in systems)


class TestSystemAttribution:
    def test_exclusive_keeps_only_unambiguous_rows(self, files):
        rates = to_comparable_rates(
            aggregate_rates(open_payer_dataset(files), PayerFilter()),
            files,
            attribution=SystemAttribution.EXCLUSIVE,
        )
        # Every rate names one system, never a comma-joined list.
        assert all("," not in (r.location or "") for r in rates)

    def test_explode_credits_every_system_the_rate_touches(self, files):
        where = PayerFilter(attribution=SystemAttribution.EXPLODE)
        rates = to_comparable_rates(
            aggregate_rates(open_payer_dataset(files), where),
            files,
            attribution=SystemAttribution.EXPLODE,
        )
        exclusive = to_comparable_rates(
            aggregate_rates(open_payer_dataset(files), PayerFilter()),
            files,
            attribution=SystemAttribution.EXCLUSIVE,
        )
        assert len(rates) > len(exclusive), "shared rows should add attributions"

    def test_a_shared_row_is_never_credited_to_one_system(self, files):
        """The pre-existing upstream bug: 44% of rows misattributed."""
        everything = aggregate_rates(
            open_payer_dataset(files), PayerFilter(attribution=SystemAttribution.EXPLODE)
        )
        shared = {s for s in everything.column("systems").to_pylist() if "," in s}
        assert shared, "fixture must contain multi-system rows"

        table = aggregate_rates(open_payer_dataset(files), PayerFilter())
        rates = to_comparable_rates(table, files, attribution=SystemAttribution.EXCLUSIVE)
        assert not any(r.location in shared for r in rates)


class TestCuratedShape:
    def test_percentage_rows_are_not_typed_as_dollars(self, files):
        """100.0 is a percent of billed charges, not a $100 rate."""
        table = aggregate_rates(
            open_payer_dataset(files), PayerFilter(drop_untranslatable_codes=False)
        )
        rates = to_comparable_rates(table, files)
        percents = [r for r in rates if r.methodology == "percentage"]
        assert percents
        assert all(r.rate_kind == "percentage" for r in percents)

    def test_dollar_rows_are_typed_as_dollars(self, files, table):
        rates = to_comparable_rates(table, files)
        assert {r.rate_kind for r in rates if r.methodology == "negotiated"} == {"dollar"}

    def test_per_diem_keeps_its_methodology_so_it_can_be_refused(self, files):
        table = aggregate_rates(
            open_payer_dataset(files), PayerFilter(drop_untranslatable_codes=False)
        )
        rates = to_comparable_rates(table, files)
        per_diem = [r for r in rates if r.methodology_family == "per_diem"]
        assert per_diem, "fixture must contain per diem rates"

    def test_institutional_is_translated_to_the_hospital_word(self, files, table):
        rates = to_comparable_rates(table, files)
        classes = {r.billing_class for r in rates}
        assert classes == {"facility", "professional"}
        assert "institutional" not in classes

    def test_unrestricted_place_of_service_becomes_both(self, files, table):
        """``CSTM-00`` is a payer literal for "all places", not POS code 00."""
        rates = to_comparable_rates(table, files)
        assert "both" in {r.setting for r in rates}

    def test_inpatient_only_place_of_service_is_typed_inpatient(self, files):
        table = aggregate_rates(
            open_payer_dataset(files), PayerFilter(drop_untranslatable_codes=False)
        )
        rates = to_comparable_rates(table, files)
        # '21|31|32|33|34|51|54|55|56|61' names no outpatient place.
        assert "inpatient" in {r.setting for r in rates}

    def test_outpatient_only_place_of_service_is_typed_outpatient(self, files, table):
        rates = to_comparable_rates(table, files)
        assert "outpatient" in {r.setting for r in rates}

    def test_every_row_is_commercial_because_tic_exempts_the_rest(self, files, table):
        """CMS exempts Medicare, MA, Medicaid and Medicaid MCO from TiC."""
        rates = to_comparable_rates(table, files)
        assert {r.product_class for r in rates} == {"commercial"}

    def test_source_is_marked_payer(self, files, table):
        assert {r.source for r in to_comparable_rates(table, files)} == {"payer"}

    def test_leading_zero_codes_survive_as_strings(self, files):
        """Casting a billing code to an integer destroys the join."""
        table = aggregate_rates(
            open_payer_dataset(files), PayerFilter(drop_untranslatable_codes=False)
        )
        rates = to_comparable_rates(table, files)
        assert any(r.code.startswith("0") for r in rates)

    def test_network_is_carried_as_the_plan(self, files, table):
        rates = to_comparable_rates(table, files)
        assert "Hmo" in {r.plan for r in rates}

    def test_vintage_reaches_the_rate(self, files, table):
        rates = to_comparable_rates(table, files)
        assert {"2026-06-05", "2026-08-05", "2026-08-01"} >= {r.vintage for r in rates if r.vintage}


class TestPayerResolution:
    def test_both_aetna_entities_resolve_to_one_contracting_party(self, files, table):
        """``Aetna_NY`` and ``AetnaALIC_*`` are different legal entities, one payer."""
        rates = to_comparable_rates(table, files)
        by_plan = {r.plan: r.payer for r in rates}
        assert by_plan["Hmo"] == "Aetna"
        assert by_plan["NY"] == "Aetna"

    def test_cigna_resolves(self, files, table):
        rates = to_comparable_rates(table, files)
        assert "Cigna" in {r.payer for r in rates}

    def test_unresolved_label_keeps_its_raw_string_rather_than_being_dropped(self, files, table):
        rates = to_comparable_rates(table, files, canonicalise_payers=False)
        assert "Aetna_NY" in {r.payer for r in rates}


class TestFacilityExpansion:
    def test_without_a_map_the_rate_keeps_the_system_name(self, files, table):
        rates = to_comparable_rates(table, files)
        assert "NYU Langone" in {r.hospital for r in rates}

    def test_a_system_rate_fans_onto_each_facility(self, files, table):
        rates = to_comparable_rates(table, files, facilities=NYU_FACILITIES)
        nyu = [r for r in rates if r.location == "NYU Langone"]
        assert {r.hospital for r in nyu} == set(NYU_FACILITIES["NYU Langone"])

    def test_the_system_it_came_from_is_still_recoverable(self, files, table):
        rates = to_comparable_rates(table, files, facilities=NYU_FACILITIES)
        nyu = [r for r in rates if r.hospital == "NYU Langone|Brooklyn"]
        assert nyu and all(r.location == "NYU Langone" for r in nyu)

    def test_an_unmapped_system_is_not_dropped(self, files, table):
        rates = to_comparable_rates(table, files, facilities=NYU_FACILITIES)
        assert "Northwell" in {r.hospital for r in rates}


class TestComparabilityIntegration:
    """The loader's output must be refusable for the right reason.

    These are the pairs the comparability layer has to reject; if the loader
    typed a field wrongly they would be silently compared instead.
    """

    def _rates(self, files) -> list[ComparableRate]:
        table = aggregate_rates(
            open_payer_dataset(files), PayerFilter(drop_untranslatable_codes=False)
        )
        return to_comparable_rates(table, files)

    def test_a_percentage_rate_is_refused_against_a_dollar_rate(self, files):
        rates = self._rates(files)
        percent = next(r for r in rates if r.rate_kind == "percentage")
        # Same service, same setting, same code system -- the pair differs only
        # in the unit its number is denominated in, which is the whole point.
        hospital_dollar = percent.__class__(
            **{
                **percent.__dict__,
                "source": "hospital",
                "rate_kind": "dollar",
                "rate_dollar": 412.0,
                "methodology": "fee schedule",
                "product_class": "commercial",
            }
        )
        verdict = can_compare(percent, hospital_dollar, cross_source=True)
        assert not verdict
        assert verdict.reason == str(NotComparable.MIXED_RATE_KIND)

    def test_a_per_diem_is_refused_against_a_fee_schedule(self, files):
        rates = self._rates(files)
        per_diem = next(r for r in rates if r.methodology_family == "per_diem")
        counterpart = per_diem.__class__(
            **{**per_diem.__dict__, "source": "hospital", "methodology": "fee schedule"}
        )
        verdict = can_compare(per_diem, counterpart, cross_source=True)
        assert not verdict
        assert verdict.reason == str(NotComparable.INCOMPATIBLE_METHODOLOGY)

    def test_a_placeholder_zero_rate_is_refused(self, files):
        rates = self._rates(files)
        zero = next(r for r in rates if r.rate_dollar == 0.0)
        counterpart = zero.__class__(
            **{**zero.__dict__, "source": "hospital", "rate_dollar": 412.0}
        )
        verdict = can_compare(zero, counterpart, cross_source=True)
        assert not verdict
        assert verdict.reason == str(NotComparable.ZERO_RATE)

    def test_a_medicare_advantage_hospital_rate_has_no_payer_counterpart(self, files):
        """MA is exempt from TiC, so a variance against it would be a bug."""
        rates = self._rates(files)
        payer_rate = next(r for r in rates if r.rate_kind == "dollar" and r.rate_dollar)
        hospital_ma = payer_rate.__class__(
            **{
                **payer_rate.__dict__,
                "source": "hospital",
                "product_class": "medicare_advantage",
            }
        )
        verdict = can_compare(payer_rate, hospital_ma, cross_source=True)
        assert not verdict
        assert verdict.reason == str(NotComparable.TIC_EXEMPT_PRODUCT)


class TestTopLevel:
    def test_load_comparable_rates_reads_end_to_end(self):
        rates = load_comparable_rates(FIXTURES, facilities=NYU_FACILITIES)
        assert rates
        assert {r.source for r in rates} == {"payer"}

    def test_distinct_systems_counts_every_system_a_row_touches(self):
        counts = distinct_systems(FIXTURES)
        assert counts["NYU Langone"] > 0
        assert set(counts) <= {
            "Montefiore",
            "Mount Sinai",
            "NYP",
            "NYU Langone",
            "Northwell",
            "WMC",
            "White Plains",
        }

    def test_a_missing_directory_is_an_error_not_an_empty_result(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            discover_payer_files(tmp_path / "nope")
