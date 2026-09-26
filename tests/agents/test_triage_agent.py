"""A1's loop, driven by a stubbed model. No test here reaches a network.

The stub replays a script: each entry is either a response to return or an
exception to raise. That is enough to exercise every path the guardrails name
-- a validated answer, a retry after a rate limit, a retry after a rejection,
the bound on retries, a refusal, and the human queue at the end of all of them.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agents.triage_agent import (
    MAX_ATTEMPTS,
    CallLog,
    Cause,
    Proposal,
    RuleTriager,
    TriageAgent,
    cost_usd,
    is_retryable,
    item_key,
    validate,
    write_human_queue,
)


def finding(**overrides: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "system": "Mount Sinai",
        "facility": "Mount Sinai Queens",
        "carrier": "Cigna",
        "code": "G0422",
        "code_type": "HCPCS",
        "setting": "outpatient",
        "billing_class": "facility",
        "hospital_plan": "Cigna Localplus - Msq",
        "payer_plan": "LocalPlus",
        "hospital_rate": "1534.46",
        "payer_rate": "154.65",
        "ratio": "0.1008",
        "relative_difference": "8.92",
        "hospital_vintage": "2026-04-01",
        "payer_vintage": "2026-08-01",
        "vintage_gap_days": "122",
        "notes": "",
        "triage_rule": "unexplained",
    }
    row.update(overrides)
    return row


@dataclass
class Block:
    text: str
    type: str = "text"


@dataclass
class Usage:
    input_tokens: int = 1_000
    output_tokens: int = 200


@dataclass
class Response:
    content: list[Block]
    stop_reason: str = "end_turn"
    usage: Usage = field(default_factory=Usage)


def answer(cause: str, confidence: float = 0.9, evidence: tuple[str, ...] = ("ratio",)) -> Response:
    payload = {
        "cause": cause,
        "confidence": confidence,
        "evidence": list(evidence),
        "reasoning": "x",
    }
    return Response([Block(json.dumps(payload))])


class StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class APIConnectionError(Exception):
    pass


class Messages:
    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: object) -> Response:
        self.requests.append(kwargs)
        if not self.script:
            raise AssertionError("the agent called the model more times than scripted")
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class Stub:
    def __init__(self, *script: object) -> None:
        self.messages = Messages(list(script))


def agent(stub: Stub, **kwargs: Any) -> tuple[TriageAgent, list[float]]:  # noqa: ANN401
    slept: list[float] = []
    return TriageAgent(stub, sleep=slept.append, **kwargs), slept


class TestAValidatedAnswer:
    def test_it_is_accepted_after_one_call(self):
        stub = Stub(answer(Cause.UNITS_OR_METHODOLOGY))
        triager, _ = agent(stub)

        outcome = triager.triage_one(finding())

        assert outcome.accepted
        assert outcome.proposal is not None
        assert outcome.proposal.cause == Cause.UNITS_OR_METHODOLOGY
        assert outcome.attempts == 1
        assert len(stub.messages.requests) == 1

    def test_every_call_is_costed_and_timed(self):
        """Guardrail 4. 1,000 in and 200 out on Opus 5 is $0.005 + $0.005."""
        triager, _ = agent(Stub(answer(Cause.UNITS_OR_METHODOLOGY)))

        triager.triage_one(finding())

        record = triager.log.records[0]
        assert record.outcome == "ok"
        assert (record.input_tokens, record.output_tokens) == (1_000, 200)
        assert record.cost_usd == pytest.approx(0.010)
        assert record.latency_ms >= 0
        assert triager.log.summary()["cost_usd"] == pytest.approx(0.010)

    def test_the_request_is_schema_constrained(self):
        stub = Stub(answer(Cause.UNITS_OR_METHODOLOGY))
        triager, _ = agent(stub)

        triager.triage_one(finding())

        request = stub.messages.requests[0]
        assert request["output_config"]["format"]["type"] == "json_schema"
        assert request["model"] == "claude-opus-5"

    def test_the_rules_verdict_is_not_shown_to_the_model(self):
        """An agent shown the baseline's answer is scored on agreeing with it."""
        stub = Stub(answer(Cause.UNITS_OR_METHODOLOGY))
        triager, _ = agent(stub)

        triager.triage_one(finding(triage_rule="systematic_offset"))

        prompt = stub.messages.requests[0]["messages"][0]["content"]
        assert "triage_rule" not in prompt
        assert "systematic_offset" not in prompt


class TestRetries:
    def test_a_rate_limit_is_retried_with_backoff(self):
        stub = Stub(StatusError(429), answer(Cause.UNITS_OR_METHODOLOGY))
        triager, slept = agent(stub, backoff_seconds=2.0)

        outcome = triager.triage_one(finding())

        assert outcome.accepted
        assert outcome.attempts == 2
        assert slept == [2.0]
        assert [r.outcome for r in triager.log.records] == ["api_error", "ok"]

    def test_backoff_doubles(self):
        stub = Stub(StatusError(503), APIConnectionError("reset"), answer(Cause.DATA_ERROR))
        triager, slept = agent(stub, backoff_seconds=1.0)

        triager.triage_one(finding())

        assert slept == [1.0, 2.0]

    def test_retries_are_bounded_and_an_outage_is_not_a_judgement(self):
        """The bound is the point: a failing finding cannot loop, or bill, forever.
        And a finding the model never saw is an error, not a human's to review."""
        stub = Stub(*[StatusError(529)] * MAX_ATTEMPTS)
        triager, slept = agent(stub)

        outcome = triager.triage_one(finding())

        assert outcome.errored
        assert outcome.reason.startswith("unavailable")
        assert len(stub.messages.requests) == MAX_ATTEMPTS
        assert len(slept) == MAX_ATTEMPTS - 1, "no sleep after the last attempt"
        assert triager.log.calls == MAX_ATTEMPTS

    def test_a_bad_request_is_not_retried(self):
        """A 400 fails identically every time; retrying only spends. And it is
        an error, not a human's to review: the model never saw the finding."""
        stub = Stub(StatusError(400))
        triager, slept = agent(stub)

        outcome = triager.triage_one(finding())

        assert outcome.routed == "error" and outcome.errored
        assert outcome.reason.startswith("fatal API error")
        assert len(stub.messages.requests) == 1
        assert slept == []

    def test_a_refusal_goes_straight_to_a_human(self):
        refused = Response([], stop_reason="refusal")
        stub = Stub(refused)
        triager, _ = agent(stub)

        outcome = triager.triage_one(finding())

        assert outcome.routed == "human"
        assert len(stub.messages.requests) == 1
        assert triager.log.records[0].outcome == "refusal"

    def test_an_unparseable_answer_is_retried(self):
        stub = Stub(Response([Block("not json")]), answer(Cause.UNITS_OR_METHODOLOGY))
        triager, _ = agent(stub)

        outcome = triager.triage_one(finding())

        assert outcome.accepted
        assert [r.outcome for r in triager.log.records] == ["unparseable", "ok"]

    def test_a_failed_call_is_still_logged(self):
        """A failed call is billed too; a log of successes understates spend."""
        triager, _ = agent(Stub(StatusError(500), answer(Cause.DATA_ERROR)))

        triager.triage_one(finding())

        assert triager.log.calls == 2


class TestValidationDrivesTheLoop:
    def test_a_rejected_answer_is_retried_with_the_reason(self):
        """The model is told what the check found, and gets another attempt."""
        stub = Stub(
            answer(Cause.VINTAGE_ARTIFACT, evidence=("vintage_gap_days",)),
            answer(Cause.DATA_ERROR),
        )
        triager, _ = agent(stub)

        outcome = triager.triage_one(finding(vintage_gap_days="5"))

        assert outcome.accepted
        assert outcome.proposal is not None
        assert outcome.proposal.cause == Cause.DATA_ERROR
        retry_prompt = stub.messages.requests[1]["messages"][0]["content"]
        assert "rejected by a deterministic check" in retry_prompt
        assert "5-day gap" in retry_prompt

    def test_an_answer_that_never_validates_ends_with_a_human(self):
        wrong = answer(Cause.VINTAGE_ARTIFACT, evidence=("vintage_gap_days",))
        stub = Stub(*[wrong] * MAX_ATTEMPTS)
        triager, _ = agent(stub)

        outcome = triager.triage_one(finding(vintage_gap_days="5"))

        assert outcome.routed == "human"
        assert "rejected" in outcome.reason
        assert outcome.proposal is not None, "the last proposal is kept for the reviewer"

    def test_low_confidence_is_routed_to_a_human(self):
        triager, _ = agent(Stub(answer(Cause.DATA_ERROR, confidence=0.55)))

        outcome = triager.triage_one(finding())

        assert outcome.routed == "human"
        assert "low confidence" in outcome.reason


class TestTheGate:
    def proposal(self, cause: str, **kwargs: object) -> Proposal:
        defaults: dict[str, Any] = {"confidence": 0.9, "evidence": ("ratio",)}
        defaults.update(kwargs)
        return Proposal(item_key(finding()), cause, **defaults)

    def test_an_invented_cause_is_rejected(self):
        """The schema says enum; only this says it and means it."""
        verdict = validate(self.proposal("it is complicated"), finding())

        assert not verdict.accepted
        assert "unknown cause" in verdict.reason

    def test_evidence_must_be_fields_the_finding_has(self):
        verdict = validate(self.proposal(Cause.DATA_ERROR, evidence=("claims_history",)), finding())

        assert not verdict.accepted
        assert "claims_history" in verdict.reason

    def test_a_cause_with_no_evidence_is_a_guess(self):
        verdict = validate(self.proposal(Cause.DATA_ERROR, evidence=()), finding())

        assert not verdict.accepted

    def test_an_abstention_needs_no_evidence(self):
        verdict = validate(self.proposal(Cause.INSUFFICIENT_EVIDENCE, evidence=()), finding())

        assert verdict.accepted

    def test_a_vintage_artifact_needs_a_known_gap(self):
        verdict = validate(self.proposal(Cause.VINTAGE_ARTIFACT), finding(vintage_gap_days=""))

        assert not verdict.accepted

    def test_a_units_mismatch_needs_a_large_ratio(self):
        verdict = validate(self.proposal(Cause.UNITS_OR_METHODOLOGY), finding(ratio="1.4"))

        assert not verdict.accepted
        assert "1.40" in verdict.reason

    def test_a_units_mismatch_holds_in_either_direction(self):
        """0.1x and 10x are the same size of disagreement."""
        assert validate(self.proposal(Cause.UNITS_OR_METHODOLOGY), finding(ratio="0.1")).accepted
        assert validate(self.proposal(Cause.UNITS_OR_METHODOLOGY), finding(ratio="10")).accepted

    def test_a_plan_mismatch_needs_a_plan(self):
        row = finding(hospital_plan="", payer_plan="")
        proposal = Proposal(item_key(row), Cause.PLAN_MISMATCH, 0.9, ("hospital_plan",))

        assert not validate(proposal, row).accepted

    def test_a_proposal_for_another_finding_is_rejected(self):
        proposal = Proposal("someone else", Cause.DATA_ERROR, 0.9, ("ratio",))

        assert not validate(proposal, finding()).accepted


class TestClassifyingFailures:
    @pytest.mark.parametrize("status", [408, 409, 429, 500, 503, 529])
    def test_these_are_retried(self, status):
        assert is_retryable(StatusError(status))

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 413])
    def test_these_are_not(self, status):
        assert not is_retryable(StatusError(status))

    def test_a_dropped_connection_is_retried(self):
        assert is_retryable(APIConnectionError("reset"))

    def test_an_arbitrary_bug_is_not(self):
        assert not is_retryable(KeyError("x"))


class TestCost:
    def test_opus_5_list_price(self):
        assert cost_usd("claude-opus-5", 1_000_000, 1_000_000) == pytest.approx(30.0)

    def test_an_unpriced_model_is_unknown_not_free(self):
        assert cost_usd("some-future-model", 1_000, 1_000) is None

    def test_one_unknown_cost_makes_the_total_unknown(self):
        triager, _ = agent(Stub(answer(Cause.DATA_ERROR)), model="some-future-model")

        triager.triage_one(finding())

        assert triager.log.cost_usd is None

    def test_the_log_is_appended_never_overwritten(self, tmp_path: Path):
        path = tmp_path / "calls.jsonl"
        for _ in range(2):
            triager, _ = agent(Stub(answer(Cause.DATA_ERROR)))
            triager.triage_one(finding())
            triager.log.append_to(path)

        assert len(path.read_text(encoding="utf-8").splitlines()) == 2


class TestTheHumanQueue:
    def test_it_holds_only_what_was_not_accepted_and_says_why(self, tmp_path: Path):
        rows = [finding(), finding(code="P2038")]
        triager, _ = agent(Stub(answer(Cause.DATA_ERROR), answer(Cause.DATA_ERROR, confidence=0.4)))

        outcomes = triager.triage(rows)
        written = write_human_queue(outcomes, rows, tmp_path / "queue.csv")

        assert written == 1
        with (tmp_path / "queue.csv").open(encoding="utf-8") as handle:
            queued = list(csv.DictReader(handle))
        assert queued[0]["code"] == "P2038"
        assert queued[0]["routed_because"].startswith("low confidence")
        assert queued[0]["proposed_cause"] == Cause.DATA_ERROR


class TestTheBaseline:
    def test_each_rule_answers_in_the_agents_vocabulary(self):
        outcomes = RuleTriager().triage(
            [finding(triage_rule="vintage_artifact"), finding(code="X", triage_rule="implausible")]
        )

        causes = [o.proposal.cause for o in outcomes if o.proposal]
        assert causes == [Cause.VINTAGE_ARTIFACT, Cause.UNITS_OR_METHODOLOGY]

    def test_unexplained_is_the_rules_abstention(self):
        outcome = RuleTriager().triage([finding(triage_rule="unexplained")])[0]

        assert outcome.proposal is not None
        assert outcome.proposal.abstains


class TestConstruction:
    def test_zero_attempts_is_refused(self):
        with pytest.raises(ValueError):
            TriageAgent(Stub(), max_attempts=0)

    def test_a_fresh_log_per_agent(self):
        assert TriageAgent(Stub()).log is not TriageAgent(Stub()).log
        assert isinstance(TriageAgent(Stub()).log, CallLog)
