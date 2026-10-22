# 32BJ Health Fund scripts

Exploratory work behind `docs/32bj-evaluation.md`. It reads only public files:

- 32BJ's Transparency in Coverage files (Healthcare Highways and Anthem/Empire)
- the local curated hospital lake
- public registries: NPPES, ProPublica Nonprofit Explorer, NY SPARCS and CMS

It does not write to the lake or touch the pipeline.

Every input and output lives under `data/raw/32bj/`, which is gitignored, so nothing
in this folder is backed up by git except the code and `facilities.csv`.

## Build (typed, re-runnable)

| script | reads | writes |
|---|---|---|
| `extract_facility_rates.py` | an Anthem in-network `.json.gz` | `<stem>.items/groups/prices.parquet` + `.meta.json` in `data/raw/32bj/anthem/derived/` |
| `build_facilities.py` | those Parquet files + NPPES | `facilities.csv` (committed) |
| `crosscheck_st_lukes.py` | the Parquet + the hospital lake | `st_lukes_crosscheck.csv` |
| `volume_sources.py` | SPARCS and CMS volume files | `top25.csv` |

Each takes its paths as arguments. Run with `python -I`, from the repository root.

## Profiling (kept as run on 2026-10-21)

`profiling/` holds the probes that produced sections 1–4 and 8.2 of the evaluation.
They are kept as the record of how those numbers were made, with hard-coded paths
replaced by arguments and nothing else changed. They are not typed and not tested.

| script | what it produced |
|---|---|
| `profile_tic.py` | the Healthcare Highways profile (`profile.json`) |
| `hosp_tins.py` | the hospital-TIN pass for those files |
| `pos_pairs.py` | the hospital-TIN pass for those files |
| `codes.py` | the hospital-TIN pass for those files |
| `ein_lookup.py` | ProPublica EIN names |
| `npi_refs.py` | NPPES reference sets |
| `extra_refs.py` | NPPES reference sets |
| `match.py` | system matching |
| `payer_strings.py` | the 32BJ string search over the hospital lake |
| `profile_anthem.py` | the Anthem profile |
| `anthem_summary.py` | the Anthem profile |
| `rc_census.py` | the revenue-code structure scan (section 8.2) |
| `bundle_check.py` | the revenue-code structure scan (section 8.2) |
