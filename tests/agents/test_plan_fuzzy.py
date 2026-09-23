"""The fuzzy plan pass, held to the strings the lake actually contains.

Every string here is copied from the curated hospital lake or a payer file's
network label, per the project's rule that parsers and matchers are tested on
real inputs. The tests that matter most are the ones that keep it from
matching: a product family is not a contract, a government product is never a
commercial network, and a rules verdict built on disagreeing tokens stands.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.plan_evals import PlanEvalSet, load_default, score_plan_matcher
from agents.plan_fuzzy import (
    EXACT,
    FAMILY,
    MATCH_THRESHOLD,
    fuzzy_resolve,
    is_government,
    resolve_as_rules,
)
from agents.plan_resolution import PlanVerdict

REPO = Path(__file__).resolve().parents[2]


class TestExactNetworks:
    @pytest.mark.parametrize(
        "plan",
        [
            "Empire Connection",
            "BCBS BLUE CONNECTION (ALL PLANS) 3777",
            "Connection SG",
            "BLUE CROSS CONNECTION/EXCHANGE",
        ],
    )
    def test_the_connection_network_is_read_from_both_spellings(self, plan):
        got = fuzzy_resolve(plan, "ConnectionEPO")

        assert got.verdict is PlanVerdict.MATCH
        assert got.confidence == EXACT
        assert got.rules.verdict is PlanVerdict.UNKNOWN, "the rules could not see it"

    def test_choice_plus_is_a_network_not_two_empty_words(self):
        """The rules drop 'choice' and 'plus' as uninformative, so ChoicePlus never matched."""
        got = fuzzy_resolve("Uhc Choice Plus - Msq", "ChoicePlus")

        assert got.verdict is PlanVerdict.MATCH

    def test_the_facility_suffix_is_not_part_of_the_plan(self):
        assert (
            fuzzy_resolve("Empire Connection - Msq", "ConnectionEPO").verdict is PlanVerdict.MATCH
        )

    def test_a_different_named_network_does_not_match(self):
        assert fuzzy_resolve("Empire Connection", "SmallGroupEPO").verdict is not PlanVerdict.MATCH


class TestWhatItMustNotMatch:
    def test_a_product_family_is_not_a_contract(self):
        """GHI names Emblem's product line; GHIHOS000001 is one contract in it."""
        got = fuzzy_resolve("GHI Access Network", "GHIHOS000001")

        assert got.verdict is PlanVerdict.UNKNOWN
        assert got.tier == "family"
        assert got.confidence == FAMILY < MATCH_THRESHOLD

    @pytest.mark.parametrize(
        "plan",
        [
            "HIP MEDICAID-ENHANCED CARE 1098",
            "HIP CHILD HEALTH PLUS 1096",
            "HIP Medicare PPO",
            "All NYS Essential Plans",
        ],
    )
    def test_a_government_product_is_never_a_commercial_network(self, plan):
        """The first prototype matched these at family level. TiC exempts them."""
        got = fuzzy_resolve(plan, "HIPHOS000091")

        assert got.verdict is PlanVerdict.UNKNOWN
        assert got.tier == "none"
        assert is_government(plan)

    def test_a_rules_no_match_is_never_overruled(self):
        """Disagreeing product tokens outrank a name.

        This case is also a known rules error: "Open Access Plus" is Cigna's OAP,
        but the rules read "open access" first, as Aetna's family. It is left to
        a reviewed change to resolve_plan, not quietly corrected here, because
        the mart calls the rules and a fix there moves results.
        """
        got = fuzzy_resolve("Cigna Open Access Plus", "NationalOAP")

        assert got.verdict is PlanVerdict.NO_MATCH
        assert got.tier == "rules"

    def test_a_rules_match_stands_at_full_confidence(self):
        got = fuzzy_resolve("Cigna PPO", "NationalPPO")

        assert got.verdict is PlanVerdict.MATCH
        assert got.confidence == 1.0

    def test_a_state_label_names_nothing_either_way(self):
        """Aetna's file is labelled 'NY'. No plan can match or be ruled out against it."""
        assert (
            fuzzy_resolve("Aetna Open Access Managed Choice", "NY").verdict is PlanVerdict.UNKNOWN
        )


class TestAgainstTheReviewedLabels:
    def test_it_does_not_lose_precision_on_the_reviewed_set(self):
        rules, _ = score_plan_matcher(load_default(REPO / "evals"))
        fuzzy, _ = score_plan_matcher(load_default(REPO / "evals"), "fuzzy", resolve_as_rules)

        assert fuzzy.false_positives == 0
        assert fuzzy.precision >= rules.precision
        assert fuzzy.true_positives == rules.true_positives

    def test_the_proposed_labels_are_marked_unreviewed(self):
        """Its new matches are outside the reviewed set, so their labels wait on a person."""
        path = REPO / "evals" / "plan_matching_proposed.jsonl"
        proposed = PlanEvalSet.load(path)

        assert proposed.labels, "the measurement wrote no proposals"
        assert proposed.reviewed_share == 0.0
        matches = [x for x in proposed.labels if x.expected == str(PlanVerdict.MATCH)]
        assert all("ConnectionEPO" in x.key for x in matches)

    def test_every_proposed_label_agrees_with_the_pass_that_proposed_it(self):
        """If this ever fails, the pass changed after the proposals were written."""
        for line in (
            (REPO / "evals" / "plan_matching_proposed.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ):
            row = json.loads(line)
            plan, _, network = row["key"].partition(" || ")
            assert str(fuzzy_resolve(plan, network).verdict) == row["expected"], row["key"]
