import json

import pytest

from agents.entity_resolution import (
    CANONICAL_PAYERS,
    CallStats,
    LlmMatcher,
    MatchProposal,
    PayerCandidate,
    RuleBasedMatcher,
    candidates_from_pairs,
    canonical_key,
    resolve,
    validate_proposal,
)


def candidate(payer: str, plan: str = "All Commercial Plans", lines: int = 100) -> PayerCandidate:
    return PayerCandidate(payer_raw=payer, plan_raw=plan, rate_lines=lines)


class TestCanonicalKey:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("AETNA [2700]", "aetna"),
            ("HIGHMARK BLUE CROSS BLUE SHIELD [5143]", "highmark blue cross blue shield"),
            ("United Healthcare", "united healthcare"),
            ("Fidelis Care", "fidelis care"),
            ("POMCO INS CO [5157]", "pomco"),
        ],
    )
    def test_strips_codes_and_corporate_noise(self, given, expected):
        assert canonical_key(given) == expected

    def test_html_entities_are_handled(self):
        assert "essential" in canonical_key("Fidelis - Essential 1&amp;2")


class TestRuleBasedMatcher:
    matcher = RuleBasedMatcher()

    @pytest.mark.parametrize(
        ("payer", "expected"),
        [
            ("uhc", "UnitedHealthcare"),
            ("United Healthcare", "UnitedHealthcare"),
            ("Oxford", "UnitedHealthcare"),
            ("Aetna", "Aetna"),
            ("Empire", "Anthem / Empire BCBS"),
            ("BCBS", "Anthem / Empire BCBS"),
            ("Emblem", "EmblemHealth"),
            ("GHI", "EmblemHealth"),
            ("HIP", "EmblemHealth"),
            ("MetroPlus", "MetroPlus"),
            ("MultiPlan", "MultiPlan"),
        ],
    )
    def test_resolves_real_aliases(self, payer, expected):
        proposal = self.matcher.propose([candidate(payer)])[0]

        assert proposal.canonical_payer == expected
        assert proposal.confidence > 0

    def test_the_four_united_spellings_collapse_to_one_party(self):
        """The finding that motivates A2: one company, four strings."""
        names = ["uhc", "united healthcare", "united", "oxford"]

        resolved = {p.canonical_payer for p in self.matcher.propose([candidate(n) for n in names])}

        assert resolved == {"UnitedHealthcare"}

    def test_unknown_payer_abstains_rather_than_guessing(self):
        proposal = self.matcher.propose([candidate("Northwell Direct")])[0]

        assert proposal.canonical_payer is None
        assert proposal.confidence == 0.0

    def test_substring_fallback_has_lower_confidence(self):
        exact = self.matcher.propose([candidate("aetna")])[0]
        fuzzy = self.matcher.propose([candidate("Aetna Better Health of New York")])[0]

        assert exact.confidence == 1.0
        assert fuzzy.canonical_payer == "Aetna"
        assert fuzzy.confidence < exact.confidence


class TestValidation:
    def test_accepts_a_well_formed_proposal(self):
        c = candidate("Aetna")
        proposal = MatchProposal(c.key, "Aetna", 0.95, "llm")

        assert validate_proposal(proposal, c).accepted

    def test_rejects_a_canonical_name_outside_the_vocabulary(self):
        """The schema constrains shape, not vocabulary. Code constrains vocabulary."""
        c = candidate("Aetna")
        proposal = MatchProposal(c.key, "Aetna Health Inc of New York", 0.99, "llm")

        result = validate_proposal(proposal, c)
        assert not result.accepted
        assert "unknown canonical payer" in result.reason

    def test_rejects_a_match_with_no_lexical_overlap(self):
        """A confident hallucination is still a hallucination."""
        c = candidate("Healthfirst")
        proposal = MatchProposal(c.key, "Cigna", 0.99, "llm")

        result = validate_proposal(proposal, c)
        assert not result.accepted
        assert "lexical overlap" in result.reason

    def test_rejects_out_of_range_confidence(self):
        c = candidate("Aetna")

        assert not validate_proposal(MatchProposal(c.key, "Aetna", 1.5, "llm"), c).accepted

    def test_rejects_a_proposal_answering_a_different_candidate(self):
        c = candidate("Aetna")
        proposal = MatchProposal("Cigna || Commercial", "Cigna", 0.99, "llm")

        assert not validate_proposal(proposal, c).accepted

    def test_abstention_is_not_accepted_but_is_not_an_error(self):
        c = candidate("Something Unknown")

        assert not validate_proposal(MatchProposal(c.key, None, 0.0, "llm"), c).accepted


class TestResolveRouting:
    def test_confident_valid_matches_are_accepted(self):
        results = resolve([candidate("Aetna")], RuleBasedMatcher())

        assert results[0].routed_to == "accepted"

    def test_low_confidence_goes_to_review(self):
        results = resolve([candidate("Aetna Better Health of New York")], RuleBasedMatcher())

        assert results[0].proposal.canonical_payer == "Aetna"
        assert results[0].routed_to == "review", "0.75 is below the 0.80 threshold"

    def test_abstentions_go_to_review_not_to_the_floor(self):
        results = resolve([candidate("Northwell Direct")], RuleBasedMatcher())

        assert results[0].routed_to == "review"


class StubMessages:
    def __init__(self, payload, usage=None, raises=None) -> None:
        self.payload = payload
        self.usage = usage
        self.raises = raises
        self.calls = 0

    def create(self, **kwargs: object):
        self.calls += 1
        self.kwargs = kwargs
        if self.raises:
            raise self.raises
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        block = type("Block", (), {"type": "text", "text": text})()
        usage = type("Usage", (), self.usage or {"input_tokens": 900, "output_tokens": 120})()
        return type("Response", (), {"content": [block], "usage": usage})()


class StubClient:
    def __init__(self, payload, usage=None, raises=None) -> None:
        self.messages = StubMessages(payload, usage, raises)


class TestLlmMatcher:
    def test_parses_structured_output(self):
        c = candidate("Oxford")
        client = StubClient(
            {
                "matches": [
                    {
                        "key": c.key,
                        "canonical_payer": "UnitedHealthcare",
                        "confidence": 0.93,
                        "reasoning": "Oxford is a UnitedHealthcare company",
                    }
                ]
            }
        )
        matcher = LlmMatcher(client)

        proposal = matcher.propose([c])[0]

        assert proposal.canonical_payer == "UnitedHealthcare"
        assert proposal.confidence == pytest.approx(0.93)
        assert proposal.source == "llm"

    def test_requests_a_constrained_schema(self):
        client = StubClient({"matches": []})

        LlmMatcher(client).propose([candidate("Aetna")])

        kwargs = client.messages.kwargs
        assert kwargs["model"] == "claude-opus-5"
        assert kwargs["output_config"]["format"]["type"] == "json_schema"

    def test_records_cost_and_latency(self):
        client = StubClient({"matches": []}, usage={"input_tokens": 1_000_000, "output_tokens": 0})
        matcher = LlmMatcher(client)

        matcher.propose([candidate("Aetna")])

        assert matcher.stats.calls == 1
        assert matcher.stats.cost_usd == pytest.approx(5.00), "Opus 5 input is $5/MTok"
        assert matcher.stats.mean_latency_ms >= 0

    def test_batches_large_candidate_lists(self):
        client = StubClient({"matches": []})
        matcher = LlmMatcher(client, batch_size=10)

        matcher.propose([candidate(f"payer {i}") for i in range(25)])

        assert client.messages.calls == 3

    def test_api_failure_abstains_rather_than_raising(self):
        client = StubClient({}, raises=RuntimeError("503 overloaded"))
        matcher = LlmMatcher(client)

        proposals = matcher.propose([candidate("Aetna")])

        assert proposals[0].canonical_payer is None
        assert matcher.stats.errors == 1
        assert "RuntimeError" in proposals[0].reasoning

    def test_unparseable_response_abstains(self):
        matcher = LlmMatcher(StubClient("not json at all"))

        proposals = matcher.propose([candidate("Aetna")])

        assert proposals[0].canonical_payer is None
        assert matcher.stats.errors == 1

    def test_missing_key_in_response_abstains_for_that_candidate(self):
        client = StubClient(
            {
                "matches": [
                    {"key": "other", "canonical_payer": "Aetna", "confidence": 1.0, "reasoning": ""}
                ]
            }
        )

        proposals = LlmMatcher(client).propose([candidate("Aetna")])

        assert proposals[0].canonical_payer is None
        assert "absent" in proposals[0].reasoning


class TestCallStats:
    def test_cost_uses_opus_5_list_price(self):
        stats = CallStats()
        stats.record(input_tokens=200_000, output_tokens=40_000, elapsed_ms=1200)

        assert stats.cost_usd == pytest.approx(200_000 / 1e6 * 5 + 40_000 / 1e6 * 25)
        assert stats.mean_latency_ms == pytest.approx(1200)


def test_candidates_from_pairs_sorts_by_volume():
    pairs = {"Aetna || Commercial": 10, "Cigna || Commercial": 500}

    candidates = candidates_from_pairs(pairs)

    assert candidates[0].payer_raw == "Cigna"
    assert candidates[0].rate_lines == 500


def test_canonical_vocabulary_has_no_duplicate_aliases():
    """A duplicated alias would silently make resolution order-dependent."""
    seen: dict[str, str] = {}
    for canonical, aliases in CANONICAL_PAYERS.items():
        for alias in aliases:
            assert alias not in seen, f"{alias!r} maps to both {seen.get(alias)} and {canonical}"
            seen[alias] = canonical
