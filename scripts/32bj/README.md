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
| `build_facility_rates.py` | those Parquet files + `facilities.csv` | `facility_rates.parquet` + `facility_items.parquet`, what the 32BJ page reads |
| `crosscheck_st_lukes.py` | `facility_rates.parquet` + the hospital lake | `st_lukes_crosscheck.csv` + `.json` |
| `volume_sources.py` | SPARCS and CMS volume APIs | `top25.csv` (copy it into the derived folder for the page) |
| `rescan_professional.py` | the 32BJ `.json.gz` + St. Luke's provider references | St. Luke's professional rates, to test the outpatient disagreement |
| `make_test_fixture.py` | `facility_rates.parquet` | `tests/fixtures/thirtytwobj/`, a slice of real rows |

Each takes its paths as arguments. Run with `python -I`, from the repository root.
DuckDB steps are capped at 1 GB and spill to `derived/_spill`: the 32BJ file's groups
table is 114M rows.

## Order

1. `extract_facility_rates.py` on `NY_HYBYMED32BJ` (about 37 min) and `NY_HXRLMED0000_06_06` (about 2 min)
2. `build_facilities.py`, then `build_facility_rates.py`
3. `volume_sources.py data/raw/32bj/volume`, then copy `top25.csv` into `data/raw/32bj/anthem/derived/`
4. `crosscheck_st_lukes.py ... data/raw/32bj/anthem/derived/st_lukes_crosscheck.csv`
5. `streamlit run analyst_app.py`, page **32BJ**

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
