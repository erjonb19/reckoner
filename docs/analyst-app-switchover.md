# Switching reckoner-ny to the analyst app

`reckoner-ny` (<https://reckoner-ny.streamlit.app>) runs `streamlit_app.py`
until the facility-grain tables exist for all seven systems. The analyst app
(`analyst_app.py`, #101) is merged but not live: its Rankings, Pair detail and
Code lookup pages read `pairs`, `residual` and the monthly rate release, and
today the committed summary has none of them. Only the preview build carried
them, for White Plains alone.

**Earliest date: 2026-10-02**, after two scheduled runs:

| when (UTC) | what | produces |
|---|---|---|
| 2026-10-01 08:00 | `reckoner-mart` (cron `0 8 1 * *`), all seven systems, chaining `triage` and `report` | gold `pairs`, `residual`, `rates`, `codes`; `lake/summary` |
| 2026-10-02 10:00 | the `summary` workflow (cron `0 10 2 * *`) | `summary/*.csv` committed to `main`; the `data-2026-10-01` release with `rates.parquet` and `codes.parquet` |

Neither has run with the facility-grain tables before. If either fails, the
switch waits for a fix and a rerun. Don't switch with six systems.

## 1. Check the data before touching the app

Run these on `main` after the October 2 commit lands.

```bash
git pull
# Seven systems in the pair table, and in coverage.
python - <<'PY'
import csv
for name in ("pairs", "coverage"):
    systems = {r["system"] for r in csv.DictReader(open(f"summary/{name}.csv", encoding="utf-8"))}
    print(name, len(systems), sorted(systems))
PY
# run.json names a release, and the release exists with both files.
python -c "import json; print(json.load(open('summary/run.json'))['release']['tag'])"
gh release view "$(python -c "import json; print(json.load(open('summary/run.json'))['release']['tag'])")"
```

Both lines should print 7. The release should list `rates.parquet` and
`codes.parquet`. The workflow checks their sha256 against `run.json` before it
commits, so a mismatch there means the workflow failed, and it will have said
so.

## 2. Click through before switching

Point the preview app at `main`, so the analyst app runs against the real
October summary before the public URL does. On <https://share.streamlit.io>,
open the preview app's **Settings** and set the branch to `main`. If Settings
does not offer the branch, delete the preview app and deploy a new one from
`main` with main file `analyst_app.py`.

Confirm all four pages have seven systems:

- **Rankings**: the Health system filter lists seven systems, and the table has
  pairs from each. Pick each system in turn.
- **Pair detail**: the Facility and insurer picker offers pairs from all seven.
- **Code lookup**: a common code (`99213`, `70450`) returns facilities from all
  seven. Check the page does not show "Rate-level data unavailable".
- **Coverage and data quality**: seven rows, both shares filled in.

Also check the top of each page: the PREVIEW caveat must be gone, because that
text came from the preview build's `run.json`, and `Data built` must read
2026-10-01 or later.

## 3. Switch

On <https://share.streamlit.io>, open **reckoner-ny** → **Settings** and change
the **Main file path** from `streamlit_app.py` to `analyst_app.py`. Keep the
repository and branch (`erjonb19/reckoner`, `main`) and the empty secrets.

If Community Cloud does not allow changing the main file of a deployed app,
there are two ways round it:

- delete `reckoner-ny` and redeploy it from `main` with `analyst_app.py`,
  claiming the same `reckoner-ny` subdomain; or
- make the change in the repository instead, with no platform setting at all:
  `git mv analyst_app.py streamlit_app.py` in a PR, after step 4's retirement of
  the old file.

Then open <https://reckoner-ny.streamlit.app> and repeat step 2's four checks
there.

## 4. Retire streamlit_app.py

Once reckoner-ny is serving the analyst app, open one PR that:

- deletes `streamlit_app.py`, `src/pipeline/summary_view.py`,
  `tests/pipeline/test_streamlit_app.py` and `tests/pipeline/test_summary_view.py`.
  Check first that nothing else imports `summary_view`;
- updates `docs/streamlit-deploy.md` (main file path) and the README's page
  section;
- removes the preview app on Community Cloud, the `preview/analyst-app` branch,
  and the `preview-2026-09-23` pre-release, which the preview build alone
  references.

The last item deletes a published release and a branch. Do it by hand, not from
a script.

## Rolling back

Set reckoner-ny's main file back to `streamlit_app.py`. Until step 4 merges,
the file is still in the repository and still reads the same `summary/`, so a
rollback costs one setting.
