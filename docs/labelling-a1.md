# Labelling A1: how to fill `evals/triage_labels.csv`

A1's agent half (`src/agents/triage_agent.py`) is built and tested against a
stubbed model. **The labels are in:** all 250 findings in the published queue,
labelled September 2026. [Results](#results) has what they show and what they
don't. An empty label file reports `not measured`, never 0%.

## 1. Make a worksheet

```
python -m agents.triage_evals --queue summary/triage_queue.csv --worksheet evals/triage_worksheet.csv
```

This writes one row per finding in the current triage queue (250 today). The
`expected_cause` column is blank. After it come the columns you judge from: the
rates, the ratio, both vintages and the gap, and the deterministic rule's
verdict (`triage_rule`) as a hint. The scorer never reads the hint.

## 2. Fill `expected_cause` for the rows you can judge

Use exactly one of these values. Anything else stops the run and names the line.

| value | use it when |
|---|---|
| `units_or_methodology` | the two rates are expressed differently: per diem vs case rate, per unit vs per service, percentage vs dollars. Usually a ratio of 3× or more. |
| `vintage_artifact` | the files are far enough apart in time that the contract probably moved. The agent may only claim this at a gap of 30 days or more. |
| `contract_offset` | the difference is a base-rate gap across the whole contract (the same ratio on many codes), not something about this code. |
| `plan_mismatch` | the hospital's plan and the payer's network are probably not the same contract. |
| `setting_or_modifier` | setting, billing class or a modifier differ in a way the join missed. |
| `data_error` | one side's value is wrong on its face: a placeholder, a typo, $0.01. |
| `genuine_disagreement` | both filings look right and describe the same contract. This is the actual finding. |
| `insufficient_evidence` | you can't tell from these fields. This is a real label. It tells the eval the right move was to abstain. |

Leave a row blank if you haven't looked at it yet. Blank rows are skipped, so a
half-finished worksheet is fine. Fill `labelled_by` and `labelled_on`
(YYYY-MM-DD). Use `note` for anything a later reader would need, such as the
code's description, or why a ratio that looks like a units mismatch isn't one.

**Label what you believe, not what the rule says.** If you copy `triage_rule`,
you're measuring agreement with the baseline, and the baseline would score
perfectly against its own answers.

## 3. Save as `evals/triage_labels.csv` and score

Copy or rename the worksheet over `evals/triage_labels.csv`. The extra context
columns are ignored, so there's no need to delete them. Then run:

```
python -m agents.triage_evals --queue summary/triage_queue.csv
```

This scores the deterministic baseline, which costs nothing. Add `--record` to
append the score to `evals/triage_results.jsonl`.

## What the numbers mean

- **precision**: of the findings it gave a cause for, the share that were right.
  This is the gate (0.90). A wrong cause in a report reads as a finding. An
  abstention reads as "we don't know", which is honest.
- **coverage**: the share it answered at all, rather than abstaining or sending
  it to a human.
- **unmatched**: labels whose finding is no longer in the queue. Gold changes
  between runs. When the queue is regenerated, labels can drift out of it, and
  they're reported rather than silently dropped.

## How many labels

A minimum of about 50, spread across causes, before any number means much. With
250 findings in the queue, labelling all of them is an afternoon. The 29 rows
the rules call `unexplained` matter most, because they're the only ones the
agent exists for.

## Running the agent itself

The agent is never run from a flag, because every finding it sees is a model
call. It runs from a script that has to be invoked on purpose:

```
pip install -e .[agents]
python scripts/run_a1_eval.py --provider gemini --record
```

**Model: Gemini 2.5 Flash, on the Gemini API free tier.** The key is read from
`GEMINI_API_KEY` and nowhere else. It is passed to the SDK, never logged or
written, and scrubbed from any error text before that reaches the call log. The
provider is pluggable (`src/agents/triage_agent.py`): `--provider anthropic`
runs Claude instead, reads `ANTHROPIC_API_KEY`, and refuses to start without
`--budget-usd`. Validation, bounded retries, the human queue and per-call
logging are the same code for both.

The script changes nothing about the agent: the prompt, validator, retry bound
and 0.80 review threshold are the ones written before any label existed. Two
settings are specific to Gemini, and neither was chosen against the labels:

- For 2.5 models only, a fixed thinking budget of 1,024 tokens, within a
  4,096-token output ceiling. Flash's thinking tokens share its output
  allowance, and without room for both, an answer can be cut off mid-JSON.
  Later models use a different thinking control, so they are left at their own
  default.
- A 30-second backoff on a 429, in place of 2 seconds, because the free tier
  limits requests per minute.

**The free tier.** Calls are paced at 10 a minute (`--rpm`). A per-minute 429
that still arrives is retried within the usual three attempts. A **daily** cap
ends the run instead, because retrying cannot clear it, and nothing is wrong
with the finding. Each finding's outcome is written to
`evals/a1_runs/gemini-gemini-2.5-flash/outcomes.jsonl` as soon as it is decided,
and every call to `calls.jsonl` as it is made. **Run the same command again,
the next day if need be, and it continues where it stopped.** Each invocation
prints how many findings it completed and appends a line to `runs.jsonl`.
`--max-requests` (default 250) caps a single invocation.

**A failed call is not an abstention.** If the API rejects the request itself,
say for a retired model name or an invalid key, the finding is an `error`, not a
human-queue entry. The run stops after three such errors, prints the first,
exits non-zero, and records nothing. On 2026-09-26, `gemini-2.5-flash` answered
every call with a 404 ("no longer available to new users"), and before this
guard the run was recorded as 250 abstentions (`docs/silent-failures.md`, #15).
To use another model, pass `--model`, and `--list-price IN OUT` if the price
table lacks it.

The score is computed and recorded only once all 250 labelled findings have an
outcome. The findings that happen to come first are not a sample of the labels.

**Cost.** The free tier bills $0. Every call is still logged with its tokens,
thinking tokens counted as output, and a **list-price-equivalent cost** at the
paid tier's $0.30 per million input tokens and $2.50 per million output. That
keeps this run comparable with a paid one, or with another model. Expect
roughly $0.40 equivalent for the whole queue. Two things to know:

- The free tier applies only when the key's Google Cloud project has no billing
  account. On a billed project the same calls are charged at that list price.
- Google may use free-tier prompts to improve its products. Every prompt here is
  a row of public price-transparency data, with no PHI.

The result is a fact about this model. Another model, or this one after an
update, is another result, so the recorded score names its provider, model and
billing.

## Results

### The rules baseline scores zero, by construction

Over all 250 labels: **precision 0.000, coverage 0.884** (221 wrong, 29
abstained, 0 unmatched).

The rules have exactly one route to `units_or_methodology`: the `implausible`
rule, at a ratio of 10× or more. The mart has already set every finding that far
apart aside as `entity_resolution_suspect` before the queue is built, so on this
queue that rule never fires and the rules never give that answer. The labelled
queue is dominated by that cause (below), so every answer the rules do give is
one of the others, and wrong. The zero measures the queue's make-up, not a
broken rule.

### What the labels measure

**210 of 250 labels are `units_or_methodology`**; the rest are 25
`genuine_disagreement` and 15 `insufficient_evidence`. The queue is dominated by
component-versus-facility mismatches: a hospital publishing one component of a
service against an insurer's rate for the whole of it, or the reverse. Their
ratios run from 0.10 to 9.98, and 106 of the 210 sit between 0.08× and 0.2×.
**So an agent's score on this queue mostly measures one skill**, spotting that
mismatch, and says little about the other seven causes, five of which have no
label at all. It is not a measure of triage in general.

The follow-up is in `docs/BUILT_VS_PLANNED.md`. First, a deterministic
component-pricing detector. Then a smaller queue, re-labelled stratified by
cause.

### The 25 labels that did not match

The first scoring left 25 labels unmatched. All 25 were Montefiore's, not White
Plains'. Montefiore's published file carries its facility name double-encoded,
UTF-8 read as cp1252 ("Center â€“ Children…"). The queue kept it that way; the
label file had it repaired. `item_key` now undoes that one round of
mis-encoding before joining, so both forms are one finding. The labels were
not edited. Before the fix: precision 0.000, coverage 0.924 over 225 matched.
