<#
.SYNOPSIS
    Take a payer manifest snapshot, and optionally register that as a daily task.

.DESCRIPTION
    The manifest can detect drift at the payer boundary, but only against a
    previous snapshot -- and nothing was taking one, so in practice there was
    rarely anything to compare against. This is the thing that takes one.

    It has to run locally. The payer Parquet is a gitignored 4.3 GB directory in
    a sibling repo, so GitHub Actions and any cloud scheduler are out: they
    cannot see the data. That is a property of where the data lives, not a
    preference.

    Run with -Register to install a daily Scheduled Task that calls this same
    script. The task runs as the current user and only while logged on, which
    avoids storing a credential for a job that reads a local directory.

.EXAMPLE
    .\scripts\snapshot_payer_manifest.ps1
    Take one snapshot now and print what changed since the last.

.EXAMPLE
    .\scripts\snapshot_payer_manifest.ps1 -Register -At 07:30
    Install the daily task.

.EXAMPLE
    .\scripts\snapshot_payer_manifest.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$PayerRoot = "..\mrf_pipeline\payer_parquet",
    # Under data/, which is gitignored: these are a local trail, not repo content.
    [string]$SnapshotDir = "data\manifests",
    [int]$Keep = 30,
    [string]$At = "07:00",
    [string]$TaskName = "Reckoner payer manifest snapshot",
    [switch]$Register,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\python.exe"
$script = Join-Path $PSScriptRoot "snapshot_payer_manifest.ps1"

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "removed scheduled task: $TaskName"
    } else {
        Write-Output "no scheduled task named: $TaskName"
    }
    exit 0
}

if ($Register) {
    # Interactive logon type: no stored credential for a job that only reads a
    # local directory. The cost is that it runs only while signed in, which for
    # a daily check on a personal machine is the right trade.
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -PayerRoot `"$PayerRoot`" -SnapshotDir `"$SnapshotDir`" -Keep $Keep" `
        -WorkingDirectory $repo
    $trigger = New-ScheduledTaskTrigger -Daily -At $At
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null

    Write-Output "registered '$TaskName', daily at $At"
    Write-Output "  snapshots : $SnapshotDir (keeping $Keep)"
    Write-Output "  log       : $SnapshotDir\snapshot.log"
    Write-Output "  remove    : .\scripts\snapshot_payer_manifest.ps1 -Unregister"
    exit 0
}

# --- take the snapshot ------------------------------------------------------
Set-Location $repo
if (-not (Test-Path $PayerRoot)) {
    # Non-zero so Task Scheduler's history shows a failure rather than success.
    # A missing payer root is exactly the state a silent success would hide.
    Write-Error "payer root not found: $PayerRoot"
    exit 1
}
New-Item -ItemType Directory -Force -Path $SnapshotDir | Out-Null
$log = Join-Path $SnapshotDir "snapshot.log"

$stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
Add-Content -Path $log -Encoding utf8 -Value "=== $stamp ==="

# --fail-on-change is deliberately NOT passed: a change is news to record, not a
# failure. The log is the record; the exit code stays about whether the check
# itself could run.
$output = & $python -m payer.manifest --payer-root $PayerRoot --snapshot-dir $SnapshotDir --keep $Keep
$code = $LASTEXITCODE
$output | Add-Content -Path $log -Encoding utf8
$output | Write-Output

if ($code -ne 0) {
    Add-Content -Path $log -Encoding utf8 -Value "snapshot failed with exit $code"
    exit $code
}

# A4: the manifest says what moved; this says whether it matters. Same reasoning
# on the exit code -- a monitor that fails the task on every new payer month
# gets muted, and a muted monitor is indistinguishable from none.
Add-Content -Path $log -Encoding utf8 -Value "--- materiality ---"
$verdict = & $python -m agents.ingest_monitor --snapshot-dir $SnapshotDir
$verdictCode = $LASTEXITCODE
$verdict | Add-Content -Path $log -Encoding utf8
$verdict | Write-Output

if ($verdictCode -ne 0) {
    Add-Content -Path $log -Encoding utf8 -Value "monitor failed with exit $verdictCode"
    exit $verdictCode
}
exit 0
