# reckoner

Reconciles **two different federal price transparency disclosures of the same
negotiated rates** for New York hospitals, quantifies where they disagree, and
explains why.

Hospitals publish under the Hospital Price Transparency rule (45 CFR 180). Insurers
publish under Transparency in Coverage. Both describe rates for the same care at the
same facilities, and they do not agree. CMS has formally named the misalignment
between the two as a barrier to price transparency.

This is a personal project — deployed, scheduled, monitored and tested, but serving
no users and supporting no one's decisions.

## What the two rules cover

|  | Hospital (45 CFR 180) | Payer (Transparency in Coverage) |
|---|---|---|
| Who publishes | hospitals | insurers |
| Plans covered | **every** payer and plan the hospital contracts with, including Medicare Advantage, Medicaid managed care and CHP | commercial group and individual **only** — CMS exempts Medicare, MA, Medicaid and Medicaid MCO |
| File size | tens to hundreds of MB | 100 GB to 1 TB+ |
| Updated | at least annually | monthly |

The consequence runs through everything here: **Medicare Advantage and Medicaid rates
exist only in hospital-side files.** Any claim otherwise is a bug.

## What is actually in it

156,484,277 hospital rate lines across 12 health systems, and 59.5M payer rate lines
across 120 TiC files and 6 carriers.

What reconciles is much smaller than those totals, and the honest accounting matters
more than the headline: `billing_class` is optional for hospitals and required for
payers, only four of the twelve systems appear in both sources at all, and roughly
46% of one carrier's rows carry a rate of exactly $0. See
[`docs/coverage.md`](docs/coverage.md) and [`docs/scope.md`](docs/scope.md) for the
full matrix and the caveats, including the negative results — two plausible
explanations for the cross-source coverage gap that the data disproved.

## How it runs

ADLS Gen2 is the authoritative store; everything else reads from it. A Container Apps
Job runs one stage per execution on a monthly schedule, authenticating with a
user-assigned managed identity, emitting structured telemetry to Log Analytics, and
exiting non-zero when what is in the lake stops matching the manifest that described
it. No secret exists in this repository or in the image.

```
python -m payer.contract --payer-root ../mrf_pipeline/payer_parquet   # the boundary contract
python -m storage.publish --all --root data/lake                      # publish to the seam
python -m reckoner_job --stage manifest                               # the scheduled check
```

## Documentation

- [`docs/BUILT_VS_PLANNED.md`](docs/BUILT_VS_PLANNED.md) — what is built, what is
  scaffolded, what is not started. Check here before believing a claim made anywhere
  else.
- [`docs/silent-failures.md`](docs/silent-failures.md) — every bug so far that
  **reported success and was wrong**, with the check that now catches each one. The
  most useful document in the repository.
- [`docs/adr/`](docs/adr/) — the design decisions, numbered, including the one that
  dropped Fabric and the one that chose the orchestrator.
- [`docs/SPEC.md`](docs/SPEC.md) — the plan.
- [`CLAUDE.md`](CLAUDE.md) — working context, domain facts, and architecture rules.

## Layout

The parser lives in a separate repository (`mrf_pipeline`), which writes the payer
Parquet this one reads. The boundary between them is a declared contract
(`src/payer/contract.py`) rather than an assumption — a column the parser had been
writing for months went unused while the reader fell back to a hardcoded date map, and
the contract exists so that cannot recur.
