import pytest

from reconcile.provenance import Provenance, merge, parse_vintage


class TestVintageParsing:
    @pytest.mark.parametrize("given", ["2026-04-01", "1/1/2026", "7/1/2026", "2026-03", "2026"])
    def test_real_published_formats(self, given):
        """These are the formats actually seen across the corpus."""
        assert parse_vintage(given) is not None

    def test_unparseable_returns_none_rather_than_guessing(self):
        assert parse_vintage("last updated recently") is None
        assert parse_vintage(None) is None


class TestCaveats:
    def test_wide_vintage_span_is_flagged_automatically(self):
        """The structural hazard: a variance may be a timing artifact."""
        p = Provenance(rows=500, hospitals=8)
        p.add_source("hospital MRFs", "2026-04-01")
        p.add_source("SPARCS", "2021")

        assert not p.is_comparable
        assert any("timing artifact" in c for c in p.caveats)
        assert not p.is_reportable

    def test_narrow_span_is_comparable(self):
        p = Provenance(rows=500, hospitals=8)
        p.add_source("MRFs", "2025-09-05")
        p.add_source("rates", "2026-04-01")

        assert p.is_comparable
        assert p.is_reportable

    def test_thin_evidence_is_flagged(self):
        p = Provenance(rows=12, hospitals=3)

        assert any("not a stable estimate" in c for c in p.caveats)
        assert not p.is_reportable

    def test_heavy_exclusion_is_flagged(self):
        p = Provenance(rows=100, hospitals=5)
        p.exclude("not dollar denominated", 900)

        assert p.included_share == pytest.approx(0.1)
        assert any("survived filtering" in c for c in p.caveats)

    def test_single_hospital_is_not_a_market_view(self):
        p = Provenance(rows=500, hospitals=1)

        assert any("single hospital" in c for c in p.caveats)

    def test_low_coverage_is_flagged(self):
        p = Provenance(rows=500, hospitals=6, coverage=0.05)

        assert any("5% of the hospital's book" in c for c in p.caveats)

    def test_a_clean_figure_carries_no_caveats(self):
        p = Provenance(rows=5000, hospitals=9, coverage=0.8)
        p.add_source("MRFs", "2026-04-01")

        assert p.caveats == []
        assert p.is_reportable


def test_merge_combines_envelopes():
    a = Provenance(rows=100, hospitals=3, coverage=0.6)
    a.exclude("no rate", 5)
    a.add_source("A", "2026-01-01")
    b = Provenance(rows=50, hospitals=5, coverage=0.2)
    b.exclude("no rate", 2)
    b.add_source("B", "2026-02-01")

    merged = merge(a, b)

    assert merged.rows == 150
    assert merged.hospitals == 5
    assert merged.excluded["no rate"] == 7
    assert merged.coverage == pytest.approx(0.2), "the weakest coverage governs"
    assert len(merged.sources) == 2
