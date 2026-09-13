# Coverage matrix

Generated 2026-09-13 from the local lake and payer parquet. Read-only; no data was modified.

## Scope

The reconciliation covers four systems; the other eight are hospital-side only by
design. See [scope.md](scope.md), which also records that only Mount Sinai supports
pairwise reconciliation, and the Empire NPI-only caveat that any carrier-to-carrier
comparison must carry.

## What this can and cannot answer

**The hospital lake carries no NPI and no EIN.** Its columns are hospital name, location name, codes, payer/plan strings and rates — there is no provider identifier anywhere in it. NPIs and EINs exist only in `mrf_pipeline/target_providers.csv` (940 NPIs) and `target_tins.csv` (120 rows marked `include=Y`), and both key to **payer-side system names**, not to the lake's hospital names. So identifiers are reported per system below, not per hospital, because the per-hospital join does not exist in the data.

**The two sides are bridged by name alone.** The mapping used is stated explicitly rather than inferred, since a wrong pairing would invent coverage:

| hospital lake | payer system |
|---|---|
| Mount Sinai Health System | Mount Sinai |
| NYU Langone Health | NYU Langone |
| NewYork-Presbyterian | NYP |
| Northwell Health | Northwell |

Every other hospital has no payer-side counterpart **because it was never a payer-side target**, not because reconciliation failed against it.

## 1. Hospital lake

| hospital | rows | distinct payers | distinct plans | distinct codes¹ | payer system | NPIs² | TINs² |
|---|---:|---:|---:|---:|---|---:|---:|
| Northwell Health | 88,964,063 | 31 | 98 | 20,415 | Northwell | 14 | 42 |
| NYC Health + Hospitals | 23,611,314 | 41 | 53 | 25,513 | — | — | — |
| Catholic Health System (Buffalo) | 17,113,618 | 93 | 260 | 658 | — | — | — |
| NYU Langone Health | 13,459,491 | 22 | 415 | 6,408 | NYU Langone | 111 | 24 |
| University of Rochester Medical Center | 4,964,600 | 116 | 188 | 15,176 | — | — | — |
| NewYork-Presbyterian | 2,778,112 | 29 | 10 | 5,752 | NYP | 73 | 9 |
| Mount Sinai Health System | 1,831,630 | 27 | 389 | 23,317 | Mount Sinai | 305 | 33 |
| Upstate University Hospital | 1,355,583 | 19 | 28 | 2,568 | — | — | — |
| Ellis Medicine | 899,212 | 17 | 17 | 766 | — | — | — |
| Rochester Regional Health | 841,244 | 24 | 28 | 16,481 | — | — | — |
| Maimonides Medical Center | 449,204 | 41 | 21 | 17,925 | — | — | — |
| Crouse Health | 216,206 | 17 | 25 | 2,831 | — | — | — |
| **total** | **156,484,277** | | | | | | |

¹ distinct `(code, code_type)` for CPT, HCPCS and MS-DRG only — the code systems both sides publish.  
² from the payer-side target lists, by system; the lake itself has neither.

## 2. Payer data

| carrier | files | rows | distinct NPIs | matched to a system | distinct TINs | matched to a system | systems reached |
|---|---:|---:|---:|---:|---:|---:|---|
| UHC | 6 | 24,445,054 | 258 | 258 | 99 | 99 | Montefiore, Mount Sinai, NYP, NYU Langone, Northwell, WMC, White Plains |
| AetnaALIC | 6 | 22,407,168 | 119 | 119 | 113 | 113 | Montefiore, Mount Sinai, NYP, NYU Langone, Northwell, WMC, White Plains |
| Empire | 4 | 3,871,901 | 127 | 127 | — | — | Montefiore, Mount Sinai, NYU Langone, Northwell, WMC, White Plains |
| Cigna | 5 | 3,743,010 | 86 | 86 | 107 | 107 | Montefiore, Mount Sinai, NYP, NYU Langone, Northwell, WMC, White Plains |
| Aetna | 1 | 1,324,087 | 125 | 125 | 109 | 109 | Montefiore, Mount Sinai, NYP, NYU Langone, Northwell, WMC, White Plains |
| Emblem | 98 | 993,195 | 34 | 34 | 28 | 28 | Montefiore, Mount Sinai, NYP, NYU Langone, Northwell, WMC, White Plains |

Row counts exclude the two Cigna files the contract marks as row-for-row duplicates.

## 3. Intersection — shared billing codes

Ranked by shared `(code, code_type)` across CPT, HCPCS and MS-DRG.

| # | hospital | carrier | shared codes | hospital codes | carrier codes | share of hospital |
|---:|---|---|---:|---:|---:|---:|
| 1 | Mount Sinai Health System | Empire | **7,843** | 23,317 | 18,469 | 34% |
| 2 | Northwell Health | Empire | **7,007** | 20,415 | 17,441 | 34% |
| 3 | Mount Sinai Health System | AetnaALIC | **6,813** | 23,317 | 18,617 | 29% |
| 4 | Mount Sinai Health System | Aetna | **6,748** | 23,317 | 18,402 | 29% |
| 5 | NYU Langone Health | AetnaALIC | **6,287** | 6,408 | 18,628 | 98% |
| 6 | NYU Langone Health | Aetna | **6,273** | 6,408 | 18,412 | 98% |
| 7 | Northwell Health | AetnaALIC | **6,227** | 20,415 | 18,763 | 31% |
| 8 | NYU Langone Health | Empire | **6,207** | 6,408 | 18,464 | 97% |
| 9 | Northwell Health | Aetna | **6,207** | 20,415 | 18,404 | 30% |
| 10 | NYU Langone Health | UHC | **6,130** | 6,408 | 16,033 | 96% |
| 11 | NYU Langone Health | Cigna | **6,116** | 6,408 | 15,470 | 95% |
| 12 | Northwell Health | Cigna | **6,041** | 20,415 | 15,828 | 30% |
| 13 | NYU Langone Health | Emblem | **5,889** | 6,408 | 11,923 | 92% |
| 14 | NewYork-Presbyterian | AetnaALIC | **5,598** | 5,752 | 18,727 | 97% |
| 15 | NewYork-Presbyterian | Aetna | **5,584** | 5,752 | 18,103 | 97% |
| 16 | NewYork-Presbyterian | Cigna | **5,514** | 5,752 | 15,613 | 96% |
| 17 | Northwell Health | UHC | **5,421** | 20,415 | 15,961 | 27% |
| 18 | NewYork-Presbyterian | Emblem | **5,408** | 5,752 | 11,576 | 94% |
| 19 | Mount Sinai Health System | Cigna | **5,393** | 23,317 | 15,661 | 23% |
| 20 | NewYork-Presbyterian | UHC | **4,931** | 5,752 | 15,959 | 86% |
| 21 | Mount Sinai Health System | Emblem | **4,787** | 23,317 | 14,276 | 21% |
| 22 | Mount Sinai Health System | UHC | **4,155** | 23,317 | 15,972 | 18% |
| 23 | Northwell Health | Emblem | **893** | 20,415 | 1,061 | 4% |

## 4. Flags

**8 of 12 hospitals appear in zero carriers.** All eight were never payer-side targets, so this is a scope boundary rather than a failure:

- NYC Health + Hospitals (23,611,314 rows)
- Catholic Health System (Buffalo) (17,113,618 rows)
- University of Rochester Medical Center (4,964,600 rows)
- Upstate University Hospital (1,355,583 rows)
- Ellis Medicine (899,212 rows)
- Rochester Regional Health (841,244 rows)
- Maimonides Medical Center (449,204 rows)
- Crouse Health (216,206 rows)

**No carrier matches zero hospitals.** All six reach at least one.

**Empire carries no TINs at all** — it matched on NPI only, where every other carrier matched mostly by TIN. Its coverage therefore rests on a different identifier from the rest, which is worth knowing before comparing carriers to each other.

**Empire does not reach NewYork-Presbyterian**, so that pair is absent from section 3 while the other three systems have all six carriers.

**Three payer-side systems have no hospital-side data at all** — Montefiore (374 NPIs), WMC (41) and White Plains (22) are parsed on the payer side but absent from the lake, so their payer rows can never be reconciled.

