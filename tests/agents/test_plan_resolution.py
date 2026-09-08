"""Plan-level matcher tests, written from the strings the files actually carry.

Hospital plan names are taken verbatim from Mount Sinai's curated rows and payer
network labels from the parsed payer filenames, so a test passing here means the
matcher handles the real vocabulary rather than a tidied version of it.
"""

import pytest

from agents.plan_resolution import (
    PlanVerdict,
    network_tokens,
    resolve_plan,
)


class TestTokenising:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # Hospital spellings: punctuation-separated.
            ("Cigna Localplus - Msq", {"localplus"}),
            ("Cigna Ppo - Tmsh", {"ppo"}),
            ("Cigna Hmo/Oap - Msq", {"hmo", "oap"}),
            ("Aetna Hmo/Pos/Ppo - Bi", {"hmo", "pos", "ppo"}),
            # Payer spellings: camel case run together.
            ("LocalPlus", {"localplus"}),
            ("NationalPPO", {"ppo"}),
            ("NationalOAP", {"oap"}),
            ("SelectEPO", {"epo"}),
            ("POSChoicePlus", {"pos"}),
        ],
    )
    def test_real_strings_tokenise(self, text, expected):
        assert set(network_tokens(text)) == expected

    def test_a_qualifier_is_not_a_product(self):
        """``National`` narrows a PPO; it does not name a different network."""
        assert network_tokens("NationalPPO") == network_tokens("Ppo")

    def test_a_market_word_names_no_network(self):
        # "Oxford Commercial" says the market, not which of six networks.
        assert network_tokens("Oxford Commercial") == frozenset()
        assert network_tokens("United Healthcare - All Payer") == frozenset()

    def test_empty_input_is_empty(self):
        assert network_tokens(None) == frozenset()
        assert network_tokens("") == frozenset()


class TestCignaMatches:
    """Cigna is the carrier whose two vocabularies line up cleanly."""

    @pytest.mark.parametrize(
        ("plan", "network"),
        [
            ("Cigna Localplus - Msq", "LocalPlus"),
            ("Cigna Ppo - Tmsh", "NationalPPO"),
            ("Cigna Hmo - Bi", "Hmo"),
        ],
    )
    def test_same_network_matches(self, plan, network):
        match = resolve_plan(plan, network)

        assert match.verdict is PlanVerdict.MATCH
        assert match

    def test_different_networks_do_not_match(self):
        match = resolve_plan("Cigna Localplus - Msq", "NationalPPO")

        assert match.verdict is PlanVerdict.NO_MATCH
        assert not match

    def test_the_facility_suffix_is_ignored(self):
        """``Msq`` and ``Tmsh`` are facilities, not products."""
        assert resolve_plan("Cigna Ppo - Msq", "NationalPPO")
        assert resolve_plan("Cigna Ppo - Tmsh", "NationalPPO")


class TestAggregatePlans:
    """A hospital rate covering several networks cannot be one network's rate."""

    def test_a_plan_naming_three_products_is_an_aggregate(self):
        match = resolve_plan("Aetna Hmo/Pos/Ppo - Bi", "Ppo")

        assert match.verdict is PlanVerdict.AGGREGATE
        assert "3 networks" in match.reasoning

    def test_aggregate_beats_a_partial_match(self):
        """PPO is in there, but the published rate is not the PPO rate."""
        match = resolve_plan("Aetna Hmo/Ppo/Pos Commercial - Snch", "Ppo")

        assert match.verdict is PlanVerdict.AGGREGATE
        assert not match

    def test_two_products_is_already_an_aggregate(self):
        assert resolve_plan("Cigna Hmo/Oap - Msq", "NationalOAP").verdict is (PlanVerdict.AGGREGATE)


class TestUnknown:
    """Not matching is an admission, not a finding."""

    def test_an_employer_named_plan_is_unknown(self):
        # NYU names plans by who bought them; nothing in the payer data can match.
        match = resolve_plan("SCREEN ACTORS GUILD 1220", "NationalPPO")

        assert match.verdict is PlanVerdict.UNKNOWN

    def test_a_market_only_plan_is_unknown(self):
        assert resolve_plan("Oxford Commercial - Msq", "ChoicePlus").verdict is (
            PlanVerdict.UNKNOWN
        )

    def test_a_missing_side_is_unknown(self):
        assert resolve_plan(None, "LocalPlus").verdict is PlanVerdict.UNKNOWN
        assert resolve_plan("Cigna Ppo", None).verdict is PlanVerdict.UNKNOWN

    def test_the_verdict_carries_its_evidence(self):
        match = resolve_plan("Cigna Localplus - Msq", "NationalPPO")

        assert match.hospital_networks == {"localplus"}
        assert match.payer_networks == {"ppo"}
        assert match.reasoning
