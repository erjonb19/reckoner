"""Reading an absent hospital billing class as ``facility``.

The refusal this relaxes was built for a real failure: an absent value treated
as *compatible with anything* cross-joined one hospital rate against both the
payer's professional and its institutional rate, and 96.4% of the pairs that
produced landed against professional -- the hospital's charge for a scan against
the radiologist's fee for reading it.

Assuming ``facility`` is a narrower claim than that shrug, and the tests that
matter are the ones proving it stays narrow: it meets institutional only, it
applies only to systems the data says publish no professional rate, and every
pair it touches says so.

Maimonides is the case that must keep refusing. It publishes 121,119
professional rows, so for it the assumption is not conservative, it is false.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds
import pytest

from reconcile.comparability import (
    ASSUMED_FACILITY_NOTE,
    ComparableRate,
    NotComparable,
    can_compare,
)
from reconcile.curated import facility_only_hospitals

IN_SCOPE = frozenset(
    {
        "Mount Sinai Health System",
        "Northwell Health",
        "NYU Langone Health",
        "NewYork-Presbyterian",
    }
)


def hospital(name: str, billing_class: str | None = None) -> ComparableRate:
    return ComparableRate(
        source="hospital",
        hospital=name,
        code="99213",
        code_type="CPT",
        payer="Aetna",
        product_class="commercial",
        billing_class=billing_class,
        rate_dollar=100.0,
        vintage="2026-04-01",
    )


def payer(billing_class: str, facility: str = "Mount Sinai Health System") -> ComparableRate:
    """A payer rate as it reaches comparability.

    Note the vocabulary: TiC says ``institutional``, but the payer loader maps it
    to ``facility`` on the way in (``_BILLING_CLASS_TO_HOSPITAL``), so by the time
    a rate is compared both sides speak the hospital template's words. Writing
    ``institutional`` here would test a rate shape that never occurs.
    """
    return ComparableRate(
        source="payer",
        hospital=facility,
        code="99213",
        code_type="CPT",
        payer="Aetna",
        product_class="commercial",
        billing_class=billing_class,
        rate_dollar=120.0,
        vintage="2026-04-01",
    )


class TestTheAssumptionStaysNarrow:
    @pytest.mark.parametrize("system", sorted(IN_SCOPE))
    def test_an_unstated_rate_meets_an_institutional_one(self, system):
        """All four in-scope systems, none of which publishes a professional rate.

        The payer side here is ``facility``, which is TiC's ``institutional``
        after the loader's mapping.
        """
        verdict = can_compare(
            hospital(system),
            payer("facility"),
            cross_source=True,
            assume_facility_when_unstated=IN_SCOPE,
        )

        assert verdict.ok
        assert verdict.assumptions == (ASSUMED_FACILITY_NOTE,)

    @pytest.mark.parametrize("system", sorted(IN_SCOPE))
    def test_an_unstated_rate_still_refuses_a_professional_one(self, system):
        """The cross-join this exists to prevent: a facility charge is not a fee."""
        verdict = can_compare(
            hospital(system),
            payer("professional"),
            cross_source=True,
            assume_facility_when_unstated=IN_SCOPE,
        )

        assert not verdict.ok
        assert verdict.reason == NotComparable.DIFFERENT_BILLING_CLASS

    def test_maimonides_is_refused_because_it_publishes_professional_rates(self):
        """For a system that says 'professional', the assumption is false, not cautious."""
        verdict = can_compare(
            hospital("Maimonides Medical Center"),
            payer("facility"),
            cross_source=True,
            assume_facility_when_unstated=IN_SCOPE,
        )

        assert not verdict.ok
        assert verdict.reason == NotComparable.BILLING_CLASS_UNSTATED

    def test_without_the_option_nothing_changes(self):
        verdict = can_compare(
            hospital("Mount Sinai Health System"), payer("facility"), cross_source=True
        )

        assert not verdict.ok
        assert verdict.reason == NotComparable.BILLING_CLASS_UNSTATED

    def test_a_stated_rate_is_never_assumed(self):
        """A system in the eligible set that did state its class is taken at its word."""
        verdict = can_compare(
            hospital("Mount Sinai Health System", "facility"),
            payer("facility"),
            cross_source=True,
            assume_facility_when_unstated=IN_SCOPE,
        )

        assert verdict.ok
        assert verdict.assumptions == (), "no assumption was needed, so none may be claimed"


class TestTheAssumptionIsVisible:
    def test_every_assumed_pair_carries_a_note(self):
        verdict = can_compare(
            hospital("NYU Langone Health"),
            payer("facility"),
            cross_source=True,
            assume_facility_when_unstated=IN_SCOPE,
        )

        assert "assumed facility" in verdict.assumptions[0]
        assert "publishes no professional rates" in verdict.assumptions[0]

    def test_the_note_reaches_the_variance_notes(self):
        from reconcile.variance import cross_source_variance

        # The variance join keys on the provider, so the payer rate must already
        # be attributed to the facility -- the same grain problem as #26.
        mart = cross_source_variance(
            [hospital("NYU Langone Health")],
            [payer("facility", facility="NYU Langone Health")],
            assume_facility_when_unstated=IN_SCOPE,
        )

        assert len(mart.rows) == 1
        assert ASSUMED_FACILITY_NOTE in mart.rows[0].notes


class TestEligibilityIsComputedFromData:
    def _lake(self, tmp_path: Path, rows: list[tuple[str, str, str]]) -> ds.Dataset:
        target = tmp_path / "curated" / "hospital_rates"
        target.mkdir(parents=True)
        import pyarrow.parquet as pq

        pq.write_table(
            pa.table(
                {
                    "hospital": pa.array([r[0] for r in rows], pa.string()),
                    "billing_class": pa.array([r[1] for r in rows], pa.string()),
                    "code_type": pa.array([r[2] for r in rows], pa.string()),
                }
            ),
            target / "part.parquet",
        )
        return ds.dataset(target, partitioning="hive")

    def test_a_system_with_no_professional_row_is_eligible(self, tmp_path):
        lake = self._lake(tmp_path, [("A", "facility", "CPT"), ("A", "", "CPT")])

        assert facility_only_hospitals(lake) == frozenset({"A"})

    def test_one_professional_row_disqualifies_a_system(self, tmp_path):
        """Maimonides has 121,119 of them; one is enough to make the claim false."""
        lake = self._lake(
            tmp_path, [("A", "facility", "CPT")] * 99 + [("A", "professional", "CPT")]
        )

        assert facility_only_hospitals(lake) == frozenset()

    def test_case_and_whitespace_do_not_hide_a_professional_row(self, tmp_path):
        """The lake really does carry both 'Facility' and 'facility'."""
        lake = self._lake(tmp_path, [("A", "  Professional  ", "CPT")])

        assert facility_only_hospitals(lake) == frozenset()

    def test_chargemaster_rows_are_not_evidence_either_way(self, tmp_path):
        """CDM carries no billing class and would make every system look eligible."""
        lake = self._lake(tmp_path, [("A", "professional", "CDM"), ("A", "facility", "CPT")])

        assert facility_only_hospitals(lake) == frozenset({"A"})
