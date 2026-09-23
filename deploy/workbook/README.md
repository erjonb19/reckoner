# Pipeline workbook

`reckoner.workbook.json` is an Azure Monitor workbook over the job's structured
logs. It has four panels: run history, duration per stage, peak memory against
the container ceiling, and stage 1's manifest verdict per layer.

The `.kql` files are the source and the JSON is built from them:

```
python scripts/build_workbook.py
```

`tests/deploy/test_workbook.py` fails if the JSON drifts from the files, or if a
query filters on an event name the job no longer emits. Without that check, a
renamed event would leave a panel reading "no results", which looks exactly
like a quiet month.

## Where it lives

Imported on 2026-09-23 as the shared workbook **"Reckoner pipeline"** in
`rg-reckoner` (Azure portal → Monitor → Workbooks). The import set the
workspace picker's default to `reckoner-logs`. The committed JSON names no
resource, so it still opens against any workspace. The imported queries were
read back and are identical to the committed file.

To update it after changing a `.kql` file: rebuild the JSON with
`python scripts/build_workbook.py`, then paste it into the workbook's
Advanced editor, or re-run the same ARM `PUT`. The resource name is fixed, a
UUID derived from `reckoner-pipeline-workbook`, so a re-import replaces the
workbook rather than duplicating it.

## Running a panel without the portal

```
az monitor log-analytics query -w <workspace-guid> --timespan P30D \
  --analytics-query "$(cat deploy/workbook/q_runs.kql)" -o table
```

## What it has already caught

Its first run showed three mart reruns as `failed, exit 1`, while
`az containerapp job execution show` still reported them `Running`. The
replicas were retrying. That exit code was the gold-verification bug fixed in
#75, which had been reporting correct writes as failures.
