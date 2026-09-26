"""The A1 runner and the Gemini provider, driven by stubs: no network, no key.

What must never be wrong: spend stays inside a budget, a daily cap stops the run
without counting unattempted findings as abstentions, a stopped run resumes where
it left off, the key never reaches a log, and a score is only recorded over every
label.
"""

from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agents.triage_agent import (
    KEY_FIELDS,
    AnthropicProvider,
    CallLog,
    GeminiProvider,
    RunStopped,
    TriageAgent,
    is_daily_cap,
    is_retryable,
)

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_a1_eval.py"
spec = importlib.util.spec_from_file_location("run_a1_eval", SCRIPT)
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

ANSWER = {
    "cause": "units_or_methodology",
    "confidence": 0.9,
    "evidence": ["ratio"],
    "reasoning": "stub",
}


class APIError(Exception):
    """Shaped like google.genai.errors.APIError: an int ``code`` and a message."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{code} {message}")
        self.code = code


PER_MINUTE = APIError(429, "Quota exceeded: GenerateRequestsPerMinutePerProjectPerModel-FreeTier")
PER_DAY = APIError(429, "Quota exceeded: GenerateRequestsPerDayPerProjectPerModel-FreeTier")


def gemini_response(body: dict[str, Any] | None = None, **usage: int) -> SimpleNamespace:
    counts = {"prompt_token_count": 900, "candidates_token_count": 120, "thoughts_token_count": 400}
    counts.update(usage)
    return SimpleNamespace(
        text=json.dumps(body or ANSWER),
        usage_metadata=SimpleNamespace(**counts),
        prompt_feedback=None,
        candidates=[SimpleNamespace(finish_reason="STOP")],
    )


class Gemini:
    """A google.genai.Client stand-in. Plays a script, then answers normally."""

    def __init__(self, script: list[object] | None = None) -> None:
        self.script = list(script or [])
        self.requests: list[dict[str, Any]] = []
        self.models = self

    def generate_content(self, **kwargs: Any) -> SimpleNamespace:  # noqa: ANN401
        self.requests.append(kwargs)
        step = self.script.pop(0) if self.script else gemini_response()
        if isinstance(step, Exception):
            raise step
        return step  # type: ignore[return-value]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def finding(code: str) -> dict[str, Any]:
    row = {name: f"{name}-value" for name in KEY_FIELDS}
    row.update({"code": code, "ratio": "0.12", "triage_rule": "systematic_offset"})
    return row


def gemini_agent(client: Gemini, log: CallLog | None = None, **kw: Any) -> TriageAgent:  # noqa: ANN401
    clock = Clock()
    provider = GeminiProvider(client, requests_per_minute=0, sleep=clock.sleep, clock=clock)
    return TriageAgent(provider, log=log or CallLog(), sleep=clock.sleep, clock=clock, **kw)


class TestTheGeminiProvider:
    def test_a_valid_answer_is_accepted_through_the_same_loop(self):
        agent = gemini_agent(Gemini())

        outcome = agent.triage_one(finding("1"))

        assert outcome.accepted
        assert outcome.proposal and outcome.proposal.cause == "units_or_methodology"

    def test_the_request_asks_for_json_against_the_schema(self):
        client = Gemini()
        gemini_agent(client).triage_one(finding("1"))

        (request,) = client.requests
        assert request["model"] == "gemini-2.5-flash"
        config = request["config"]
        assert config["response_mime_type"] == "application/json"
        assert config["response_json_schema"]["required"] == [
            "cause",
            "confidence",
            "evidence",
            "reasoning",
        ]
        assert "units_or_methodology" in config["system_instruction"]

    def test_thinking_tokens_are_logged_as_output_at_list_price(self):
        """The free tier bills $0; the log records what the paid tier would."""
        log = CallLog()
        gemini_agent(Gemini(), log).triage_one(finding("1"))

        (record,) = log.records
        assert (record.input_tokens, record.output_tokens) == (900, 520)
        assert record.cost_usd == pytest.approx(900 / 1e6 * 0.30 + 520 / 1e6 * 2.50)
        assert record.provider == "gemini"

    def test_calls_are_paced_for_the_free_tier(self):
        clock = Clock()
        provider = GeminiProvider(Gemini(), requests_per_minute=10, sleep=clock.sleep, clock=clock)
        agent = TriageAgent(provider, sleep=clock.sleep, clock=clock)

        agent.triage([finding("1"), finding("2"), finding("3")])

        assert clock.slept == [pytest.approx(6.0), pytest.approx(6.0)]

    def test_a_per_minute_429_is_retried_within_the_same_bound(self):
        client = Gemini([PER_MINUTE])
        log = CallLog()

        outcome = gemini_agent(client, log).triage_one(finding("1"))

        assert outcome.accepted and outcome.attempts == 2
        assert [r.outcome for r in log.records] == ["api_error", "ok"]

    def test_a_blocked_response_goes_to_a_human(self):
        blocked = gemini_response()
        blocked.prompt_feedback = SimpleNamespace(block_reason="SAFETY")

        outcome = gemini_agent(Gemini([blocked])).triage_one(finding("1"))

        assert outcome.routed == "human"
        assert "declined" in outcome.reason

    def test_truncated_json_is_retried_not_accepted(self):
        cut = gemini_response()
        cut.text = '{"cause": "units_or_meth'

        outcome = gemini_agent(Gemini([cut])).triage_one(finding("1"))

        assert outcome.accepted and outcome.attempts == 2


class TestTheDailyCap:
    def test_it_is_told_apart_from_a_per_minute_limit(self):
        assert is_daily_cap(PER_DAY)
        assert not is_daily_cap(PER_MINUTE)
        assert is_retryable(PER_MINUTE)

    def test_it_stops_the_run_instead_of_routing_the_finding(self):
        log = CallLog()

        with pytest.raises(RunStopped):
            gemini_agent(Gemini([PER_DAY]), log).triage_one(finding("1"))

        assert [r.outcome for r in log.records] == ["daily_cap"]


class TestResuming:
    def test_a_stopped_run_continues_where_it_left_off(self, tmp_path: Path):
        rows = [finding(str(i)) for i in range(5)]
        path = tmp_path / "outcomes.jsonl"

        first = runner.Resumable(gemini_agent(Gemini([gemini_response()] * 2 + [PER_DAY])), path)
        first.triage(rows)
        second_client = Gemini()
        second = runner.Resumable(gemini_agent(second_client), path)
        outcomes = second.triage(rows)

        assert (first.completed_this_run, first.complete()) == (2, False)
        assert "daily request cap" in first.stopped
        assert second.completed_this_run == 3
        assert len(second_client.requests) == 3, "nothing done twice"
        assert second.complete() and len(outcomes) == 5

    def test_the_call_log_survives_a_stop_and_totals_across_runs(self, tmp_path: Path):
        sink = tmp_path / "calls.jsonl"
        gemini_agent(Gemini(), CallLog.resume(sink)).triage_one(finding("1"))

        resumed = CallLog.resume(sink)
        gemini_agent(Gemini(), resumed).triage_one(finding("2"))

        assert resumed.calls == 2
        assert len(sink.read_text(encoding="utf-8").splitlines()) == 2


class TestTheKey:
    def test_it_is_scrubbed_from_an_error_that_echoes_it(self):
        secret = "AIzaFAKE-not-a-real-key"
        log = CallLog()
        error = APIError(400, f"API key not valid: {secret}")

        gemini_agent(Gemini([error]), log, redact=(secret,)).triage_one(finding("1"))

        assert secret not in json.dumps([r.detail for r in log.records])
        assert "[redacted]" in log.records[0].detail


class Anthropic:
    """The Anthropic stub the loop has always been tested with."""

    def __init__(self) -> None:
        self.calls = 0
        self.messages = self

    def create(self, **_: Any) -> SimpleNamespace:  # noqa: ANN401
        self.calls += 1
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(ANSWER))],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=800, output_tokens=150),
        )


class TestTheBudget:
    def test_spend_never_passes_it_and_the_run_stops_cleanly(self, tmp_path: Path):
        log = CallLog()
        stub = Anthropic()
        provider = runner.Budgeted(AnthropicProvider(stub), log, budget_usd=0.40)
        agent = runner.Resumable(TriageAgent(provider, log=log), tmp_path / "o.jsonl")

        agent.triage([finding(str(i)) for i in range(50)])

        assert (log.cost_usd or 0) <= 0.40
        assert "budget" in agent.stopped
        assert 0 < stub.calls < 50
        assert not agent.complete(), "unattempted findings are not abstentions"

    def test_an_unpriced_model_is_refused(self):
        with pytest.raises(SystemExit):
            runner.Budgeted(AnthropicProvider(Anthropic(), "unpriced"), CallLog(), 5)


def write_inputs(tmp_path: Path, n: int) -> tuple[Path, Path]:
    rows = [finding(str(i)) for i in range(n)]
    queue, labels = tmp_path / "queue.csv", tmp_path / "labels.csv"
    columns = [*KEY_FIELDS, "ratio", "triage_rule"]
    with queue.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    with labels.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*KEY_FIELDS, "expected_cause"])
        writer.writeheader()
        for row in rows:
            writer.writerow({**{k: row[k] for k in KEY_FIELDS}, "expected_cause": ANSWER["cause"]})
    return queue, labels


class TestTheCommand:
    def run(self, tmp_path: Path, *extra: str) -> int:
        queue, labels = write_inputs(tmp_path, 3)
        return runner.main(
            [
                "--queue",
                str(queue),
                "--labels",
                str(labels),
                "--runs",
                str(tmp_path / "runs"),
                "--results",
                str(tmp_path / "results.jsonl"),
                "--rpm",
                "0",
                *extra,
            ]
        )

    @pytest.fixture(autouse=True)
    def stubbed(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "AIzaFAKE")
        monkeypatch.setattr(
            runner,
            "provider_for",
            lambda name, model, key, rpm: GeminiProvider(
                Gemini(), model, requests_per_minute=0, sleep=lambda _: None
            ),
        )

    def test_it_scores_only_once_every_label_has_an_outcome(self, tmp_path: Path, capsys):
        assert self.run(tmp_path, "--max-requests", "2", "--record") == 0
        first = capsys.readouterr().out
        assert "this run: 2 findings completed" in first
        assert "not scored: 1 findings remain" in first
        assert not (tmp_path / "results.jsonl").exists()

        assert self.run(tmp_path, "--record") == 0
        second = capsys.readouterr().out
        assert "this run: 1 findings completed" in second
        records = [
            json.loads(line)
            for line in (tmp_path / "results.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        agent = records[-1]
        assert (agent["model"], agent["provider"]) == ("gemini-2.5-flash", "gemini")
        assert "free tier" in agent["billing"]
        assert agent["precision"] == 1.0 and agent["calls"] == 3

    def test_the_key_is_never_written(self, tmp_path: Path):
        self.run(tmp_path, "--record")

        for path in tmp_path.rglob("*"):
            if path.is_file():
                assert "AIzaFAKE" not in path.read_text(encoding="utf-8")

    def test_without_a_key_nothing_runs(self, monkeypatch, capsys):
        monkeypatch.delenv("GEMINI_API_KEY")

        assert runner.main([]) == 2
        assert "GEMINI_API_KEY is not set" in capsys.readouterr().err

    def test_anthropic_needs_a_budget(self, monkeypatch, capsys):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")

        assert runner.main(["--provider", "anthropic"]) == 2
        assert "--budget-usd" in capsys.readouterr().err
