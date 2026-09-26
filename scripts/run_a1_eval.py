"""Run A1's agent over the labelled queue, resumably, and score it beside the rules.

Deliberately a script and not a flag on ``agents.triage_evals``: every finding it
sees is a model call, so running it is a decision someone makes, not an option
someone passes by accident (``docs/labelling-a1.md``).

It changes nothing about the agent. The prompt, the validator, the retry bound
and the 0.80 review threshold are the ones the code already had before a single
label existed; the point of the run is to measure them, not to fit them.

**Providers.** ``--provider gemini`` (the default) calls Gemini 2.5 Flash, key
from ``GEMINI_API_KEY`` only, paced for the free tier. ``--provider anthropic``
calls Claude, key from ``ANTHROPIC_API_KEY``, and requires ``--budget-usd``.
Either way every call's tokens and a list-price-equivalent cost are logged, so a
free-tier run is comparable to a paid one.

**Resumable.** Each finding's outcome is written to the run directory the moment
it is decided, and every call is logged as it is made. A daily request cap, a
spent budget or ``--max-requests`` stops the run cleanly between findings; the
next invocation skips what is done and continues. The score is computed and
recorded only when every labelled finding has an outcome: a score over the
findings that happened to come first is not a score over the labels.

    python scripts/run_a1_eval.py --provider gemini --record
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agents.triage_agent import (
    DEFAULT_MODEL,
    PRICES_PER_MTOK,
    AnthropicProvider,
    CallLog,
    Completion,
    GeminiProvider,
    Outcome,
    Provider,
    RuleTriager,
    RunStopped,
    TriageAgent,
    cost_usd,
    item_key,
    write_human_queue,
)
from agents.triage_evals import (
    LABELS,
    RESULTS,
    SAMPLE_SEED,
    append_result,
    gate,
    load_labels,
    load_queue,
    sample_queue,
    score,
)

MODELS = {"gemini": "gemini-2.5-flash", "anthropic": DEFAULT_MODEL}
KEYS = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
BILLING = {"gemini": "free tier ($0 billed; cost shown at paid list price)", "anthropic": "paid"}

#: Fatal API errors this run before it stops. A wrong model name or a rejected
#: key fails every call identically; three in a row is proof enough, and 250 was
#: once reported as a score (silent failure #15).
MAX_FATAL = 3

#: The most one call can cost: a generous prompt and a full reply.
WORST_CALL_TOKENS = (3_000, 4_096)


class Budgeted:
    """A provider that will not make a call the budget cannot cover in the worst case.

    Spend is read from the call log, which on a resumed run includes every
    earlier run's calls, so the budget covers the whole evaluation.
    """

    def __init__(self, provider: Provider, log: CallLog, budget_usd: float) -> None:
        worst = cost_usd(provider.model, *WORST_CALL_TOKENS)
        if worst is None:
            raise SystemExit(f"{provider.model} has no known price; refusing a budgeted run")
        self.provider, self.log, self.budget, self.worst = provider, log, budget_usd, worst
        self.name, self.model = provider.name, provider.model

    def complete(self, **kwargs: Any) -> Completion:  # noqa: ANN401
        spent = self.log.cost_usd or 0.0
        if spent + self.worst > self.budget:
            raise RunStopped(f"budget: ${spent:.4f} of ${self.budget:.2f} spent")
        return self.provider.complete(**kwargs)


class Capped:
    """A provider that stops the run after ``limit`` calls in this invocation."""

    def __init__(self, provider: Provider, limit: int) -> None:
        self.provider, self.limit, self.made = provider, limit, 0
        self.name, self.model = provider.name, provider.model

    def complete(self, **kwargs: Any) -> Completion:  # noqa: ANN401
        if self.made >= self.limit:
            raise RunStopped(f"--max-requests {self.limit} reached for this run")
        self.made += 1
        return self.provider.complete(**kwargs)


class Resumable:
    """Triage what is not yet done, keep every outcome on disk, stop cleanly."""

    def __init__(self, agent: TriageAgent, outcomes: Path, max_fatal: int = MAX_FATAL) -> None:
        self.agent, self.path, self.max_fatal = agent, outcomes, max_fatal
        self.name = agent.name
        self.done: dict[str, Outcome] = {}
        if outcomes.exists():
            with outcomes.open(encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        outcome = Outcome.from_json(json.loads(line))
                        if not outcome.errored:
                            self.done[outcome.key] = outcome
        self.completed_this_run = 0
        self.fatal: list[str] = []
        self.stopped = ""
        self.rows: list[dict[str, Any]] = []

    def triage(self, rows: Sequence[dict[str, Any]]) -> list[Outcome]:
        self.rows = list(rows)
        for row in rows:
            if item_key(row) in self.done:
                continue
            try:
                outcome = self.agent.triage_one(row)
            except RunStopped as stop:
                self.stopped = str(stop)
                break
            if outcome.errored:
                # Never persisted, so the next run tries the finding again; and
                # never scored, because the model was never asked.
                self.fatal.append(outcome.reason)
                if len(self.fatal) >= self.max_fatal:
                    self.stopped = f"{len(self.fatal)} fatal API errors; the last: {outcome.reason}"
                    break
                continue
            self.done[outcome.key] = outcome
            self.completed_this_run += 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(outcome.to_json()) + "\n")
        return [self.done[k] for k in (item_key(r) for r in rows) if k in self.done]

    def complete(self) -> bool:
        return all(item_key(row) in self.done for row in self.rows)


def gemini_models(key: str) -> list[str]:
    """The model ids this key may call generateContent on. Listing is not a
    generation request, so it does not count against the daily cap."""
    from google import genai

    names = []
    for model in genai.Client(api_key=key).models.list():
        actions = getattr(model, "supported_actions", None) or []
        if "generateContent" in actions:
            names.append(str(model.name).removeprefix("models/"))
    return sorted(names)


def provider_for(name: str, model: str, key: str, rpm: float) -> Provider:
    if name == "gemini":
        from google import genai

        return GeminiProvider(genai.Client(api_key=key), model, requests_per_minute=rpm)
    import anthropic

    return AnthropicProvider(anthropic.Anthropic(api_key=key), model)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--provider", choices=sorted(MODELS), default="gemini")
    parser.add_argument("--model", help="defaults to gemini-2.5-flash or claude-opus-5")
    parser.add_argument("--budget-usd", type=float, help="required for --provider anthropic")
    parser.add_argument("--rpm", type=float, default=10, help="requests per minute (Gemini)")
    parser.add_argument("--max-requests", type=int, default=250, help="per invocation")
    parser.add_argument("--queue", type=Path, default=Path("summary/triage_queue.csv"))
    parser.add_argument("--labels", type=Path, default=LABELS)
    parser.add_argument("--runs", type=Path, default=Path("evals/a1_runs"))
    parser.add_argument("--results", type=Path, default=RESULTS)
    parser.add_argument("--record", action="store_true", help="append both scores to --results")
    parser.add_argument(
        "--sample",
        nargs="?",
        type=int,
        const=60,
        default=0,
        metavar="N",
        help="score a fixed-seed sample of about N findings (default 60): every "
        "rule-unexplained finding plus a stratified spread of the rest",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="print the Gemini models this key can generate with, and stop",
    )
    parser.add_argument(
        "--list-price",
        nargs=2,
        type=float,
        metavar=("IN", "OUT"),
        help="USD per million input and output tokens, for a model the price table lacks",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=None,
        help="retries per finding; the agent's own bound unless given",
    )
    args = parser.parse_args(argv)
    sample_size = args.sample

    model = args.model or MODELS[args.provider]
    if args.list_price:
        PRICES_PER_MTOK[model] = (args.list_price[0], args.list_price[1])
    if model not in PRICES_PER_MTOK:
        print(
            f"warning: {model} has no list price; costs will read 'unknown'. "
            "Pass --list-price IN OUT to record an equivalent.",
            file=sys.stderr,
        )
    variable = KEYS[args.provider]
    # Read here, passed to the SDK, and nowhere else: it is never logged, never
    # written, and scrubbed from any error text that reaches the call log.
    key = os.environ.get(variable, "")
    if not key:
        print(f"{variable} is not set; nothing was run.", file=sys.stderr)
        return 2
    if args.provider == "anthropic" and args.budget_usd is None:
        print("--provider anthropic bills per call; pass --budget-usd.", file=sys.stderr)
        return 2

    run_dir = args.runs / f"{args.provider}-{model}"
    log = CallLog.resume(run_dir / "calls.jsonl")
    before_calls = log.calls
    if args.provider == "gemini":
        # Before any finding: a model the key cannot use fails here, once, with
        # the list it can use -- not 250 times as a score (silent failure #15).
        try:
            available = gemini_models(key)
        except Exception as exc:  # the key itself, or the network
            message = str(exc).replace(key, "[redacted]")
            print(f"could not list models: {type(exc).__name__}: {message}", file=sys.stderr)
            return 2
        if args.list_models:
            print("\n".join(available))
            return 0
        if model not in available:
            print(
                f"{model} is not available to this key. Available: {', '.join(available)}",
                file=sys.stderr,
            )
            return 2
    provider: Provider = provider_for(args.provider, model, key, args.rpm)
    if args.budget_usd is not None:
        provider = Budgeted(provider, log, args.budget_usd)
    provider = Capped(provider, args.max_requests)
    options: dict[str, Any] = {"model": model, "log": log, "redact": (key,)}
    if args.provider == "gemini":
        # A 429 that pacing did not prevent is a per-minute limit; wait out a
        # minute rather than two seconds. The bound on attempts is unchanged.
        options["backoff_seconds"] = 30.0
    if args.max_attempts is not None:
        options["max_attempts"] = args.max_attempts
    agent = Resumable(TriageAgent(provider, **options), run_dir / "outcomes.jsonl")

    queue = load_queue(args.queue)
    labels = load_labels(args.labels)
    scope = ""
    if sample_size:
        full = len(queue)
        queue = sample_queue(queue, sample_size)
        keys = {item_key(r) for r in queue}
        labels = [label for label in labels if label.key in keys]
        unexplained = sum(1 for r in queue if r.get("triage_rule") == "unexplained")
        scope = (
            f"{len(queue)} of {full} findings, seed {SAMPLE_SEED}: all {unexplained} "
            f"rule-unexplained plus {len(queue) - unexplained} stratified by rule and system"
        )
        print(f"sample: {scope}")
    result = score(agent, queue, labels, log=log)

    run_line = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provider": args.provider,
        "model": model,
        "completed_this_run": agent.completed_this_run,
        "done": len(agent.done),
        "of": len(agent.rows),
        "calls_this_run": log.calls - before_calls,
        "stopped": agent.stopped,
    }
    with (run_dir / "runs.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(run_line) + "\n")
    if agent.fatal:
        print(
            f"FAILED: {len(agent.fatal)} calls failed before the model answered. "
            f"First error: {agent.fatal[0]}",
            file=sys.stderr,
        )
    print(
        f"this run: {agent.completed_this_run} findings completed, "
        f"{log.calls - before_calls} calls; {len(agent.done)} of {len(agent.rows)} done"
    )
    if agent.stopped:
        print(f"stopped: {agent.stopped}")

    if agent.fatal:
        print("not scored: fix the error above; failed findings will be retried.")
        return 1
    if not agent.complete():
        print(
            f"not scored: {len(agent.rows) - len(agent.done)} findings remain. "
            "Run the same command again to continue."
        )
        return 0

    baseline = score(RuleTriager(), queue, labels)
    result.model, result.provider, result.billing = model, args.provider, BILLING[args.provider]
    baseline.sample = result.sample = scope
    outcomes = [agent.done[item_key(r)] for r in agent.rows]
    human = write_human_queue(outcomes, agent.rows, run_dir / "human_queue.csv")
    routed = Counter(o.reason.split(":")[0].split(" (")[0] for o in outcomes if not o.accepted)
    for triager in (baseline, result):
        print(triager.summary())
        passed, why = gate(triager)
        print(f"  gate: {'pass' if passed else 'hold'} -- {why}")
    print(f"human queue: {human} findings -> {run_dir / 'human_queue.csv'}  {dict(routed)}")
    print(json.dumps({"calls": log.summary(), "billing": BILLING[args.provider]}, indent=1))
    if args.record:
        append_result(baseline, args.results)
        append_result(result, args.results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
