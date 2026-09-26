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
from collections.abc import Iterator
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
    item_key,
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
        monkeypatch.setattr(runner, "gemini_client", lambda key: SimpleNamespace())
        monkeypatch.setattr(runner, "gemini_models", lambda client: ["gemini-2.5-flash"])
        monkeypatch.setattr(
            runner,
            "provider_for",
            lambda name, model, key, rpm, client=None: GeminiProvider(
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


GONE = APIError(
    404,
    "NOT_FOUND. This model models/gemini-2.5-flash is no longer available to new users.",
)


class TestFatalErrorsAreNotAbstentions:
    """Silent failure #15: 250 fatal 404s were scored as 250 abstentions."""

    def test_a_fatal_call_is_an_error_not_a_human_route(self):
        outcome = gemini_agent(Gemini([GONE])).triage_one(finding("1"))

        assert outcome.errored and not outcome.accepted
        assert "no longer available" in outcome.reason

    def test_errored_outcomes_leave_no_score_and_no_record(self, tmp_path: Path):
        from agents.triage_evals import TriageLabel, append_result, gate, score

        rows = [finding(str(i)) for i in range(3)]
        labels = [TriageLabel(item_key(r), "units_or_methodology") for r in rows]
        agent = gemini_agent(Gemini([GONE] * 3))

        result = score(agent, rows, labels)

        assert result.status == "errored" and result.errors == 3
        assert result.precision is None and result.coverage is None
        assert result.abstained == 0 and result.routed_to_human == 0
        assert gate(result)[0] is False
        assert "no longer available" in result.summary()
        with pytest.raises(ValueError, match="errored"):
            append_result(result, tmp_path / "results.jsonl")

    def test_the_run_stops_after_a_few_and_keeps_nothing(self, tmp_path: Path):
        client = Gemini([GONE] * 50)
        path = tmp_path / "outcomes.jsonl"
        agent = runner.Resumable(gemini_agent(client), path)

        agent.triage([finding(str(i)) for i in range(50)])

        assert len(client.requests) == runner.MAX_FATAL
        assert "fatal API errors" in agent.stopped
        assert not path.exists(), "failed findings are retried next run, not remembered"

    def test_the_human_queue_holds_no_errors(self, tmp_path: Path):
        from agents.triage_agent import Outcome, write_human_queue

        rows = [finding("1"), finding("2")]
        outcomes = [
            Outcome(item_key(rows[0]), "error", "fatal API error: 404", 1),
            Outcome(item_key(rows[1]), "human", "low confidence (0.50)", 1),
        ]

        assert write_human_queue(outcomes, rows, tmp_path / "h.csv") == 1


class TestTheCommandOnAFatalError:
    def test_it_prints_the_error_exits_nonzero_and_records_nothing(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        queue, labels = write_inputs(tmp_path, 10)
        monkeypatch.setenv("GEMINI_API_KEY", "AIzaFAKE")
        monkeypatch.setattr(runner, "gemini_client", lambda key: SimpleNamespace())
        monkeypatch.setattr(runner, "gemini_models", lambda client: ["gemini-2.5-flash"])
        monkeypatch.setattr(
            runner,
            "provider_for",
            lambda name, model, key, rpm, client=None: GeminiProvider(
                Gemini([GONE] * 10), model, requests_per_minute=0, sleep=lambda _: None
            ),
        )
        results = tmp_path / "results.jsonl"

        code = runner.main(
            [
                "--queue",
                str(queue),
                "--labels",
                str(labels),
                "--runs",
                str(tmp_path / "runs"),
                "--results",
                str(results),
                "--rpm",
                "0",
                "--record",
            ]
        )

        captured = capsys.readouterr()
        assert code == 1
        assert "no longer available to new users" in captured.err
        assert "not scored" in captured.out
        assert not results.exists()


class TestModelSettings:
    def test_a_later_model_is_not_sent_the_2_5_thinking_budget(self):
        client = Gemini()
        clock = Clock()
        provider = GeminiProvider(
            client, "gemini-3.8-flash", requests_per_minute=0, sleep=clock.sleep, clock=clock
        )
        TriageAgent(provider, sleep=clock.sleep, clock=clock).triage_one(finding("1"))

        assert "thinking_config" not in client.requests[0]["config"]

    def test_2_5_keeps_its_budget(self):
        client = Gemini()
        gemini_agent(client).triage_one(finding("1"))

        assert client.requests[0]["config"]["thinking_config"] == {"thinking_budget": 1024}


OVERLOADED = APIError(503, "UNAVAILABLE. This model is currently experiencing high demand.")


class TestAnOutage:
    """Three 503s on one finding once went to the human queue as a judgement."""

    def test_a_finding_the_model_never_saw_is_an_error(self):
        outcome = gemini_agent(Gemini([OVERLOADED] * 3)).triage_one(finding("1"))

        assert outcome.errored
        assert outcome.reason.startswith("unavailable")

    def test_one_answer_among_the_failures_is_still_an_answer(self):
        outcome = gemini_agent(Gemini([OVERLOADED, OVERLOADED])).triage_one(finding("1"))

        assert outcome.accepted and outcome.attempts == 3

    def test_an_unparseable_reply_counts_as_answered(self):
        cut = gemini_response()
        cut.text = "{"

        outcome = gemini_agent(Gemini([cut, cut, cut])).triage_one(finding("1"))

        assert outcome.routed == "human", "the model answered, badly: a person should look"


class TestTheSample:
    def queue(self) -> list[dict[str, Any]]:
        rows = []
        for i in range(250):
            row = finding(str(i))
            row["triage_rule"] = (
                "unexplained" if i < 29 else "systematic_offset" if i < 175 else "vintage_artifact"
            )
            row["system"] = ["A", "B", "C"][i % 3]
            rows.append(row)
        return rows

    def test_it_holds_every_unexplained_finding_and_about_sixty(self):
        from agents.triage_evals import sample_queue

        chosen = sample_queue(self.queue())

        assert len(chosen) == 60
        assert sum(r["triage_rule"] == "unexplained" for r in chosen) == 29

    def test_the_rest_is_spread_in_proportion(self):
        from agents.triage_evals import sample_queue

        rules = [r["triage_rule"] for r in sample_queue(self.queue())]

        # 146 offset and 75 vintage share 31 places: about 20 and 11.
        assert abs(rules.count("systematic_offset") - 20) <= 1
        assert abs(rules.count("vintage_artifact") - 11) <= 1

    def test_the_same_seed_gives_the_same_sample_in_any_order(self):
        from agents.triage_evals import sample_queue

        rows = self.queue()
        first = [item_key(r) for r in sample_queue(rows)]
        again = [item_key(r) for r in sample_queue(list(reversed(rows)))]

        assert first == again
        assert [item_key(r) for r in sample_queue(rows, seed=1)] != first

    def test_it_never_reads_the_labels(self):
        """Only the queue is an argument; there is nothing else to read."""
        import inspect

        from agents.triage_evals import sample_queue

        assert list(inspect.signature(sample_queue).parameters) == ["rows", "size", "seed"]


class TestTheCommandWithASample(TestTheCommand):
    def test_the_score_says_it_is_a_sample_and_how_big(self, tmp_path: Path, capsys):
        queue, labels = write_inputs(tmp_path, 12)
        results = tmp_path / "results.jsonl"

        code = runner.main(
            [
                "--queue",
                str(queue),
                "--labels",
                str(labels),
                "--runs",
                str(tmp_path / "runs"),
                "--results",
                str(results),
                "--rpm",
                "0",
                "--sample",
                "5",
                "--record",
            ]
        )

        out = capsys.readouterr().out
        assert code == 0
        assert "[SAMPLE: 5 of 12 findings" in out
        record = json.loads(results.read_text(encoding="utf-8").splitlines()[-1])
        assert record["scored"] == 5 and record["sample"].startswith("5 of 12")


class TestTheModelCheck:
    def test_a_model_the_key_cannot_use_stops_before_any_call(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("GEMINI_API_KEY", "AIzaFAKE")
        monkeypatch.setattr(runner, "gemini_client", lambda key: SimpleNamespace())
        monkeypatch.setattr(runner, "gemini_models", lambda client: ["gemini-3.5-flash-lite"])
        monkeypatch.setattr(runner, "provider_for", lambda *a: pytest.fail("no call"))

        assert runner.main(["--model", "gemini-2.5-flash"]) == 2
        assert "Available: gemini-3.5-flash-lite" in capsys.readouterr().err

    def test_list_models_prints_and_stops(self, monkeypatch, capsys):
        monkeypatch.setenv("GEMINI_API_KEY", "AIzaFAKE")
        monkeypatch.setattr(runner, "gemini_client", lambda key: SimpleNamespace())
        monkeypatch.setattr(runner, "gemini_models", lambda client: ["a-model", "b-model"])

        assert runner.main(["--list-models"]) == 0
        assert capsys.readouterr().out.split() == ["a-model", "b-model"]


class Connection:
    """The HTTP connection a google-genai client owns and closes."""

    def __init__(self) -> None:
        self.closed = False
        self.generated = 0

    def check(self) -> None:
        if self.closed:
            raise RuntimeError("Cannot send a request, as the client has been closed.")


class Models:
    """Like the SDK's: holds the connection, not the client."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def list(self) -> Iterator[SimpleNamespace]:
        """A lazy pager, as the SDK returns: nothing is sent until iterated."""

        def pager() -> Iterator[SimpleNamespace]:
            self.connection.check()
            yield SimpleNamespace(
                name="models/gemini-3.5-flash-lite", supported_actions=["generateContent"]
            )
            yield SimpleNamespace(name="models/text-embedding", supported_actions=["embedContent"])

        return pager()

    def generate_content(self, **_: Any) -> SimpleNamespace:  # noqa: ANN401
        self.connection.check()
        self.connection.generated += 1
        return gemini_response()


class SdkLikeClient:
    """Closes its connection when nothing references it, as Client.__del__ does."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.models = Models(connection)

    def __del__(self) -> None:
        self.connection.closed = True


class TestOneClientForTheWholeRun:
    """The run once failed before any finding: 'the client has been closed'."""

    def test_the_fake_reproduces_the_bug(self):
        """Without this, the test below could pass against a fake that never closes."""
        connection = Connection()

        with pytest.raises(RuntimeError, match="has been closed"):
            list(SdkLikeClient(connection).models.list())

    def test_the_model_check_then_calls_on_the_same_client(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        connection = Connection()
        made: list[SdkLikeClient] = []

        def client(key: str) -> SdkLikeClient:
            made.append(SdkLikeClient(connection))
            return made[-1]

        monkeypatch.setenv("GEMINI_API_KEY", "AIzaFAKE")
        monkeypatch.setattr(runner, "gemini_client", client)
        made.clear()
        queue, labels = write_inputs(tmp_path, 3)

        code = runner.main(
            [
                "--model",
                "gemini-3.5-flash-lite",
                "--queue",
                str(queue),
                "--labels",
                str(labels),
                "--runs",
                str(tmp_path / "runs"),
                "--results",
                str(tmp_path / "r.jsonl"),
                "--rpm",
                "0",
                "--list-price",
                "0.1",
                "0.4",
            ]
        )

        err = capsys.readouterr().err
        assert "could not list models" not in err
        assert code == 0
        assert len(made) == 1, "one client, for the listing and every call"
        assert connection.generated == 3
