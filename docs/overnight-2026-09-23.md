# Overnight, 2026-09-23

Two lines per item, in the order worked. Items that need a decision or a login
from you are marked **STOP**.

| # | item | outcome |
|---|---|---|
| 9 | A1 scaffold | `agents/triage_agent.py`: propose → deterministic `validate` → bounded retries (3, doubling backoff, rejection reason fed back) → human queue. Every attempt, failed ones included, is logged with tokens, cost and latency; an unpriced model costs `None`, not $0. `agents/triage_evals.py` scores any triager from `evals/triage_labels.csv`. 62 tests, stubbed model, no API call made. |
| | | **Needs you, not blocking**: `evals/triage_labels.csv` ships empty, so the eval reports `not measured`. `docs/labelling-a1.md` covers filling it. Run `--worksheet`, fill `expected_cause`, and save over the file. |
