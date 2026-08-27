# NY Price Transparency Reconciliation Pipeline — Spec Sheet

**Version:** 2.0 (supersedes the July 2026 Snowflake extension spec)
**Date:** August 26, 2026

## What changed from v1

| v1 (July 2026) | v2 (this document) |
| --- | --- |
| Extension of the Security-Constrained Agent Runtime | Standalone new project. No DuckDB, no dependency on the runtime repo. |
| Snowflake as third backend | Microsoft Fabric as the platform. Snowflake reserved for a separate smaller project. |
| CMS hospital-quality star ratings | NY price transparency data — hospital MRFs plus one payer TiC file. |
| dbt-centric | Fabric-native (notebooks, SQL, semantic model). dbt story moves to the Snowflake project. |
| Dashboard as the consumer | Dashboard plus an LLM-assisted entity resolution layer with evals. |

**Why the data changed.** Star ratings barely move, so the incremental model was ceremonial and no decision depended on it. Price transparency data has real cadence, real money attached, and maps directly to healthcare finance and managed care roles.

## The core idea

Hospitals and payers each publish the negotiated rate for the same service, under two different federal rules, and the numbers frequently do not agree. CMS has formally acknowledged this: in the December 2025 proposed rule the Departments named **misalignment between the Transparency in Coverage rule and the 2019 Hospital Price Transparency rule** as one of three main barriers to achieving the goals of the 2020 rules, alongside file inaccessibility and lack of contextual information.

**Deliverable:** a pipeline and analytical layer that reconciles both disclosures for New York hospitals, quantifies where they disagree, and explains why.

This is a problem regulators have named and the market has not solved. That framing is worth more in an interview than another well-built dbt project.

## The two data sources

|  | Hospital Price Transparency (45 CFR 180) | Transparency in Coverage |
| --- | --- | --- |
| **Publisher** | Hospitals | Payers |
| **Products** | Every payer/plan the hospital contracts with | Commercial group + individual market only |
| **Medicare Advantage** | Included | Excluded |
| **Medicaid MCO / CHP** | Included | Excluded |
| **Marketplace / QHP** | Included | Included |
| **Grain** | Hospital → payer → plan → code | Payer → plan → provider → code |
| **File size** | Tens to hundreds of MB per hospital | 100 GB – 1 TB+ per file |
| **Update cadence** | At least annually | Monthly |
| **Discovery** | `<hospital-domain>/cms-hpt.txt` | Payer table-of-contents file |
| **Standardization** | CMS template layout required since 7/1/2024; additional required elements as of 1/1/2025 | CMS schema, but wide payer-level variation |

**Third source:** CMS fee schedules (Physician Fee Schedule, OPPS, IPPS). Free, small, and the denominator for percent-of-Medicare, which is the unit contracting teams actually use.

**Critical correction to note:** Medicare, Medicare Advantage, Medicaid, and Medicaid MCO plans are explicitly listed by CMS as **not** required to meet TiC requirements. Those products are only visible through the hospital-side files. Anyone who claims to see Medicaid rates in payer MRFs is mistaken.

## Scope

### Hospitals (spine)

15–25 NY hospitals spanning:

- NYC and Long Island
- Hudson Valley
- Capital District (Albany)
- Rochester / Syracuse / Central NY
- Buffalo / Western NY

Selection criteria: file present and parseable, CMS template compliant, meaningful payer/plan breadth.

### Payer TiC file (second lens)

Start with **Oxford / UnitedHealthcare**. Files run ~100 GB uncompressed, 99.9% accessible, consistently well-formatted. The obstacle is an API in front of the files that rate-limits downloads, plus a table of contents spanning thousands of files.

**Avoid Empire / Elevance for v1.** Some files exceed 1 TB uncompressed with 10B+ records; the table of contents alone has been measured at ~180 GB uncompressed. Note the trap: many URLs with different parameters point to the same file, so parse root files only (~1k, not 10k+).

### Codes

30–50 high-value services, mixed inpatient (DRG/APR-DRG) and outpatient (CPT/HCPCS), chosen for cross-hospital comparability.

## Platform

| Layer | Choice | Cost |
| --- | --- | --- |
| Heavy parse | Databricks Free Edition (or local) | $0 |
| Authoritative storage | ADLS Gen2, Hot LRS | ~$1–4/mo |
| Warehouse / modeling | Microsoft Fabric, 60-day trial → paused F2 PAYG | $0, then ~$15/mo |
| Semantic + BI | Fabric semantic model + Power BI | Pro license if sharing |
| AI layer | Azure AI Foundry, pay-per-token | a few $ |

**Total target: under $20/month, $0 for the first 60 days.**

### Design rules that make the cost work

1. **Parse once, land curated.** Read large files a single time, write a filtered slice. Never rescan raw.
2. **OneLake shortcuts to ADLS Gen2.** Fabric is compute and presentation over data you own. Trial expiry becomes a non-event.
3. **Logic in code, in git.** Notebooks and SQL, not Dataflow Gen2. Code ports; GUI artifacts do not.
4. **Script the F2 pause/resume.** Always-on F2 is ~$263/mo; disciplined use is ~$15.
5. **Budget alerts on day one** in both Azure and Fabric.

## Phases

Each phase is independently complete. If the clock runs out at Phase 2, there is still a finished project.

### Phase 0 — Foundation (~half day)

- Confirm you can activate a Fabric trial (tenant question — this is the gate)
- ADLS Gen2 storage account, containers for `raw/`, `curated/`
- Repo scaffold, CLAUDE.md, CI skeleton
- Azure budget alert + Fabric capacity metrics

**Exit:** Fabric workspace reachable, shortcut to ADLS working, empty CI run green.

### Phase 1 — Hospital MRF ingest (~1 weekend)

- Crawler resolving `cms-hpt.txt` for the target hospital list
- HEAD-request size probe; record Content-Length per file before download
- Parser handling the CMS template layout plus common deviations
- `LOAD_AUDIT` table: batch id, source URL, file vintage, rows in/out, checksum
- `_rejects` quarantine table with reason codes — never hard-fail the run
- Land curated Parquet to ADLS

**Exit:** N hospitals loaded, audit table reconciles, rejects triaged and explained.

### Phase 2 — Medicare benchmark + first marts (~half weekend)

- Load CMS fee schedules (PFS, OPPS, IPPS)
- Locality and setting matching logic
- Percent-of-Medicare normalization
- Dimensional model: hospital dim, payer/plan dim, code dim, rate fact
- Fabric semantic model + Power BI report

**Exit:** A working report answering "how does hospital X's rate for service Y compare across payers, as a percent of Medicare."

### Phase 3 — Payer TiC ingest (~1 weekend)

- Table-of-contents walker with rate-limit handling and resumable downloads
- Streaming parser, filtered to target NPIs/TINs and code set on the fly
- Provider reference resolution (the genuinely hard part — rates key to provider group IDs, often in separate referenced files)
- Land curated payer-side Parquet alongside hospital-side

**Exit:** One payer's NY rates landed for the target hospitals and codes, with the parse cost documented.

### Phase 4 — Reconciliation + AI layer (~1–2 weekends)

- Join keys: billing code, code type, provider, payer, plan, setting
- **Entity resolution:** payer and plan names are unstandardized free text on both sides. LLM-assisted matcher, labeled eval set, precision/recall reported, human review queue for low-confidence pairs.
- **Methodology classification:** case rate vs per diem vs percent-of-charges vs fee schedule, extracted from free-text fields.
- **Comparability rules:** explicit inclusion/exclusion logic. Document what cannot be compared and why.
- **Variance mart + report:** where the two disclosures disagree, by how much, and the candidate explanation.

**Exit:** A reconciliation report with a documented methodology, an eval score on the matcher, and a review queue.

### Phase 5 — Write-up (~2 hrs)

- README with architecture diagram
- Methodology document (this is the artifact that signals seniority)
- Incident log from deliberate failure testing
- Recorded walkthrough of the Power BI report, so the demo survives trial expiry

## Failure testing (do this, it is cheap and high-value)

Deliberately trigger and document:

1. Schema change in an upstream hospital file
2. Same file delivered twice
3. Partial load / interrupted download
4. Stale source (file vintage older than expected)

Capture each alert, fix it, commit the incident log. This converts "I built a pipeline" into "here is my incident log," which is a materially different conversation.

## Honest claims checklist

| Claim | Earned after |
| --- | --- |
| "Hands-on Microsoft Fabric" | Phase 1 |
| "Azure data platform: ADLS Gen2, Fabric, AI Foundry" | Phase 2 |
| "Ingested and reconciled federal price transparency disclosures at scale" | Phase 3 |
| "Built LLM-assisted entity resolution with an eval harness and human review queue" | Phase 4 |
| "Production experience" | Never. No real users or decisions depend on it. |

**Framing:** "production-grade personal project" — deployed, scheduled, monitored, tested.

## Open questions to resolve before starting

1. **Can you activate a Fabric trial?** Tenant-scoped; consumer Microsoft accounts generally cannot. Do not use the Montefiore tenant. This is a hard gate.
2. **Can Databricks Free Edition write to external storage you control?** If not, parse locally and upload. Affects Phase 1 and 3 design.
3. **Actual file sizes.** Run the HEAD probe across the hospital list and the Oxford TOC before committing.
4. **F4 vs F64 on the trial?** Sixteen-fold difference in headroom.
5. **Public repo naming NY health systems** — you are in process with at least one. Decide deliberately whether provider names are visible in the public artifact or anonymized in the display layer.

## Known risks

- **Vintage mismatch is structural.** Hospital files update annually, payer files monthly. A variance may be a timing artifact, not a real disagreement. This cannot be fully engineered away — document it rather than hiding it.
- **Methodology heterogeneity.** Negotiated dollar, percentage, per diem, and case rate are not directly comparable. Hospitals' own disclaimers warn that line-item comparisons may not reflect total contracted reimbursement. Getting this wrong is the fastest way for an experienced interviewer to discredit the work.
- **Uneven hospital compliance.** Expect to drop some hospitals despite the template requirement.
- **Entity resolution difficulty is unmeasured.** Sample real payer/plan name fields from both sources early, before committing to the Phase 4 design.
- **Fabric trial expiry at day 60.** Mitigated by shortcuts and captured artifacts, but the live demo lapses unless you move to paused F2.

## Reference links

- CMS Hospital Price Transparency: https://www.cms.gov/priorities/key-initiatives/hospital-price-transparency
- Steps for making public standard charges (CMS template): https://www.cms.gov/files/document/steps-machine-readable-file.pdf
- TiC proposed rule (Dec 2025): https://www.federalregister.gov/documents/2025/12/23/2025-23693/transparency-in-coverage
- CMS TiC technical guidance: https://github.com/CMSgov/price-transparency-guide
- UHC TiC files: https://transparency-in-coverage.uhc.com/
- Fabric trial: https://learn.microsoft.com/en-us/fabric/fundamentals/fabric-trial
