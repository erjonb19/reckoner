#!/usr/bin/env bash
# Run the mart one system per execution, sequentially.
#
# The selection goes on the job definition, not on `job start --env-vars`.
# That flag replaces the whole container template and silently resets cpu and
# memory to the CLI's defaults: an execution started that way reported Running
# at 0.5 vCPU / 1 GiB instead of 4 / 8, with the env var and image correct and
# nothing saying the sizing had gone. Hence the guard below, which refuses to
# start anything that is not on 4 vCPU.
#
#   bash scripts/run_mart_per_system.sh /some/output/dir
set -u
JOB=reckoner-mart
RG=rg-reckoner
OUT="$1"
for SLUG in mount-sinai-health-system northwell-health nyu-langone-health newyork-presbyterian; do
  echo "=== $SLUG ==="
  az containerapp job update -n $JOB -g $RG --set-env-vars "RECKONER_SYSTEM=$SLUG" -o none 2>&1|grep -v WARNING|tail -0
  CPU=$(az containerapp job show -n $JOB -g $RG --query "properties.template.containers[0].resources.cpu" -o tsv 2>/dev/null|tr -d '\r')
  MEM=$(az containerapp job show -n $JOB -g $RG --query "properties.template.containers[0].resources.memory" -o tsv 2>/dev/null|tr -d '\r')
  echo "  sizing before start: ${CPU} vCPU / ${MEM}"
  if [ "$CPU" != "4" ] && [ "$CPU" != "4.0" ]; then echo "  ABORT: sizing is not 4 vCPU"; exit 1; fi
  EXEC=$(az containerapp job start -n $JOB -g $RG --query "name" -o tsv 2>/dev/null|tr -d '\r')
  echo "  execution: $EXEC"
  echo "$SLUG $EXEC" >> "$OUT/executions.txt"
  for i in $(seq 1 130); do
    S=$(az containerapp job execution show -n $JOB -g $RG --job-execution-name "$EXEC" --query "properties.status" -o tsv 2>/dev/null|tr -d '\r')
    case "$S" in Succeeded|Failed|Degraded) echo "  -> $S"; break;; esac
    sleep 30
  done
done
echo "=== restoring RECKONER_SYSTEM to unset (all four) ==="
az containerapp job update -n $JOB -g $RG --remove-env-vars RECKONER_SYSTEM -o none 2>&1|grep -v WARNING|tail -0
echo done
