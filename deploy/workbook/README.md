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

## Importing it

**Not done from here.** Saving a workbook creates an Azure resource, and that's
your call. To open it without saving: Azure portal → Monitor → Workbooks → New →
Advanced editor (`</>`) → paste the JSON → Apply. It asks for the workspace:
choose `reckoner-logs`. Save it only if you want it kept.

Each query was run against `reckoner-logs` with `az monitor log-analytics query`
before being committed. The import itself has not been tried.

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
