# 32BJ Health Fund: evaluation before any ingest

Status: **the inpatient 32BJ page is live (section 8.11); the rest is profile only.** Nothing here touches the lake, the payer
contract, or the pipeline. Written 2026-10-21 from five public files downloaded that
day, the local curated hospital lake, and public registries (NPPES, ProPublica's
Nonprofit Explorer). It sits behind the freeze (`docs/FREEZE_PLAN.md`): anything it
proposes is post-2026-10-31 work unless you decide otherwise.

## What this found, in five lines

1. **The 32BJ files are professional fee schedules, not hospital contracts.** Of
   928,489 negotiated prices across the four in-network files, 928,339 are
   `professional`. The 150 `institutional` rows are physical therapy and speech
   codes, office E&M visits, and 26 MS-DRG rows at $150–$992 that cannot be
   inpatient case rates.
2. **Neither Mount Sinai nor Northwell publishes a professional-class row** in our
   hospital lake. Codes overlap almost completely (12,749 of 12,813), but billing
   class does not. Under ADR 0005, nothing in these files reconciles against those
   two systems' hospital files.
3. **No hospital file in the lake names 32BJ under Mount Sinai or Northwell.**
   The only 32BJ strings are Montefiore's St. Luke's Cornwall (`Anthem / Local 32BJ`)
   and NYU Langone (`BCBS / LOCAL 32 BJ 1301`). Every other "SEIU" hit is **1199SEIU**,
   a different union and fund.
4. **No Montefiore TIN or organisation NPI appears in any of the five Healthcare
   Highways files.** (Montefiore does appear in the Anthem files; see section 4.)
5. **The vintage is ambiguous and the rates have lapsed.** Every in-network file
   says `last_updated_on: 2025-09-30` and `expiration_date: 2026-09-28`. That
   `last_updated_on` predates both direct contracts it supposedly encodes.

The Anthem table of contents was never listed, because the link in the request was
still the `<paste here>` placeholder. Seven Anthem files downloaded by hand were
profiled instead (section 4). They hold the hospital facility rates that the
Healthcare Highways files lack.

---

## 1. Files profiled

All five came from `https://hchmrfblob.blob.core.windows.net/clearcost/`. They are
stored in `data/raw/32bj/`, which is gitignored (`.gitignore:43`, `data/`). Disk had
331 GB free; the whole set is 44 MB compressed and 402 MB uncompressed. Every file
was stream-read with ijson (yajl2_c backend), straight out of the zip. No file was
decompressed to disk or loaded whole.

| file | contract | compressed | uncompressed | schema | blob Last-Modified |
|---|---|---:|---:|---|---|
| `PS_MTS_1.zip` | Mount Sinai direct | 1,826,093 | 50,046,613 | 2.2.1 | 2026-09-28 |
| `PS_NW1_1.zip` | Northwell direct | 42,225,028 | 352,234,624 | 2.2.1 | 2026-09-28 |
| `PS_SP1_1.zip` | Spear PT | 1,544 | 21,088 | 2.2.1 | 2026-09-28 |
| `PS_SP2_1.zip` | Spear PT | 1,238 | 14,025 | **1.2.0** | 2025-12-04 |
| `32BJ_..._OON_MRF.json.zip` | out-of-network | 21,348 | 332,675 (6 files) | 2.0 | 2025-12-31 |

SHA-256 of each zip, for re-runs:

```
9fe9c5b9043cc8f68b22ba316a167b546104b6739e8205e38410d06346324ef7  PS_MTS_1.zip
aae43d5fb06be3e199f6b6a73f33a5ad38be5c40f272762397044babe9f8085e  PS_NW1_1.zip
0c9b5d431920e3472bb5485e1c7a5c5cd0b980735dbc81f383a2b380397bab4b  PS_SP1_1.zip
7264dcc3da21da91d05a2c304756f64d5ba37b0d5f967bc94c767cc31d2a76ad  PS_SP2_1.zip
a395616b64b95d085f9b3a350555d0103aa0f2ed050fd832ce87706fe02b8e59  32BJ_Transparency_in_Coverage_OON_MRF.json.zip
```

### Reporting entity and plan identity

**The in-network files do not say they are 32BJ's.** All four name
`reporting_entity_name: "Healthcare Highways"` with type `"Group Health Plan"`. Three
of them (MTS, NW1, SP1) carry **no** `plan_name`, `plan_sponsor_name`, `plan_id`, or
`issuer_name` at all. SP2, the v1.2.0 file, says `plan_name: "Health Plan"` and names
Healthcare Highways as issuer and sponsor. So the attribution to 32BJ comes only from
the hosting path and from the OON files.

The OON files are the only ones that identify the plan:

| field | value |
|---|---|
| `reporting_entity_name` | Healthcare Highways, LLC |
| `reporting_entity_type` | third party administrator |
| `plan_name` | Building Service 32BJ Health Fund |
| `plan_id_type` / `plan_id` | EIN / 132928869 |
| `plan_sponsor_name` | Board of the Trustees of the Building Service 32BJ Health Fund |
| `plan_market_type` | group |

That gap matters for the schema proposal in section 7: the sponsor has to be supplied
from outside the file, because the file does not carry it.

### In-network files

| | MTS | NW1 | SP1 | SP2 |
|---|---:|---:|---:|---:|
| `in_network` items | 12,813 | 12,835 | 33 | 33 |
| negotiated prices | 103,451 | 824,939 | 66 | 33 |
| provider groups | 448 | 2,649 | 1 | 1 |
| distinct TINs | 417 | 2,370 | 1 | 1 |
| distinct NPIs | 6,671 | 26,833 | 1 | 1 |
| `network_name` values | MTS | NW1, NW2, NW3, NW4 | SP1 - Spear PT Union Center | (none) |
| $0 rates | 688 | 1,774 | 0 | 0 |
| `expiration_date` | 2026-09-28 | 2026-09-28 | 2026-09-28 | 2025-12-04 |

**Billing code types.** No APR-DRG and no RC anywhere. MS-DRG appears only in NW1,
and only for 22 codes.

| declared type | MTS | NW1 | note |
|---|---:|---:|---|
| CPT | 9,468 | 9,468 | 485 of these are category II/III/PLA (`0001U`, `0002M` …) |
| HCPCS | 3,345 | 3,345 | all match the letter-plus-four shape |
| MS-DRG | 0 | 22 | see below |
| APR-DRG | 0 | 0 | |
| RC | 0 | 0 | |

MTS and NW1 list identical CPT/HCPCS code counts. The `in_network` array is a
master code list priced per provider group, not a set of codes chosen per contract.

**negotiated_type and billing_class.** Every price, in every file, is
`negotiated_type: negotiated` under `negotiation_arrangement: ffs`. There are no
percentages, per diems, derived rates, or bundles. Billing class:

| | professional | institutional |
|---|---:|---:|
| MTS | 103,451 | 0 |
| NW1 | 824,888 | 51 |
| SP1 | 0 | 66 |
| SP2 | 0 | 33 |

**Every price in every file says `setting: inpatient`**, including office E&M codes
billed with place of service 11. Treat `setting` as a default the publisher fills in,
not as data.

**The 26 MS-DRG prices are not hospital case rates.** They run from $150 to $992 and
carry place-of-service codes 12 (home), 21 (inpatient), 22 (outpatient), 31 (SNF) and
34 (hospice). Kidney transplant (DRG 652) is $825. The TINs behind them are small
groups in NW4, not hospitals. They are most likely per-visit amounts tagged with a
DRG. They must not be compared to a hospital's DRG case rate.

The other 25 NW1 institutional prices split two ways:

- 19 speech, PT and OT codes at $90–$115, under eight TINs in NW4
- six rows under Staten Island University Hospital (112868878) and Northwell Health
  Laboratories (113412370), in NW2: four office E&M codes and two PT evaluations at
  $150–$225

**Spear PT (SP1, SP2)** is one clinic: TIN 202775993, NPI 1063454577. The files hold
33 physical-medicine codes (97010–97039, G0283) at $85–$125, labelled
`institutional`/`inpatient`. SP2 is the older, expired version of SP1. Neither has
anything to do with hospital reconciliation.

### Out-of-network files

There is one TOC and six regional files (`bas`, `bas_pa`, `metro`, `north`, `sub`,
`tri`), all dated 2026-01-01. Together they hold 169 `out_of_network` items and 1,594
payment rows, all `professional`. Every item is declared `CPT`, but 46 are HCPCS
(`A0425`, `H0018` …) and 2 are three-digit codes. **These are allowed amounts, not
negotiated rates, and they are not comparable to anything in the hospital lake.**
They are useful context only.

The 127 NPIs resolve (NPPES) to 66 individuals and 61 organisations. The
organisations are mostly out-of-state labs and NJ/FL/CA behavioural-health and detox
providers. NY hospital NPIs among them:

- NewYork-Presbyterian: four NPIs (Manhattan, Queens, Bronxville)
- NYC Health + Hospitals
- Mount Sinai Hospital-Ambulance (1568601342)

---

## 2. Do the TINs match Mount Sinai and Northwell?

**Method.** Every EIN in the four in-network files (2,718 distinct) went to
ProPublica's Nonprofit Explorer API, which resolves only nonprofits. 88 resolved,
all MTS or NW1. The rest are physician PCs and LLCs that the public EIN registries
don't name. To characterise those, NPIs were sampled from NPPES. System reference
sets came from three public sources:

- EINs from the CMS filename convention in our own hospital source URLs
- ProPublica name searches
- type-2 NPIs from NPPES organisation searches (1,428 NPIs across the three systems)
  plus the `type_2_npi` header of the Montefiore-system v3.0.0 hospital files

### NW1 (Northwell direct): yes, for network NW1

The file holds four networks, and only two of them are Northwell:

| network | TINs | resolved nonprofits | what they are |
|---|---:|---:|---|
| NW1 | 153 | 70 | **69 are Northwell or Nuvance entities** |
| NW2 | 2 | 2 | Staten Island University Hospital; Northwell Health Laboratories |
| NW3 | 1,521 | 3 | not Northwell (Harmony Health Care LI, Beacon Christian CHC …) |
| NW4 | 711 | 12 | not Northwell: RWJBarnabas (NJ), St. Luke's (PA), St Barnabas Hospital (Bronx), St. Vincent's (MO) … |

Northwell hospital EINs present in NW1:

| hospital | EIN |
|---|---|
| Long Island Jewish Medical Center | 112241326 |
| Staten Island University Hospital | 112868878 |
| Mather | 111639818 |
| Northern Westchester | 131740118 |
| Phelps | 131725076 |
| South Shore University Hospital | 111667761 |
| Huntington | 111630914 |
| Central Suffolk (Peconic Bay) | 111661359 |
| Glen Cove | 111633487 |
| Lenox Hill | 131624070 |
| Danbury | 060646597 |
| Norwalk | 066068853 |
| Northwell Healthcare Inc | 112965586 |

NW1 also includes North Shore-LIJ Medical PC (453023019, 8,390 NPIs) and dozens of
faculty-practice PCs.

**Those hospital TINs carry professional prices only.** No NW1 row is a facility
rate for a Northwell hospital. The likeliest reading is hospital-employed clinicians
billing under the hospital's TIN.

NW3 and NW4 make up most of the file's provider references. If a future ingest
filters by `network_name`, keep NW1 and NW2 and drop NW3 and NW4.

TIN 131740118 is worth flagging. It sits in the same 13-17401xx EIN block as
Montefiore's hospitals (131740114, 131740119), which makes it look like a Montefiore
hit at first. It is **Northern Westchester Hospital**, a Northwell hospital.

### MTS (Mount Sinai direct): Mount Sinai clinicians, not Mount Sinai hospitals

None of the twelve Mount Sinai system EINs is in the file:

| entity | EIN |
|---|---|
| The Mount Sinai Hospital | 131624096 |
| Icahn School of Medicine | 136171197 |
| Beth Israel / MSBHC | 135564934 |
| Morningside | 132997301 |
| NYEE | 135562304 |
| South Nassau | 111352310 |
| Mount Sinai Health Partners PC | 812057452 |
| five more | — |

No Mount Sinai organisation NPI is in it either. Only 2 of 417 TINs are nonprofits,
both Ryan community health centers. A random 300-NPI sample resolved 299 in NPPES:
**all type-1 (individuals)**, 281 in NY, and **89 at zip 10029**, the East Harlem
main campus (1 Gustave L Levy Pl, 1176 5th Ave, 1468/1470 Madison, 5 E 98th St).
After that come 10019 (Mount Sinai West) and 10025 (Morningside). The largest TIN
(611661781, 3,997 NPIs) is not in the nonprofit registry, but its sampled clinicians
practise on the Mount Sinai campus.

MTS is therefore the professional side of a Mount Sinai-affiliated network: faculty
and voluntary attendings plus independent practices. Hospital facility pricing for
the March 2026 Mount Sinai agreement is not in this file.

### Montefiore: no

- **EINs:** none of the 18 Montefiore-system EINs appears in any of the five files.
  These are the five hospital EINs from our source filenames plus ProPublica hits:
  Montefiore Health System, Montefiore Einstein, Albert Einstein College of
  Medicine, Burke, Schaffer, White Plains Hospital, and others.
- **Organisation NPIs:** none of the 377+ Montefiore and affiliate NPIs appears, nor
  the `type_2_npi` values from the Montefiore, Nyack and Mount Vernon hospital
  headers.
- **OON:** no OON NPI resolves to a Montefiore, Einstein, Nyack, White Plains or
  Burke name.

**Limit:** this is an organisation-level check. A Montefiore-affiliated *individual*
clinician who also bills under some other TIN in these files would not show up.
Ruling that out needs the full NPPES dissemination file plus an affiliation source,
and public NPPES carries no employer field.

---

## 3. 32BJ strings in the hospital lake

The search ran over `data/lake/curated/hospital_rates`, all 15 systems, matching
`payer_name_raw` or `plan_name_raw` case-insensitively against `32 ?BJ`, `SEIU`,
`Building Serv`, and `Bldg Serv`. A looser pass for `Local 32`, `Healthcare
Highways`, `HCH`, `ClearCost` and `union` across the three named systems found
nothing more.

**32BJ itself:**

| system | payer / plan string | rows | where |
|---|---|---:|---|
| Montefiore | `Anthem` / `Local 32BJ` | 7,184 | **St. Luke's Cornwall only** (141340054), 2026-04: 3,097 CPT + 938 HCPCS outpatient, 1,256 APR-DRG inpatient case rates, 1,893 CDM/RC (mostly % of charges) |
| NYU Langone | `BCBS` / `LOCAL 32 BJ 1301` | 33,445 | four campuses, 2026-01, CPT/HCPCS, no `billing_class` |
| Mount Sinai | — | 0 | |
| Northwell | — | 0 | |

**1199SEIU, which is not 32BJ,** accounts for every other hit:

| system | string | rows |
|---|---|---:|
| Mount Sinai | `Local 1199` / `1199 Seiu - Tmsh`, `- Msq`, `- Bi`, `- Slw`, `- Brook` and `1199 Seiu` / `- Nyeei` | 138,989 (six plans, ~23k each, `facility`) |
| Montefiore | `SEIU1199` / `SEIU1199`; `SEIU1199` / `Local 1199` | 99,531 |
| NYU Langone | `1199` / `LOCAL 1199 SEIU HOMECARE BENEFIT FUND 2778` | 32,562 |
| NewYork-Presbyterian | `1199SEIU Funds` / `All Commercial Plans` | 9,412 |

A matcher that keys on "SEIU" will merge two unrelated funds. The A2 plan matcher
needs `32bj` and `1199` as distinct plan keys before any 32BJ work starts.

**Why Mount Sinai and Northwell are silent may be timing, not omission.** Northwell
Direct and 32BJ announced on 2025-12-16; Mount Sinai announced on 2026-03-06. Our
hospital vintages for those systems are 2026-03 (Northwell's main campuses) and
2026-04 (Mount Sinai). Both are close enough to the start dates that the hospital
files may simply predate the contracts. Per the vintage-mismatch rule, the absence is
**not a finding** until a later hospital vintage is checked.

---

## 4. Anthem (Empire) files for 32BJ

Seven files were downloaded by hand from Anthem's index for EIN 132928869 and moved to
`data/raw/32bj/anthem/` (gitignored). They were profiled with ijson, streamed from the
.gz, and nothing was ingested. The table of contents itself was not listed, so these
files' membership in 32BJ's TOC rests on how they were downloaded and on the network
label below.

| file | .gz bytes | uncompressed | items | prices | billing class |
|---|---:|---:|---:|---:|---|
| `NY_HYBYMED32BJ` | 4,058,084,485 | 15,300,907,162 | 26,750 | 6,571,533 | 4,902,246 prof / 1,669,287 inst |
| `NY_HXRLMED0000_01_06` | 995,181,580 | 3,255,353,396 | 4,474 | 8,801 | professional only |
| `NY_HXRLMED0000_02_06` | 1,198,811,561 | 4,091,166,597 | 6,868 | 8,256 | professional only |
| `NY_HXRLMED0000_03_06` | 1,370,966,346 | 4,917,862,216 | 9,611 | 35,075 | professional only |
| `NY_HXRLMED0000_04_06` | 309,737,769 | 1,077,249,357 | 13,992 | 113,417 | professional only |
| `NY_HXRLMED0000_05_06` | 140,318,657 | 554,100,683 | 17,272 | 449,280 | professional only |
| `NY_HXRLMED0000_06_06` | 105,977,223 | 1,633,181,965 | 26,750 | 5,967,218 | 4,287,763 prof / 1,679,455 inst |

**Header fields are the same in all seven files:**

- `reporting_entity_name`: Empire BlueCross BlueShield
- `reporting_entity_type`: Health Insurance Network
- schema 2.0.0
- `last_updated_on`: 2026-10-01

The files carry no plan fields. **Every TIN in every file is `type: npi`; there are no
EINs**, so the EIN checks used for the Healthcare Highways files cannot run here.
Matching used organisation NPIs from NPPES, resolved by name and taxonomy. Name-search
noise was dropped: for example, "North Shore Dental Arts" is not Northwell.

**HYBYMED32BJ: code types and pricing**

- **Codes:** CPT 13,033, HCPCS 11,069, APR-DRG 1,272, MS-DRG 738, RC 389, APC 248,
  CSTM-ALL 1.
- **Negotiated types:** fee schedule 5,236,314; negotiated 1,139,679; derived 120,937;
  percentage 60,733; per diem 13,870.
- **TINs:** 35,281, all of them NPIs.

**Two networks, one 32BJ-specific.**

| network | prices |
|---|---:|
| `NY Preferred Facility Tier1  32BJ` | 1,334,741 |
| `HMO POS PROVIDERS` | 5,140,190 |
| both | 96,602 |

Part 06 has the same 26,750 items and code set, but only `HMO POS PROVIDERS`. Parts
01–05 are professional-only remainders whose provider groups bundle around 159k NPIs
each. Those groups include every system, so they say nothing about any one of them.

**Hospital facility (institutional) rates in HYBYMED32BJ, by system:**

| system | facilities with institutional rates | network | 32BJ Tier1 vs HMO POS rate |
|---|---|---|---|
| Montefiore | Montefiore Medical Center (Bronx), Nyack, New Rochelle, Mount Vernon, White Plains, St. Luke's Cornwall | 32BJ Tier1 | **identical** (11,032 of 11,171 code keys identical, the rest overlap); inpatient as APR-DRG `derived` |
| Mount Sinai | The Mount Sinai Hospital, Mount Sinai Queens (LIC NPI), Mount Sinai Brooklyn (Beth Israel Brooklyn NPI) | 32BJ Tier1 | **0.917×** (≈ 1/1.09) on negotiated, fee schedule, MS-DRG `derived` and per diem; % of charges unchanged |
| Mount Sinai | South Nassau, Diagnostic & Treatment Center, rehab, ESRD | 32BJ Tier1 | identical on the rows this profile compared; **corrected in 8.10**: South Nassau's inpatient MS-DRG case rates are 0.917× too, and so are Morningside/West's and New York Eye and Ear's |
| NYU Langone | Tisch/Kimmel (two NPIs), Long Island, Brooklyn, plus units | HMO POS or both | identical |
| Northwell | **none for the 12 acute hospitals** (corrected 2026-10-21, section 8.10: they have standard Anthem rates in the HMO file, but no rows at all in the 32BJ file); 32BJ-tier rows only for psych units, dialysis centers, a rehab unit and an ambulatory surgery center | 32BJ Tier1 for those units | n/a |
| St. Barnabas | none; none of its 6 NPPES organisation NPIs appears in any file | — | — |

**How to read it**

- **Mount Sinai.** The 32BJ file prices Mount Sinai acute campuses 8.3% below
  Empire's HMO POS rates for the same code: three campuses in this profile, all six
  acute campuses' inpatient case rates in the full extraction (8.10). This is a uniform multiplier, and it
  appears in a file dated after the March 2026 Mount Sinai–32BJ agreement. That is
  consistent with the agreement, but it is an inference: the file does not say why.
- **Northwell.** Northwell's acute hospitals are in Anthem's standard network but not
  in the 32BJ tier. That fits the press release: 32BJ reaches Northwell's hospitals
  through Northwell Direct, not Anthem. That fit is an inference. (This section first
  said the acute hospitals had no facility rates in Anthem's files at all; the full
  extraction in section 8.10 corrected it.)
- **Vintage.** Both Anthem files are dated 2026-10-01, so the Tier1 vs HMO POS
  comparison has no vintage gap. Comparing them with the hospital files does: those
  are 2026-03/04.
- **Size.** Every non-professional row for these five systems (institutional or
  unlabelled) in the 32BJ file fits in a 1.6 MB gzip CSV. If this is ingested later,
  storage is not the constraint.
- **Not yet done.** Montefiore's St. Luke's Cornwall publishes `Anthem / Local 32BJ`
  rates (section 3). Those can be checked code by code against St. Luke's Cornwall
  rows here.

---

## 5. Vintage

| source | `last_updated_on` | other date |
|---|---|---|
| PS_MTS_1, PS_NW1_1, PS_SP1_1 | 2025-09-30 | blob modified 2026-09-28; every price expires 2026-09-28 |
| PS_SP2_1 | 2025-09-30 | blob modified 2025-12-04; expires 2025-12-04 |
| OON files | 2026-01-01 | blob modified 2025-12-31 |

`last_updated_on` is not credible here. The MTS file is dated six months before the
Mount Sinai agreement was announced. The pattern, an expiration equal to the upload
date, suggests the generator stamps `expiration_date` at publish time and never
updates `last_updated_on`. As of today every in-network rate is past its stated
expiry.

**Proposal:** take vintage from the blob's `Last-Modified`, recorded in `LOAD_AUDIT`
with the file's own `last_updated_on` kept alongside. Re-probe monthly. This is a
departure from `_file_vintage`, which trusts `last_updated_on`, so it needs a
per-source override rather than a global change.

---

## 6. What a 32BJ comparison could and could not show

**Not possible with these files: hospital facility rate vs 32BJ direct rate.** The
32BJ side is professional; the Mount Sinai and Northwell hospital sides are
facility-only, and neither names 32BJ. That is the comparison the request asks
about, and the data does not support it today.

What is possible, in order of value:

1. **32BJ professional rates as percent of Medicare PFS.** The benchmark already
   loads PFS (`src/benchmark/`). Matching on CPT/HCPCS plus modifier, setting
   (facility vs non-facility PFS) and the NYC or LI locality would test public claims
   directly:
   - the 5% annual cap on fee-schedule increases (Northwell press release)
   - a widely repeated, not yet verified "185% of Medicare on average"

   Locality is load-bearing: NW1 spans Manhattan, Long Island, Westchester and CT
   (Danbury, Norwalk).

   Two parts of the file will not survive cleanly:
   - `setting` is useless, so facility vs non-facility PFS has to come from place of
     service or a documented default.
   - No row carries a `billing_code_modifier`, so global, -26 and -TC cannot be told
     apart. That is a documented exclusion candidate for radiology and pathology.

2. **32BJ direct vs the same clinicians' carrier rates.** The payer lake's
   `matched_tins` and `matched_npis` columns allow a TIN-level join:
   - "Northwell's own TINs under Northwell Direct (NW1)" against the same TINs under
     Empire, UHC, Aetna and Cigna
   - the same for Mount Sinai clinician TINs (MTS)

   This is payer-vs-payer, so it is a new comparison type, not a variance-mart row.
   It is the closest public test of "direct contracting saves 20%".

3. **Hospital vs 32BJ via Anthem, where a hospital names it.** Montefiore's St.
   Luke's Cornwall publishes 1,256 APR-DRG case rates and 4,035 CPT/HCPCS outpatient
   rates for `Anthem / Local 32BJ`. NYU Langone publishes 33,445 rows for
   `BCBS / LOCAL 32 BJ 1301`. If the Anthem TOC shows a 32BJ-specific in-network
   file, those rows become a like-for-like hospital-vs-payer reconciliation inside
   the existing rules (ADRs 0005, 0006). That depends on step 4.

4. **Re-check Mount Sinai and Northwell hospital files at their next vintage** for a
   32BJ or Northwell Direct plan line. If the contracts are in force and the line is
   still absent, that is a reportable hospital-side gap.

---

## 7. Proposed plan

### 7.1 Schema: partition by plan sponsor, not by carrier

Today payer silver is keyed `carrier` then `vintage`. `discover_payer_files` splits
the filename stem into carrier and network, and the contract checks that the `payer`
column agrees with the filename (`src/payer/contract.py`). That model assumes the
publisher and the owner of the rates are the same insurer. 32BJ breaks it twice:

- **Healthcare Highways is the publisher, not the payer.** It is a TPA/network that
  publishes for other self-funded plans too. `carrier=healthcare-highways` would mix
  sponsors as soon as a second HCH client was added.
- **32BJ's rates come from two publishers.** The direct contracts come through
  Healthcare Highways; the Anthem network comes through Anthem's own files, where
  32BJ is at best one `reporting_plans` entry.

**Recommendation.** Partition the new source by **plan sponsor**:
`silver/payer_rates/carrier=32bj/vintage=…`, with stems such as `32BJ_MTS` and
`32BJ_NW1` so the existing carrier_network split still holds. Add these columns:

| column | 32BJ value | why |
|---|---|---|
| `plan_sponsor_name` | Building Service 32BJ Health Fund | absent from the in-network files; supplied from a source registry, with the OON file as evidence |
| `plan_id` | 132928869 | |
| `reporting_entity_name` | Healthcare Highways | already a column |
| `reporting_entity_type` | group health plan / TPA | |
| `contract_path` | `direct` | vs `carrier` for Empire/UHC/…; the variable the 20% claim is about |
| `network_name` | MTS, NW1–NW4 | already a column; becomes a filter |
| `vintage_source` | `blob_last_modified` | per section 5 |

For existing carriers, `plan_sponsor_name` stays null, meaning many sponsors behind
one carrier network. The contract gains a rule: `contract_path = direct` requires a
non-null sponsor.

The alternative is a full `sponsor=` partition level above `carrier=`. That is
cleaner if many self-funded plans follow, but it rewrites every existing path and
the upload manifest. It is not worth doing for one sponsor.

### 7.2 TIN-to-system mapping

Build `data/reference/tin_system_map.csv` from public sources only. Columns: `tin`,
`system_slug`, `entity_name`, `entity_kind` (`hospital`, `faculty_practice`,
`medical_pc`, `affiliate`), `source` (`hpt_filename`, `hpt_type_2_npi`,
`propublica`, `nppes_sample`), `confidence`, `checked_on`.

Seed it from what this profile already established:

- the 13 Northwell hospital EINs above, plus North Shore-LIJ Medical PC and the
  faculty-practice PCs (about 70 rows)
- Nuvance's medical practices (562669185, 061137531) and hospitals (Danbury
  060646597, Norwalk 066068853), mapped to `northwell-health` to match how the
  hospital lake already files Danbury and Norwalk
- Mount Sinai: the 12 system EINs, even though none appears in MTS. MTS TINs map to
  `mount-sinai-health-system` with `entity_kind = affiliate` and `confidence = low`
  where only the NPI-address sample supports it. Raise confidence only through a
  public source.
- Montefiore: the 18 EINs, kept so the Montefiore check is repeatable rather than
  done by hand

Additions:

- An A2-style review queue for TINs with many NPIs but no public name, such as
  611661781. A reviewer's decision becomes a label, as with the plan matcher.
- No Montefiore internal data. Every row cites a public source.

### 7.3 Ingest filters and storage

Measured: the full NW1 and MTS price rows as `(code_type, code, billing_class,
rate, modifier)` come to **2.7 MB** of zstd Parquet (2.32 MB NW1, 0.41 MB MTS). Per-row
TIN and NPI attribution, carried the way the payer lake does today, would multiply
that by an estimated 5–10x, still well under 50 MB. Against the 5 GB target (5.282 GB
measured in September), 32BJ is noise either way. The filters are for analytic
correctness more than bytes:

| filter | keeps | drops |
|---|---|---|
| file | MTS, NW1 | SP1, SP2 (one PT clinic); OON files (allowed amounts; keep raw only) |
| `network_name` | MTS; NW1, NW2 | NW3, NW4 (non-Northwell networks) |
| `billing_class` | professional; institutional to `_rejects` | the 26 MS-DRG rows, reason `drg_not_case_rate` |
| `negotiated_rate` | > 0 | $0 rows to `_rejects`, reason `zero_rate` (688 + 1,774) |
| `expiration_date` | kept as a column | nothing; it marks lapsed rates rather than filtering them |
| TIN | rows whose TIN is in `tin_system_map` | others stay in bronze |

Raw zips stay local, and bronze keeps the full parse as today: parse once, land
curated (architecture rule 1). The network filter is most of the reduction. NW1 and
NW2 account for about 9% of the NW1 file's price-by-provider-group references
(3,469,143 of 37,526,142), and NW3/NW4 for the rest. Post-filter price-row counts
were not measured in this pass; the first real parse should report them.

### 7.4 Order of work, after the freeze

1. Get the Anthem TOC link and finish step 4. If Anthem publishes a 32BJ in-network
   file, that is the comparison worth building, and it fits existing rules.
2. Split `32bj` from `1199` in the plan matcher. This is a label change with tests
   on real strings from section 3.
3. Build `tin_system_map.csv` and its test: every seeded EIN must resolve to the
   same system across vintages.
4. Write a parser test from the real MTS file (CLAUDE.md conventions: tests from real
   files), then land MTS/NW1 through the payer contract with the new columns.
5. Run the percent-of-Medicare view for 32BJ professional rates. Report locality
   coverage and exclusions (no modifiers, no setting) before any number.

---

## 8. Next: 32BJ-tier hospital rates side by side (plan, not run)

**Goal.** Show 32BJ-tier hospital facility rates, in dollars, for Montefiore, Mount
Sinai and NYU from `NY_HYBYMED32BJ`. Each rate sits next to the same facility's
standard Anthem rate. The output is a 32BJ page in `analyst_app.py` and a downloadable
workbook.

**Status.** This is a plan only. Nothing below has been built or extracted. Two
read-only structure scans were run on 2026-10-21 to answer the revenue-code question
(8.2): `rc_census.py`, 4 minutes, and `bundle_check.py`, 4 minutes.

### 8.0 Rules for this work

- **Branch.** Work happens on the local branch `explore/32bj`, created from `main`.
  `main` is used because `analyst_app.py` exists there. On `freeze/findings-page` it
  has already been renamed to `streamlit_app.py`.
- **Git.** Commits are local only. Nothing is pushed, no PR is opened, nothing is
  deployed, and `main` is not changed.
- **Memory.** Free RAM is checked before each long pass, and a watcher reports if it
  drops below 0.8 GB. If memory runs low, work stops and you are told. No process is
  killed. The one earlier stop was caused by WSL holding 6.3 GB, not by the profiler,
  which held 0.26 GB.
- **Data sources.** Public data only. Montefiore appears only through public
  registries, Anthem's public TiC file, and Montefiore's own published hospital file.

### 8.1 What the data allows (checked while planning)

**Inpatient DRG type by system.** No inpatient rate is ever compared across DRG
types.

| system | case-rate DRG type | `negotiated_type` | codes | also present |
|---|---|---|---:|---|
| Montefiore | APR-DRG | `derived` | 1,272 | 51 MS-DRG `per diem` (psych and rehab type) |
| Mount Sinai | MS-DRG | `derived` | 738 | a few MS-DRG `per diem` and `negotiated` |
| NYU | MS-DRG | `derived` | 730 | a few MS-DRG `per diem` and `negotiated` |

**APR-DRG codes.** Each code is four digits, base DRG plus severity, and the item also
carries a separate `severity_of_illness` field. For example, `0972` with severity `2`
is DRG 097 at severity 2. The file uses APR-DRG version 34.0. One row per DRG and
severity means 318 base DRGs × 4 severity levels.

**APC.** None of the three systems has an APC rate. Outpatient therefore means CPT and
HCPCS rows with `billing_class = institutional`.

**Campus columns.**

| system | campus | NPI |
|---|---|---|
| Montefiore | Montefiore Medical Center (**one Bronx column**: Moses, Weiler, Wakefield) | 1952476988 |
| Montefiore | Nyack | 1104808062 |
| Montefiore | New Rochelle | 1720414154 |
| Montefiore | Mount Vernon | 1992131320 |
| Montefiore | White Plains | 1447255153 |
| Montefiore | St. Luke's Cornwall | 1003923434 |
| Montefiore | St. Luke's Cornwall psych unit | 1124295746 |
| Montefiore | Einstein Advanced Care, Elmsford (outpatient) | 1134892847 |
| Montefiore | Schaffer Extended Care (SNF) | 1538595962 |
| Mount Sinai | The Mount Sinai Hospital | 1932103413 |
| Mount Sinai | Mount Sinai Queens (NPPES: Mount Sinai Hospital, Long Island City) | 1245248939 |
| Mount Sinai | Mount Sinai Brooklyn (NPPES: Beth Israel Medical Center, Brooklyn; two NPIs) | 1174768071, 1174689665 |
| Mount Sinai | South Nassau | 1922079094 |
| Mount Sinai | Diagnostic & Treatment Center | 1669476156 |
| Mount Sinai | psych, rehab, ESRD and SNF units | 13 NPIs |
| NYU | Tisch/Kimmel | 1669578324, 1801992631 |
| NYU | Long Island (Mineola) | 1427616002 |
| NYU | Brooklyn | 1619341716 |
| NYU | Long Island Community (Patchogue) | 1831910355 |
| NYU | NYU Hospitals Center | 1598864118 |
| NYU | oncology, psych, rehab, ESRD and mental-health units | 11 NPIs |
| Northwell | 12 acute hospitals (standard Anthem rates only) plus units: ESRD, psych, rehab, SNF, an ambulatory surgery center | 27 NPIs (see 8.10) |

The full list will be committed as `scripts/32bj/facilities.csv`, with NPPES name,
taxonomy and city for every NPI.

**Northwell's acute-care hospital NPIs** have institutional rates in the standard
Anthem file but **none in the 32BJ tier** (corrected; see 8.10). This plan first said
they had no institutional rates at all.

**Campus attribution.** In this file the billing "TIN" is an NPI, and each group lists
member NPIs.

- A rate belongs to a campus when that campus's NPI is the group's billing NPI.
- When a campus NPI is only a member of the group, the extraction records it
  separately and does not use it to assign a campus. The profile could not tell these
  two cases apart.
- Some rate entries reference several campuses at once, and the rate then applies to
  each campus named.
- 396 keys carry more than one distinct rate. These are shown as ranges (8.4).

**Expected results (from section 4).** These are expectations to confirm, not results
yet.

- Mount Sinai 32BJ rates are 0.917× standard (about 1/1.09) at The Mount Sinai
  Hospital, Queens and Brooklyn, and identical at the other Mount Sinai facilities.
- Montefiore and NYU 32BJ rates are identical to standard.

### 8.2 Revenue codes: what the 32BJ file does (scanned 2026-10-21)

**Question.** Are revenue-code (RC) rows standalone rates? Or are some CPT rates tied
to a revenue code through `bundled_codes`, modifiers, `additional_information` or
something similar?

**Short answer.**

- **RC rows are standalone rates.**
- **CPT/HCPCS rates are tied to revenue codes in one way only:** a bundle's list of
  revenue codes says which revenue codes the CPT rate applies under. The rate itself
  never varies by revenue code.

**1. Standalone RC rates.**

- **Volume.** 389 RC items, 380 `ffs` and 9 `bundle`. They carry 11,748 institutional
  prices.
- **What they are:** room, board and unit per diems, emergency room charges,
  percent-of-charge items, and so on.

| RC rows | negotiated type | setting | prices |
|---|---|---|---:|
| all | `percentage` | outpatient | 5,039 |
| all | `per diem` | outpatient | 3,836 |
| all | `per diem` | inpatient | 1,387 |
| all | `percentage` | both | 462 |
| all | `negotiated` | both | 312 |
| all | `percentage` | inpatient | 309 |
| all | `negotiated` | outpatient | 284 |
| all | `per diem` | both | 71 |
| all | `negotiated` | inpatient | 48 |

Examples:

- 0820–0889: dialysis per diems
- 0905/0906: intensive outpatient psychiatric care per diems
- 0450–0459: emergency room, `negotiated`, setting `both`
- 0762: observation, `negotiated`

**2. CPT/HCPCS bundles.**

- **Volume.** 5,193 CPT/HCPCS items are `negotiation_arrangement: bundle`: 3,007 CPT
  and 2,186 HCPCS. Their `bundled_codes` list revenue codes: 131,497 CPT→RC entries
  and 52,403 HCPCS→RC entries.
- **One bundle per code.** Every bundled code has **exactly one** bundle item, so
  exactly one RC list. No code has two bundles with different RC lists.
- **List size.** The lists are long: a median of 42 revenue codes, maximum 70. For
  example, CPT 72133 (lumbar CT) lists 0260, 0320–0352 and others, plus 0724
  (birthing center).
- **Clinically broad.** The lists cover far more than the matching department, so
  they are eligibility lists, not RC-specific contracts.
- **Separate from fee-for-service.**
  - The same code also has an ordinary `ffs` item, but for **different provider
    groups**. Of 360,756 institutional bundle prices (counted per group), 360,726 had
    no `ffs` price for the same code and group, and 10 had an equal one.
  - **None differed.** A facility's rate for a code is either a bundle rate with an
    RC list or a plain rate. It is never one price per revenue code.
- **Other bundled codes.** 1,395 bundle items also bundle non-RC codes: CPT→CPT 538,
  HCPCS→CPT 6,466, HCPCS→HCPCS 387, CPT→HCPCS 32, CPT→ICD 21. These are packaged
  services.

**3. No other RC link.**

- `additional_information` does not appear anywhere in the file.
- `billing_code_modifier` appears only on professional rows (50, 62, 80, AS, 26, TC,
  RR, NU and so on).
- Institutional rows never carry a modifier, and no modifier is a revenue code.
- `service_code` is present on every price.

**What this means for the RC rules you set.**

- **RC-only rates** are shown as their own code type, RC, with their setting and
  methodology. Most are per diem or percent of charges, so most go to the per diem
  and percentage tables. Only 644 RC prices are dollar `negotiated`.
- **RC+CPT pairs** become "this CPT's bundle rate, valid under this RC". When you
  paste a pair (RC, CPT), the page:
  1. looks up the CPT rate for each campus;
  2. if the rate is a bundle and the RC is on its list, shows the rate labelled
     **"bundle rate; RC listed"**;
  3. if the rate is a bundle and the RC is **not** on its list, shows the rate
     flagged **"RC not listed for this code"**. The rate is not hidden, because the
     contract does not say what happens then.
  4. if the rate is a plain rate, shows it labelled **"not specific to revenue
     code"**.
- **No RC-specific CPT price will appear,** because the file has none. The page says
  this in its definitions, so a blank is not read as missing data.

### 8.3 Extraction: all facility rows, to Parquet

**Script.** `scripts/32bj/extract_facility_rates.py`.

- **Inputs:** `NY_HYBYMED32BJ` (about 35 minutes; reading provider references is the
  slow part) and `NY_HXRLMED0000_06_06` (about 2 minutes).
- **One file at a time,** with the memory rule in 8.0.
- **Streaming:** ijson, with the `read(0)`-safe cut at `"in_network"` for provider
  references.

**Rows kept.** Every non-professional price whose provider group has a billing NPI or
member NPI in `facilities.csv`. That covers all of Montefiore, Mount Sinai, NYU and
the Northwell units. Every code type is kept: APR-DRG, MS-DRG, CPT, HCPCS, RC, APC
and CSTM-ALL.

**Output.** `data/raw/32bj/anthem/derived/facility_rates.parquet`, one row per
(source file, rate entry, price, group). Columns:

- **Source:** `source_file`, `source_vintage` (2026-10-01).
- **Network:** `network_names` (list), `is_32bj_tier` (the list includes
  `NY Preferred Facility Tier1  32BJ`).
- **Provider:** `group_id`, `billing_npi`, `member_facility_npis`, `system`,
  `campus`, `attribution` (`billing` or `member`).
- **Code:** `billing_code_type`, `billing_code`, `billing_code_type_version`,
  `severity_of_illness`, `description`.
- **Arrangement:** `negotiation_arrangement`, `bundled_rcs` (list), `bundled_other`
  (list).
- **Price:** `billing_class`, `negotiated_type`, `setting`, `service_code`,
  `modifier`, `negotiated_rate`, `expiration_date`.

Expected size is under 20 MB; the profile's narrower CSV was 1.6 MB.

**DuckDB.** All queries run in DuckDB over the Parquet. DuckDB is installed locally but
is **not yet a declared dependency**, so it is added to `pyproject.toml`.

**What counts as each rate.**

- **32BJ-tier rate:** a row in `NY_HYBYMED32BJ` with `is_32bj_tier`.
- **Standard Anthem rate:** the same billing NPI and key in `NY_HXRLMED0000_06_06`,
  network `HMO POS PROVIDERS`, same 2026-10-01 vintage.
- **HMO-only rows inside the 32BJ file** (for example NYU's 3,878) are kept and
  labelled, but they never fill a 32BJ-tier cell.

### 8.4 Comparison rules

- **Dollars only.** Case-rate and fee tables use `negotiated`, `fee schedule` and
  `derived`. `derived` is labelled; Anthem uses it for its DRG case rates.
  - **Per diem** rates go to their own table.
  - **Percentage** (percent of charges) rates go to a counts-and-examples table.
    They have no dollar value without the hospital's chargemaster (CLAUDE.md,
    methodology heterogeneity).
- **Keys.**
  - **Inpatient:** (code type, code, severity for APR-DRG, setting `inpatient`).
  - **Outpatient:** (code type, code, setting `outpatient` or `both`).
  - **Price detail:** `service_code` is kept but not used as a key unless two rates
    for the same code differ only by it. Institutional rows carry no modifiers
    (8.2).
- **Several rates for one key.** The cell shows `min – max`, with the count and a ⚑
  flag. No single value is ever picked silently.
- **Discount.** Each cell shows `32BJ ÷ standard`, and each system gets a median.
- **No 32BJ-tier row.** If a campus has no 32BJ-tier row for a code, the cell shows
  "—", not the standard rate.

### 8.5 "Most common": decided sources

**APR-DRG.** NY SPARCS *Hospital Inpatient Discharges (De-Identified)*, latest
available year on health.data.ny.gov.

- **Filter:** `Payment Typology 1 = Private Health Insurance`.
- **Count:** discharges by APR-DRG code × APR severity.
- **Top 25:** the 25 DRG-and-severity pairs.

**MS-DRG.** CMS *Medicare Inpatient Hospitals – by Provider and Service*, latest year.

- **Filter:** NY providers.
- **Count:** `Tot_Dschrgs` summed by MS-DRG.
- **Top 25:** the 25 largest.
- **Caption:** "Ranked by Medicare discharge volume in New York. This leans toward
  conditions common in people over 65; commercial volume (for example childbirth)
  is under-represented."

**Outpatient.**

- **Candidate codes:** the CPT and HCPCS codes in CMS's 70 shoppable services.
- **Ranking:** NY volume in CMS *Medicare Physician & Other Practitioners – by
  Geography and Service*, `Rndrng_Prvdr_Geo_Desc = New York`,
  `Place_Of_Srvc = F` (facility), summed `Tot_Srvcs`.
- **Top 25:** the 25 highest-volume codes.
- **Shoppable codes with no NY facility volume** rank last.

**The same rule for all three.** A code makes the top 25 on volume alone. If some
campus has no rate for it, that campus's cell shows "—". Codes are not dropped to
make the table look complete.

**Sources and vintages.** The downloaded volume files go to
`data/raw/32bj/volume/`. Their URLs and years are recorded in the page's
data-sources section.

### 8.6 Cross-check: St. Luke's Cornwall, hospital file against Anthem file

**Hospital side.** Lake rows for EIN 141340054, NPI 1003923434, with payer `Anthem` /
plan `Local 32BJ`, `file_vintage` 2026-04-01.

| code type | setting | methodology | rows |
|---|---|---|---:|
| APR-DRG | inpatient | case rate (dollar) | 1,244 |
| APR-DRG | inpatient | case rate (algorithm only) | 12 |
| CPT | outpatient | fee schedule | 2,899 |
| CPT | outpatient | case rate | 129 |
| HCPCS | outpatient | fee schedule | 929 |
| CDM | — | percent of charges | 1,875 |
| RC | — | percent of charges or case rate | 18 |

`billing_class` is null throughout. APR-DRG codes use the same four-digit form
(`0011`).

**Anthem side.** St. Luke's Cornwall 32BJ-tier rows from 8.3: billing NPI 1003923434,
plus its psych unit 1124295746.

**Match rules:**

- APR-DRG case rate ↔ APR-DRG `derived`, on the four-digit code.
- CPT/HCPCS fee schedule or case rate ↔ `negotiated`, `fee schedule` or `derived`, on
  the code, setting outpatient.
- RC ↔ RC, on the code.

**Reporting.**

- **Outcomes.** Each match is reported in one of four outcomes, by code type:
  - exact to the cent
  - within 1%
  - other (with the ratio)
  - present on one side only
- **Largest disagreements.** The 20 largest disagreements are listed.
- **Ratio distribution.** The distribution of ratios is shown, because a single
  uniform ratio means an escalator between April and October, not a disagreement.

**Hazards stated with the result:**

- **Vintage.** The hospital file is 2026-04; Anthem is 2026-10-01.
- **No billing class on the hospital side.** The hospital file omits it. Matching on
  code type and setting is the explicit rule here, narrower than ADR 0005.
- **The 12 algorithm-only DRG rows** are listed but not compared.

**Output.**

- `derived/st_lukes_crosscheck.csv`
- a short result paragraph in this doc

This runs before any table is published. If the two sources disagree, the page
carries the caveat.

### 8.7 The 32BJ page in `analyst_app.py`

**Code layout.** The layout follows the existing pattern: logic in a module with no UI
and with tests, and layout in the page.

- **`src/pipeline/thirtytwobj_view.py` (logic).**
  - loading Parquet through DuckDB
  - the top-25 tables
  - the 32BJ ÷ standard ratios
  - code parsing and normalisation
  - workbook building
- **`analyst_app.py` (layout).** One new page function, `thirtytwobj_page`,
  registered with `st.Page(..., title="32BJ", url_path="32bj")`.
- **Tests.** `tests/pipeline/test_thirtytwobj_view.py`. Its fixtures are a small slice
  of real extracted rows, per CLAUDE.md: tests from real files.

**Data on the page.**

- **Where it reads from.** The page reads `RECKONER_32BJ`, defaulting to
  `data/raw/32bj/anthem/derived/`.
- **When the data is missing.** It says plainly that the 32BJ data is not present,
  for example on Community Cloud, where nothing is deployed.
- **What this branch does not do.** It adds no network call and does not change
  `requirements.txt`.

**Merge note.** `freeze/findings-page` deletes `analyst_app.py`. The page function is
kept self-contained, so moving it into `streamlit_app.py` later is a cut and paste.

**Page layout, top to bottom:**

1. **Plain-English summary.** The draft below is checked against the extracted
   numbers before it ships. It is precise about campuses, because the discount is
   not system-wide.

   > 32BJ members using Anthem have their own hospital tier, "NY Preferred Facility
   > Tier1 32BJ". At three Mount Sinai hospitals (The Mount Sinai Hospital, Mount
   > Sinai Queens and Mount Sinai Brooklyn), 32BJ rates are about 8% below Anthem's
   > standard rates for the same service. Mount Sinai's other facilities, and every
   > Montefiore and NYU Langone hospital, are at the standard Anthem rate.
   > Northwell's 12 main hospitals are not in the 32BJ tier: Anthem lists them only
   > at its standard rates. Some Northwell psychiatric units, dialysis centers, a
   > rehabilitation unit and an ambulatory surgery center are in the 32BJ tier. 32BJ
   > reaches Northwell's hospitals through a separate direct contract (Northwell
   > Direct).

2. **Default view: top-25 tables, each with a CSV and Excel download.**
   - **Inpatient APR-DRG (Montefiore only).** One row per DRG and severity. One 32BJ
     column per campus, with standard alongside.
   - **Inpatient MS-DRG (Mount Sinai and NYU).** One row per MS-DRG. Columns for
     each campus.
   - **Outpatient CPT/HCPCS.** All three systems, with a methodology label per
     cell.
   - **Rules.** Dollars only, and never across DRG types. Several rates show as
     `min – max ⚑`. Per diem, percentage and RC-only rates sit in collapsed tables
     below.

3. **Filters (sidebar).** These apply to every table and download:
   - system
   - campus (dependent on system)
   - inpatient / outpatient
   - code type (APR-DRG, MS-DRG, CPT, HCPCS, RC)
   - network (32BJ tier, standard, both side by side)

4. **Code selection.** Pasted codes and picked codes are combined. When any are
   chosen, the tables show those codes instead of the top 25. This is specified in
   8.7.1.

5. **Downloads.**
   - The filtered table, as Excel and as CSV.
   - A findings workbook (Excel) with three sheets:
     - *Summary:* the plain-English text plus per-system medians of 32BJ ÷ standard.
     - *Definitions.*
     - *Data:* the filtered rows, one rate per row with every label.
   - Excel is written with openpyxl, already a project dependency.

6. **Definitions, data sources and limits,** written for a non-technical reader:
   - **Definitions:** what a 32BJ-tier rate is; what "standard Anthem rate" means;
     DRG versus APR-DRG and severity; case rate, per diem and percent of charges;
     what ⚑ means; what "not specific to revenue code" means.
   - **Data sources:** the Anthem file names and dates; SPARCS, CMS and shoppable
     list years.
   - **Limits:**
     - the Bronx campuses are one column
     - Northwell's acute hospitals are not in the 32BJ tier (standard Anthem only)
     - the hospital file is from April and the Anthem file from October
     - percent-of-charges rates have no dollar value
     - Medicare volume skews toward older patients

#### 8.7.1 Code selection rules

**Paste box.**

- Accepts a column copied from Excel, up to several thousand codes.
- Splits lines on newlines.
- Splits columns on tabs, falling back to commas.
- Splits remaining tokens on spaces only when a line has no tab or comma. This is
  because `139 4` might be APR-DRG 139 at severity 4. That case is detected and
  offered as an ambiguity, not split silently.

**Two columns.**

- If the first value looks like a revenue code (8.7.2), the line becomes an RC+CPT
  pair, matched as in 8.2.
- Otherwise only the first value is used.

**Cleaning.** Trim, uppercase, drop blanks, and drop header-like tokens: any token
with no digit, or words like "code", "CPT", "HCPCS", "DRG". Remove duplicates after
normalisation.

**Summary after pasting:**

- **pasted:** tokens kept after removing blanks and headers
- **found:** at least one rate in the current filters
- **not found**
- **duplicates removed**
- **ambiguous:** more than one code type matched in the data

Not-found and ambiguous codes are listed, each with a CSV download.

**Picker.** A searchable multi-select of every code with its description. Its
selections are added to the pasted set.

#### 8.7.2 Type detection and leading zeros

**Approach.** Each token generates every candidate it could be. The candidates are
then checked against the codes actually in the data.

- Exactly one hit: that type is used.
- More than one hit: every hit is shown, flagged ambiguous.
- No hit: the code is not found.

The page never guesses.

| pasted | candidates generated | note |
|---|---|---|
| 5 digits, e.g. `72133` | CPT | |
| 4 digits + letter, e.g. `0223U`, `0042T` | CPT (category II/III, PLA) | |
| letter + 4 digits, e.g. `G0300` | HCPCS | |
| `139-4`, `139.4`, `139 4` | APR-DRG `1394` | DRG with severity |
| 4 digits, first digit not 0, e.g. `1394` | APR-DRG `1394`; CPT `01394` (anesthesia) | ambiguous only if both exist in the data |
| 4 digits starting with 0, e.g. `0450`, `0011` | RC `0450`; APR-DRG `0011` | `0011` could be an RC or DRG 001 at severity 1, so it is flagged if both exist in the data |
| 3 digits, e.g. `470` | MS-DRG `470` for Mount Sinai/NYU; APR-DRG base `470` (all four severities) for Montefiore; RC `0470` | each system is matched to its own DRG type; RC is flagged if it also exists |
| 1–2 digits, e.g. `39` | MS-DRG `039`; RC `0039`; CPT `00039` | leading zeros restored; flagged if more than one exists |
| `100` | MS-DRG `100`; RC `0100`; CPT `00100` | the anesthesia code `00100` exists; flagged if more than one exists |

Two of your examples need care:

- **`450` → `0450`.** `450` is also MS-DRG 450. Under the rule above it is flagged
  ambiguous rather than silently turned into the emergency-room revenue code.
- **`39` → `039`.** This is applied as MS-DRG 039 when only that exists in the data.

### 8.8 Moving the scratchpad into the repo

**Scripts → `scripts/32bj/` (tracked; committed locally on `explore/32bj`).**

| scratchpad file | role |
|---|---|
| `profile_tic.py` | Healthcare Highways zips |
| `profile_anthem.py` | Anthem .gz |
| `anthem_summary.py` | |
| `rc_census.py` | |
| `bundle_check.py` | |
| `ein_lookup.py` | |
| `npi_refs.py` | |
| `extra_refs.py` | |
| `hosp_tins.py` | |
| `pos_pairs.py` | |
| `payer_strings.py` | |
| `codes.py` | |
| `match.py` | |
| (new) `extract_facility_rates.py` | |
| (new) `crosscheck_st_lukes.py` | |
| (new) `volume_sources.py` | |
| (new) `facilities.csv` | |

One-off probes are dropped: `peek.py`, `census.py`, `rawscan.py`, `inst.py`,
`npi_sample.py`.

**Changes when moving:**

- Scratchpad paths become arguments.
- Type hints are added.
- The target-NPI and EIN lists move into one module that every script imports.

**Data → `data/raw/32bj/anthem/derived/` (Anthem) and `data/raw/32bj/derived/`
(Healthcare Highways and lake searches).** Both are gitignored, so they stay local.

- **Anthem:** profiles, `_inst.csv.gz` files, `rc_census_32bj.json`,
  `bundle_check_32bj.json`, `anthem_target_npis.json`.
- **Caches:** `nppes_cache.json`, `npi_refs.json`, `extra_refs.json`,
  `ein_names.json`.
- **Healthcare Highways and lake searches:** `profile.json`, MTS/NW1 rate extracts,
  payer-name CSVs, `oon_npis.json`.

Moving them keeps them past the end of the session. It is not a backup.

### 8.9 Order of work and checkpoints

1. **Move the scratchpad (8.8) and commit the scripts.** Local commit.
2. **Write `facilities.csv`, then run `extract_facility_rates.py`.** The 32BJ file
   first, then part 06, one at a time, with the memory watcher. Check row counts
   against the profile: about 1.67M institutional prices in the 32BJ file, before
   filtering to these systems.
3. **Run the St. Luke's cross-check (8.6).** The result goes in this doc.
   **Checkpoint:** report to you before step 5 if the sources disagree.
4. **Fetch the volume sources (8.5)** and build the three top-25 lists.
5. **Build `thirtytwobj_view.py` with tests, then the page.**
   - Run `pytest` and the existing app tests.
   - Run the app locally to see it, without deploying.
6. **Make a local commit per step.** No push.

### 8.10 Build status, 2026-10-21 (night): stopped at the cross-check

**Built.** All of it is local on `explore/32bj`, with nothing pushed.

- **Extraction.**
  - `extract_facility_rates.py` ran on both Anthem files.
  - The 32BJ file: 1,669,287 non-professional prices, exactly the profile's count.
    Its provider references expand to 114,108,476 provider-group rows; the HMO
    file's expand to 785,905. Pass 1 took 25 minutes and pass 2 12 minutes.
  - The HMO file: 1,679,455 non-professional prices.
- **Facilities: 94 across the four systems.**
  - `build_facilities.py` resolved every billing NPI with a non-professional price
    in NPPES: 1,385 of them. 94 belong to the four systems.
  - This found campuses the earlier name search missed:
    - Mount Sinai: Morningside/West (billing as St. Luke's-Roosevelt), New York Eye
      and Ear, and a second NPI for The Mount Sinai Hospital
    - Montefiore: Burke Rehabilitation, and St. Luke's Cornwall's dialysis unit
    - NYU: a second Long Island Community Hospital NPI
- **Tidy table.** `facility_rates.parquet`: 1,568,115 rows, 7.6 MB, built in 26
  seconds under a 1 GB DuckDB cap.
  - **Structure.** It streams, with no `DISTINCT`: the first attempt ran out of memory
    under the cap. Revenue-code lists sit in `facility_items.parquet`.
- **Top-25 lists** from SPARCS 2023 and CMS 2024 (`volume_sources.py`).
  - **APR-DRG** is led by normal newborn (640), vaginal delivery (560) and cesarean
    (540).
  - **MS-DRG** is led by sepsis (871), heart failure (291) and pneumonia (177).
  - **Outpatient** is led by head CT, abdomen/pelvis CT, cataract surgery and
    screening mammography.
  - **No facility volume.** 22 of the shoppable codes have no NY facility-setting
    volume (labs, consults, preventive visits). They rank last.
- **The page and its logic.** These were written before the cross-check ran, which is
  earlier than 8.9 orders. They are **not committed**, because the cross-check gates
  them.
  - `src/pipeline/thirtytwobj_view.py`
  - the 32BJ page in `analyst_app.py`
  - `tests/pipeline/test_thirtytwobj_view.py`: the 13 parsing and labelling tests
    pass; the 7 data tests wait for a fixture.

**Northwell, confirmed from the full extraction.**

- **Acute hospitals.** All 12 have only standard Anthem rates, about 11,400 rows
  each, and no rows at all in the 32BJ file:
  - NSUH, LIJ, Cohen Children's, LIJ Valley Stream, Lenox Hill, SIUH
  - Glen Cove, Huntington, Plainview, South Shore, Phelps, Northern Westchester
- **Units in the 32BJ tier.**
  - psych units: SIUH, Huntington, South Shore, NSUH Syosset, Glen Cove, LIJ Valley
    Stream
  - dialysis centers: NSUH Manhasset, LIJ New Hyde Park, LIJ Queens Village
  - SIUH's rehab unit
  - the Northern Westchester ambulatory surgery center
- **Corrected.** Section 4, 8.1 and the page summary now say this. The earlier "no
  acute facility rates in Anthem" came from the profile's NPI matching and was
  wrong.

**St. Luke's Cornwall cross-check: the sources disagree on outpatient rates.**

| code type | on both sides | exact | within 1% | differ | median Anthem ÷ hospital |
|---|---:|---:|---:|---:|---:|
| APR-DRG (inpatient case rates) | 1,244 | 609 | 635 | **0** | 1.000 |
| CPT (outpatient) | 1,913 | 88 | 842 | **983** | 1.000 (5th–95th pct 0.84–3.49) |
| HCPCS (outpatient) | 78 | 2 | 9 | **67** | 1.754 |

The "within 1%" rows are presumably rounding.

- **Inpatient agrees completely.**
- **Outpatient disagrees on about half of CPT codes and most HCPCS codes.** Where they
  differ, Anthem is usually higher: 816 higher against 167 lower. The gap is large
  for diagnostic tests and imaging, for example:

  | code | test | hospital file | Anthem |
  |---|---|---:|---:|
  | 92587 | auditory evoked test | $36.03 | $755.09 |
  | 77470 | special radiation treatment | $368.39 | $5,588.82 |
  | 75625 | angiography | $905.02 | $6,515.89 |

- **What it is not.**
  - Not the 32BJ tier specifically: St. Luke's 32BJ and standard Anthem rates are
    identical.
  - Not bundle against plain rates: Anthem's bundle rates split about evenly between
    agreeing and differing, and 71 plain-rate CPT codes differ too.
  - Not vintage alone: a 6-month escalator would be a uniform ratio, not a spread
    from 0.84 to 3.5.
- **Code-level detail:** `data/raw/32bj/anthem/derived/st_lukes_crosscheck.csv`.

**Stopped here for your decision.** The tables, fixture and page are not finished,
because the outpatient tables would show Anthem's rates for codes where St. Luke's
own file says something different. Options:

1. **Publish with a caveat.** Inpatient (which agrees) as planned. Outpatient labelled
   "Anthem's published rate; St. Luke's own file disagrees on about half of outpatient
   codes".
2. **Investigate first** which side is the facility rate. Two cheap tests:
   - compare St. Luke's hospital-file 32BJ outpatient amounts with Anthem's
     *professional* rates for the same codes. A match would mean the hospital file's
     32BJ outpatient column is professional. This needs a re-scan of the 32BJ file's
     professional rows, about 35 minutes.
   - compare both sides with Medicare OPPS payment for the same codes.
3. **Inpatient only for now.** Hold the outpatient tables until it is explained.

**Decision, 2026-10-21: options 3 and 2.**

- **Option 3 done.** The page shows inpatient tables only. Outpatient sits behind one
  switch, `OUTPATIENT_UNDER_REVIEW` in `src/pipeline/thirtytwobj_view.py`, with the
  note "Outpatient rates under review: they don't yet match the hospital's own file."
  The switch hides outpatient everywhere it could appear:
  - the top-25 tables
  - pasted and picked codes
  - the per diem, percentage and RC tables
  - the downloads
- **Option 2's first test run** (below).

**Mount Sinai, corrected from the full extraction.** With every billing NPI resolved
and campuses assigned by billing NPI, all six Mount Sinai acute campuses carry
inpatient MS-DRG case rates at exactly 0.917× standard in the 32BJ tier:

- The Mount Sinai Hospital, Queens and Brooklyn
- Morningside/West, New York Eye and Ear, and South Nassau

Section 4's "South Nassau identical" held only for the outpatient fee-schedule rows
that profile compared: those are at 1.000. Montefiore's seven campuses and NYU's four
are at 1.000 on every inpatient code compared.

**The professional-fee test: the explanation does not hold.**

- **Method.**
  - `rescan_professional.py` streamed the 32BJ file once more, in 17 minutes.
  - It kept every **professional** price on the 4,567 provider references that
    include St. Luke's (as billing NPI): 82,727 prices over 9,963 codes.
  - It also kept every distinct professional rate Anthem publishes for those codes,
    for **any** provider.
  - DuckDB stayed under the 1 GB cap, and nothing was killed.
- **Results.**

  | St. Luke's 32BJ outpatient code (hospital file) | codes | equals a St. Luke's professional rate | within 1% of one | equals any Anthem professional rate |
  |---|---:|---:|---:|---:|
  | disagrees with Anthem's facility rate | 1,050 | 0 | 0 | 0 |
  | agrees with Anthem's facility rate | 941 | 0 | 0 | 0 |

- **Not a fixed multiple either.** The hospital-file amounts are about **10× St.
  Luke's professional rates**: median 10.7×, 10th–90th percentile 7.1–14.3×, against
  the office-setting (POS 11), no-modifier professional rate.

So the disagreeing amounts in St. Luke's own file are facility-scale prices, not
professional fees. They simply differ from Anthem's facility price for the same code.

**What is left to explain it, untested:**

- **The other comparison benchmark.** Compare both sides with Medicare OPPS payment
  for the same codes. If one side tracks a fixed percentage of OPPS, it is likely the
  contract's actual basis.
- **Vintage, but not as a uniform escalator.** A mid-year contract change between
  April and October that re-priced some outpatient categories (diagnostic tests and
  imaging stand out) and not others would look like this.
- **A different fee schedule in the hospital file.** Which of the hospital's
  fee-schedule versions it published under the Local 32BJ label cannot be told from
  the file alone.

Outpatient stays hidden until you decide. Nothing has been added back.

### 8.11 Shipped, 2026-10-22: the 32BJ page is live (inpatient only)

Decision, 2026-10-22: ship the inpatient page to the public repo and the live app.
This overrides the freeze for this page and the earlier no-push rule.

**Where it runs.** Page **32BJ** at <https://reckoner-ny-2-0.streamlit.app/32bj>,
LIVE_STATUS

**What it reads.** `summary/32bj/` is committed. It holds:

| file | size | content |
|---|---:|---|
| `facility_rates.parquet` | 7.6 MB | 1,568,115 rows |
| `facility_items.parquet` | 0.2 MB | bundle RC lists |
| `top25.csv` | | the code rankings |
| `st_lukes_crosscheck.json` | | the cross-check outcome |

`.gitignore` re-includes only `summary/32bj/*.parquet`. The raw Anthem downloads
and every intermediate file stay under `data/`, which git ignores.
`requirements.txt` gains `duckdb` and `openpyxl`, which Community Cloud installs.
Outpatient stays off (`OUTPATIENT_UNDER_REVIEW = True`).

**Checked in a real browser before pushing** (Playwright, local):

- both inpatient top-25 tables load, and the outpatient note shows
- the network filter re-renders without errors
- 20 pasted codes resolve: 19 counted (the word `abc` is dropped as a header), 17
  found, 1 not found, 1 duplicate, 4 ambiguous and flagged
- the Excel download opens

One display bug was fixed. Excel cells held rates as text (`"$11,624.37"`), so they
could not be summed or sorted. They are now numbers with a currency format.

**Independent check** (a separate agent that did not build the page, with read-only
SQL of its own):

- **10 random cells from the page against the Parquet: 10/10 exact.**
- **Mount Sinai 0.917×:** every compared MS-DRG case rate at all six campuses is at
  0.9167 (Brooklyn on its 11 comparable codes).
- **Montefiore and NYU 1.000×:** Montefiore is at 1.000 on every code at all seven
  acute campuses. NYU is at 1.000 everywhere it can be compared.
- **Northwell:** 0 tier rows at the 12 acute NPIs, and 11 unit campuses in the tier.
- **The commit:** no raw files, secrets or non-public data. The largest blob is the
  7.6 MB Parquet.

**What the check found, and what changed:**

1. **The summary stated "Northwell Direct" as fact.** The Anthem files show only
   that Northwell's acute hospitals are absent from the tier, not why. The sentence
   now says "The Anthem files do not say how 32BJ members reach Northwell's main
   hospitals." The press-release fit (section 4) stays an inference, here only.
2. **St. Luke's Cornwall's psychiatric unit (NPI 1124295746) was an acute campus.**
   It was a duplicate APR-DRG column beside the hospital's.
   - `build_facilities.py` now leaves a labelled `... Unit` taxonomy out of acute.
   - The rates file was rebuilt. Only labels changed: 0 rates differ, and 19,980
     rows changed flag.
   - The same rebuild fixed "St. Luke'S" (a `str.title` artifact).
3. **Few codes can be compared at three campuses, and the summary now says why for
   each.**
   - **Mount Sinai Brooklyn (18) and NYU Langone Long Island Community (9)** bill
     under two NPIs with different rates. Most cells are ranges (⚑), with no ratio.
   - **NYU Langone Long Island (Mineola)** is different. The 32BJ tier lists dollar
     inpatient rates for only 22 of its DRGs. Its case rates appear only in standard
     Anthem and in the HMO network inside the 32BJ file. (The checker said it had no
     tier rows at all. It has 14,468, almost all outpatient.)
4. **The cross-check caption blended inpatient and outpatient** (67.5%). It now
   reads: 1,244 of 1,244 inpatient case rates agree, but only 941 of 1,991
   outpatient codes (47%). `crosscheck_st_lukes.py` writes both, as `by_setting`.

## Reproducing this

The profiling scripts lived in a session scratchpad, not the repo; they read only
the downloaded zips, the local lake, and public APIs:

- a stream profiler: `zipfile.open` into `ijson.items` for `provider_references.item`
  and `in_network.item` / `out_of_network.item`
- ProPublica EIN lookups (`/nonprofits/api/v2/organizations/{ein}.json`), 2,718 EINs,
  4 concurrent requests
- NPPES lookups (`npiregistry.cms.hhs.gov/api/?version=2.1`): organisation searches
  per system, a 3-NPI sample from each of the top 30 MTS TINs, a 300-NPI random MTS
  sample, and all 127 OON NPIs
- DuckDB over `data/lake/curated/hospital_rates` for the string search and code
  overlap

If this proceeds, the profiler should become `scripts/profile_tic.py`, with its test
built from `PS_SP1_1.json`, the 21 KB real file.
