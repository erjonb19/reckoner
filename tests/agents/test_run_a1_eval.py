"""The A1 eval runner's budget: the one thing about it that must never be wrong."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agents.triage_agent import KEY_FIELDS, CallLog, TriageAgent

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_a1_eval.py"
spec = importlib.util.spec_from_file_location("run_a1_eval", SCRIPT)
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class Stub:
    """Answers every finding the same way, at a fixed token count."""

    def __init__(self) -> None:
        self.calls = 0
        self.messages = self

    def create(self, **_: Any) -> SimpleNamespace:  # noqa: ANN401
        self.calls += 1
        body = {
            "cause": "units_or_methodology",
            "confidence": 0.9,
            "evidence": ["ratio"],
            "reasoning": "stub",
        }
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(body))],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=800, output_tokens=150),
        )


def finding(code: str) -> dict[str, Any]:
    row = {name: f"{name}-value" for name in KEY_FIELDS}
    row.update({"code": code, "ratio": "0.12"})
    return row


def test_spend_never_passes_the_budget():
    log = CallLog()
    stub = Stub()
    budgeted = runner.Budgeted(stub, log, budget_usd=0.10, model="claude-opus-5")
    agent = TriageAgent(budgeted, log=log, sleep=lambda _: None)

    outcomes = agent.triage([finding(str(i)) for i in range(50)])

    assert (log.cost_usd or 0) <= 0.10
    assert budgeted.stopped > 0
    stopped = [o for o in outcomes if not o.accepted]
    assert stopped and all("budget" in o.reason for o in stopped)
    assert stub.calls < 50


def test_an_unpriced_model_is_refused():
    with pytest.raises(SystemExit):
        runner.Budgeted(Stub(), CallLog(), budget_usd=5, model="some-unpriced-model")


def test_without_a_key_nothing_runs(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert runner.main(["--budget-usd", "5"]) == 2
    assert "nothing was run" in capsys.readouterr().err
