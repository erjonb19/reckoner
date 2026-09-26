# October 2 checklist: prove the October run, then switch reckoner-ny

`reckoner-ny` (<https://reckoner-ny.streamlit.app>) runs `streamlit_app.py` until
this checklist is done. The analyst app (`analyst_app.py`, #101) is merged but not
live. Its Rankings, Pair detail and Code lookup pages read `pairs`, `residual` and
the monthly rate release, and until the October run only a preview build had them,
for White Plains alone.

Two scheduled runs come first:

| when (UTC) | what | produces |
|---|---|---|
| 2026-10-01 08:00 | `reckoner-mart` (cron `0 8 1 * *`): all seven systems, chaining `triage` and `report`. The first run with the spool (#103). | gold for every table; `lake/summary` |
| 2026-10-02 10:00 | the `summary` workflow (cron `0 10 2 * *`) | `summary/*.csv` committed to `main`; the `data-2026-10-01` release |

Do the steps in order. **Who** says who runs it: **Claude Code**, or **you** where the
step needs your Community Cloud login, a judgement, or a deletion.

## 1. The October 1 mart run succeeded (Claude Code)

```bash
az containerapp job execution list -n reckoner-mart -g rg-reckoner \
  --query "[?properties.startTime >= '2026-10-01'].{name:name, status:properties.status, start:properties.startTime, end:properties.endTime}" -o table
```

The execution must read `Succeeded`, with no retry, because `replicaRetryLimit` is 0.
Then check the workbook ("Reckoner pipeline" in `rg-reckoner`), or run its queries
directly (`deploy/workbook/README.md`):

- **Run history** (`q_runs.kql`): the attempt ends in `job_end`, not "killed (no job_end)".
- **Duration per stage** (`q_stage_duration.kql`): mart, triage and report all ran. The report
  only runs after a full, successful mart.
- **Peak memory** (`q_memory.kql`): under the 8,192 MiB ceiling. This is the first time
  all seven systems have run in one execution.
- **Spool events**: `mart_spooled` for payer silver and for each system. Any
  `mart_spool_skipped`, `mart_spool_failed` or `mart_spool_mismatch` means that
  system read ADLS directly. The run is still correct, but note which systems.

**If the run failed, stop here.** Gold is unchanged, because the mart writes only at the
end. Don't switch the app. The failure is the next piece of work.

## 2. White Plains gold is byte-identical to 2026-09-23 (Claude Code)

```bash
RECKONER_STORAGE=adls RECKONER_ADLS_ACCOUNT=reckonerlake0914 RECKONER_ADLS_ROOT=lake \
  python scripts/verify_gold_baseline.py docs/measurements/white-plains-gold-2026-09-23.json
```

This reads about 1 MB. Every line must say `same`, and the last line `byte-identical`.
Eight tables compare by file sha256. `rates` compares by its rows, because the
2026-09-23 file was written before `preserve_order` and its row order was not
reproducible. Report the output in `docs/BUILT_VS_PLANNED.md` either way.

**If it fails:** turn the spool off so the next run reads ADLS directly, and record it:

```bash
az containerapp job update -n reckoner-mart -g rg-reckoner --set-env-vars RECKONER_SPOOL_DIR=off
```

Then **stop and don't switch the app**, whatever step 1 said. Gold that differs from what
the same silver produced a week earlier is a finding to explain first. A difference
could also be a silver change, so check the silver manifests' `published_at` before
blaming the spool.

## 3. Read operations, before and after (Claude Code)

Query Cost Management for the resource group, daily, grouped by meter. The same query
produced the September numbers. Take `Hot LRS Read Operations` for 2026-10-01 and record
it beside 2026-09-23 in `docs/BUILT_VS_PLANNED.md`:

| day | runs | read operations | cost |
|---|---|---|---|
| 2026-09-23 (before) | 28 system reconciliations | 4,979,846 | $2.49 |
| 2026-10-01 (after) | 7 systems, one execution | *fill in* | *fill in* |

Compare per system-run: before was about 178,000. Cost Management can lag by a day, so
if 2026-10-01 looks partial on the 2nd, read it again on the 3rd before recording it.
The README's cost section says the "after" comes from this run, so it gets the number
too.

## 4. The October 2 snapshot has seven systems of pair tables (Claude Code)

After the 10:00 workflow commits:

```bash
git pull
python - <<'PY'
import csv, json
for name in ("pairs", "coverage", "residual"):
    systems = {r["system"] for r in csv.DictReader(open(f"summary/{name}.csv", encoding="utf-8"))}
    print(name, len(systems), sorted(systems))
meta = json.load(open("summary/run.json", encoding="utf-8"))
print("built", meta["built_at"][:10], "release", meta["release"]["tag"], "caveats", meta["caveats"])
PY
gh release view "$(python -c "import json; print(json.load(open('summary/run.json'))['release']['tag'])")"
```

`pairs`, `coverage` and `residual` each print 7. `built` is 2026-10-01 or later. The
release lists `rates.parquet` and `codes.parquet`; the workflow checks their sha256
against `run.json` before it commits. The only caveat should be the "No PHI" line.

## 5. Switch the app

**5a. Click through on the preview app (you).** On <https://share.streamlit.io>, point
the preview app at branch `main` with main file `analyst_app.py`. If Settings does not
offer the branch, delete the preview app and deploy a new one from `main`. Claude Code
can drive this in your browser if you ask. Then check all four pages have seven
systems:

- **Rankings**: the Health system filter lists seven, and each has pairs.
- **Pair detail**: the Facility and insurer picker offers pairs from all seven.
- **Code lookup**: `99213` and `70450` return facilities from all seven, with no
  "Rate-level data unavailable".
- **Coverage and data quality**: seven rows, both shares filled in.

At the top of every page, the PREVIEW caveat is gone and "Data built" reads 2026-10-01
or later.

**5b. Switch reckoner-ny (you).** On <https://share.streamlit.io>, open **reckoner-ny**
→ **Settings** and change **Main file path** from `streamlit_app.py` to
`analyst_app.py`. Keep the repository, branch and empty secrets. If Community Cloud
does not allow changing the main file of a deployed app, either delete `reckoner-ny`
and redeploy from `main` with `analyst_app.py`, claiming the same subdomain, or have
Claude Code rename `analyst_app.py` to `streamlit_app.py` in 5d's PR, which needs
no platform setting at all.

**5c. Check the live page (Claude Code, then you).** Repeat 5a's four checks on
<https://reckoner-ny.streamlit.app>.

**5d. Retire the old page (Claude Code opens the PR; you delete).** One PR deletes
`streamlit_app.py`, `src/pipeline/summary_view.py`, `tests/pipeline/test_streamlit_app.py`
and `tests/pipeline/test_summary_view.py`, after checking nothing else imports
`summary_view`. It also updates `docs/streamlit-deploy.md` and the README's page
section. By hand, not from a script, **you** remove the preview app on Community
Cloud, the `preview/analyst-app` branch, and the `preview-2026-09-23` pre-release.

## 6. Retake the README screenshots and GIF (Claude Code)

`docs/img/` holds the old page's five tabs from 2026-09-18, before the unit changed.
Replace them with the four pages of the live analyst app:

- one image per page, in both themes if they read differently;
- the GIF showing a filter narrowing Rankings, then a row opening Pair detail.

`scripts/shoot_streamlit.py` targets the old page's tabs and needs adapting to the
four pages. It waits for content rather than time, which matters on a page that
sleeps: wake the page and confirm it has drawn before capturing. Assemble the GIF with
`scripts/make_gif.py`. Update the README's image references and captions to match.

## 7. docs/coverage.md (Claude Code)

It is a 2026-09-13 snapshot, marked superseded. Regenerate it from current gold if its
identifier and name-bridging sections still earn their place. Otherwise delete it and
point the README's documentation list at `docs/scope.md`, which holds current
coverage. Say which in the PR.

## 8. Bring the docs up to date (Claude Code)

- `docs/BUILT_VS_PLANNED.md`: step 2's result; step 3's read counts; the switch-over
  (moves from "Not started" to built, with the date); the A1 and triage numbers if the
  October gold changed them.
- `README.md`: the reconciliation table and residual from October's `summary/coverage.csv`,
  the page section (four pages, not five views), the cost section's "after" number, and
  the test count.
- `CLAUDE.md`: its "Current phase" section, where anything above changed it.

Every number is checked against its source file, as in the September review.

## Rolling back

- **If step 1 or step 2 fails, don't switch the app.** The old page keeps working on
  the last good `summary/`.
- After 5b, set reckoner-ny's main file back to `streamlit_app.py`. Until 5d merges,
  the file is still in the repository and still reads the same `summary/`, so a
  rollback costs one setting.
- The spool: `RECKONER_SPOOL_DIR=off` on the job restores direct reads, with no
  code change.
