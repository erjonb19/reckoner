# CLAUDE.md

Project context for Claude Code. Read `docs/SPEC.md` for the full plan.

## What this project is

A pipeline that reconciles **two different federal price transparency disclosures** of the same negotiated rates for New York hospitals, quantifies where they disagree, and explains why. CMS has formally named the misalignment between these two rules as a barrier to price transparency.

This is a **production-grade personal project**: deployed, scheduled, monitored, tested. It is NOT production experience. Never write resume or README language claiming real users or business decisions depend on it.

## Domain facts you must not get wrong

There are **two separate rules**. Do not conflate them.

**Hospital Price Transparency (45 CFR 180)** — hospitals publish. Covers **every payer and plan the hospital contracts with**, including Medicare Advantage, Medicaid managed care, and CHP. CMS template layout required since 7/1/2024. Files are tens to hundreds of MB. Updated at least annually. Discoverable at `<hospital-domain>/cms-hpt.txt`.

**Transparency in Coverage** — payers publish. Covers **commercial group and individual market only**. CMS explicitly exempts Medicare, Medicare Advantage, Medicaid, and Medicaid MCO plans. Files are 100 GB to 1 TB+. Updated monthly.

**Consequence:** Medicare Advantage and Medicaid rates exist ONLY in hospital-side files. If code or docs imply otherwise, that is a bug.

**Third source:** CMS fee schedules (PFS, OPPS, IPPS) — the denominator for percent-of-Medicare, which is the unit contracting teams actually use.

## Architecture rules

1. **Parse once, land curated.** Read large files a single time, write a filtered slice to ADLS Gen2. Never rescan raw files.
2. **ADLS Gen2 is authoritative.** Fabric reads via OneLake shortcuts. Fabric is compute and presentation, never the only copy of the data.
3. **Logic in code, in git.** Notebooks and SQL. Do NOT use Dataflow Gen2 or GUI pipeline definitions — they do not port when the Fabric trial ends.
4. **Quarantine, never hard-fail.** Rows failing validation go to `_rejects` with a reason code. Alert on reject-rate thresholds.
5. **Every load is audited.** `LOAD_AUDIT`: batch id, source URL, file vintage, rows in, rows out, checksum. Loads must be idempotent and re-runnable.

## Agent rules

Four agents, all doing work **inside** the pipeline. NL-to-SQL over the marts is explicitly out of scope.

- **A1 Variance triage** — classify hospital-vs-payer discrepancies. Structured output only.
- **A2 Entity resolution** — match free-text payer/plan names across sources. Review decisions become labels.
- **A3 Schema adaptation** — generate parser adapters for non-conforming hospital files. Sandboxed.
- **A4 Ingest monitoring** — detect new file drops, judge material vs cosmetic change.

**Guardrails, non-negotiable:**
1. Agent proposes, deterministic code validates. Nothing reaches a curated table without passing tests.
2. Generated code executes sandboxed, no write access downstream.
3. Every agent action produces a testable artifact — adapter, match, or classification. Never just prose.
4. Bounded retries, human queue as terminal failure path, per-call cost and latency tracking.

**Build order:** deterministic implementation first, agent second, after the reject/variance tables show where the long tail is.

## Analytical hazards

- **Vintage mismatch is structural.** Hospital files update annually, payer files monthly. A variance may be a timing artifact. Never report a discrepancy as a finding without ruling this out.
- **Methodology heterogeneity.** Negotiated dollar, percentage, per diem, and case rate are not comparable. Comparability rules must be explicit and documented. Exclusions must be justified.
- **Percent-of-Medicare requires correct matching** on fee schedule, setting, and geographic locality. Not a lookup.

## Conventions

- Python 3.11+, type hints required
- pytest for tests; every parser needs tests built from **real files**, not from the CMS spec
- Streaming parsers for anything over ~1 GB; never load a full payer file into memory
- Secrets via environment, never in code
- PR-driven: CI gates on every push, no direct commits to main

## Layout

```
├── CLAUDE.md
├── docs/SPEC.md
├── src/
│   ├── discovery/     # cms-hpt.txt crawler, TOC walker, HEAD size probe
│   ├── hospital/      # CMS template parser + deviation handlers
│   ├── payer/         # streaming TiC parser, provider reference resolution
│   ├── benchmark/     # CMS fee schedule loaders
│   ├── reconcile/     # join logic, comparability rules, variance mart
│   └── agents/        # A1-A4, each with its own eval harness
├── notebooks/         # Fabric notebooks, exported
├── tests/
└── evals/             # labeled sets, scoring, results history
```

## Current phase

Phase 0 — foundation. See `docs/SPEC.md`.

**Open gate:** Fabric trial activation depends on tenant access. If unresolved, do not write Fabric-specific code yet.
