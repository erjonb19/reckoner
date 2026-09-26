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

The same goes for an outage. A finding whose every attempt failed before the
model answered (three 503s, "this model is currently experiencing high demand")
is an `error` and is retried on the next run. It is not a human-queue entry. On
2026-09-26, 15 of the first 21 calls to `gemini-3.8-flash` were 503s, and three
findings had been saved as human-queue outcomes. The runner also checks the
model against the key's model list before the first finding
(`--list-models` prints that list).

**Free-tier limits, read from Google AI Studio's rate-limit page on
2026-09-26.** Google's docs no longer publish the numbers. They apply per
project, and requests per day reset at midnight Pacific.

| model | requests/min | tokens/min | requests/day |
|---|---|---|---|
| Gemini 3.8 Flash, and every other Flash from 2.5 to 3.7 | 5 | 250K | 20 |
| Gemini 2.5 Flash Lite | 10 | 250K | 20 |
| Gemini 3.1 Flash Lite | 15 | 250K | 500 |
| **Gemini 3.5 Flash Lite** | 15 | 250K | **500** |

At 20 requests a day the full queue takes two weeks. Gemini 3.5 Flash Lite
allows 500.

**A sample.** `--sample` scores about 60 findings: all 29 the rules left
`unexplained`, the ones the agent exists for, plus 31 spread across rule and
health system in proportion, with a fixed seed (`SAMPLE_SEED`). The selection
reads the queue and never the labels. The rules baseline is scored on the same
sample. Both records carry a `sample` field, and the printed score is marked
`[SAMPLE: …]`. A sample's precision estimates the queue's; it is not the
queue's.

The score is computed and recorded only once every finding in scope has an
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

### The agent, measured: Gemini 3.5 Flash Lite on a 60-finding sample

Run on 2026-09-26 on the Gemini API free tier ($0 billed). The sample is
`--sample` at seed 20260926: all 29 findings the rules left `unexplained`, plus
31 stratified by rule and system. The rules are scored on the same 60.

| triager | precision | coverage | correct | wrong | to human |
|---|---|---|---|---|---|
| rules | 0.000 | 0.517 | 0 | 31 | 0 |
| agent, `gemini-3.5-flash-lite` | **0.952** | 0.350 | 20 | 1 | 39 |

All 60 calls succeeded on the first attempt, using 38,218 input tokens and
6,904 output. No answer failed validation. The gate passes at precision 0.952.
The two runs before this one produced no score: `gemini-2.5-flash` returned a
404, and `gemini-3.8-flash` hit its 20-a-day cap (silent failure #15).

**What the answers have in common.** The table below reads only the queue's
fields and the agent's own outputs. Nothing here was used to change a prompt, a
threshold or a rule.

- **It is confident about one thing.** All 21 accepted answers came on the
  first attempt, at 0.85 or 0.90. 20 of them are `units_or_methodology`, and
  every one of those sits 3× or more from the insurer's median (median 7.8×).
  That is the one cause the labels are dominated by, and the one cause
  `validate` can check (a ratio of 3× or more).
- **The 39 held back are the same answer, said with less certainty, plus
  everything else.** Of the 43 findings 3× or more apart, 21 still went to the
  human queue. 20 of those proposed `units_or_methodology`, the same cause the
  accepted ones gave, at 0.50–0.75. Their reasoning often names the likely
  mechanism itself: professional against technical component, per diem, per
  unit. The 17 findings within 3× never cleared 0.80. Those answers were 11
  `insufficient_evidence`, 3 `genuine_disagreement`, 2 `vintage_artifact`
  and 1 `contract_offset`. Near the median, the agent has no confident answer.
- **The findings it exists for are mostly still open.** It settled 9 of the 29
  rule-`unexplained` findings (8 right, 1 wrong). The other 20 went to a
  person.
- **The one wrong answer sits on a boundary.** CPT 84702 (a lab test), Northwell
  (Huntington Hospital) against Cigna: the hospital publishes $346.87, and the
  insurer's three rates run $31.22–$35.12. That is 9.9× apart, just under the
  10× at which the mart sets a pair aside as implausible. The agent called it
  `data_error` ("such a large magnitude discrepancy for a standard lab code");
  the label says `units_or_methodology`. Nothing deterministic separates the two
  causes at that distance. `validate` checks a ratio for units, but has no
  precondition for a data error. It was the only answer 3× or more apart that
  named something other than units.

**What this does and does not show.** The agent is a precise, cautious
classifier of unit and component mismatches. It says so when it isn't sure,
and it rarely claims anything else. The sample is 60 findings, one model on one
day, and the labels are 84% one cause. So the 0.952 is a measure of that one
skill, with a wide interval at n=21. Coverage is what it costs: 0.35, with
most of the residual still going to a person. The component-pricing detector in
`docs/BUILT_VS_PLANNED.md` would settle the confident cases deterministically
and leave the agent the harder ones. Only a re-labelled queue can say how it
does on those.

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
