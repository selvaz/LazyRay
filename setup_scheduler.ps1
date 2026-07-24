# ============================================================================
# setup_scheduler.ps1 -- creates the Windows scheduled task for LazyRay's
# Dalio v2 report + Telegram send.
#
# Timed to run after market-data-hub's second daily run (MarketData_USClose,
# 13:15 Mon-Fri), which is what refreshes the macro_panel data this report
# reads. That run + its own report/Telegram steps finish in ~11 minutes in
# practice (observed 13:15:02 -> 13:26:07); 13:40 leaves a comfortable buffer
# and still runs before MarketData_HMMRegime (13:45), which is independent
# (separate DB, no lock contention) but keeps the two off the same minute.
#
# Run from PowerShell as administrator:
#     powershell -ExecutionPolicy Bypass -File .\setup_scheduler.ps1
#
# To remove the task:
#     powershell -ExecutionPolicy Bypass -File .\setup_scheduler.ps1 -Remove
# ============================================================================
param(
    [switch]$Remove,
    [string]$Root = "",
    [string]$Python = "C:\ProgramData\spyder-6\python.exe"
)

$ErrorActionPreference = "Stop"
if (!$Root) {
    $Root = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$wrapper = Join-Path $Root "run_dalio_v2_with_telegram.ps1"
$logDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$taskName = "LazyRay_DalioV2Report"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "Removed task $taskName"
    }
    Write-Host "Done."
    return
}

$logFile = Join-Path $logDir "$taskName.log"
$runDalioArgs = @('--csv')
# -Command (not -File) so PowerShell's own parser sees *>>: Task Scheduler
# invokes powershell.exe directly, and -File would pass ">>" through as an
# inert literal argument instead of redirecting output (same reasoning as
# market-data-hub's setup_scheduler.ps1).
$argText = ($runDalioArgs | ForEach-Object { "'" + $_.Replace("'", "''") + "'" }) -join ","
$cmdString = "& '$wrapper' -RunDalioArgs $argText *>> '$logFile'"
$psArgs = "-NoProfile -ExecutionPolicy Bypass -Command `"$cmdString`""

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $psArgs
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "13:40"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 2)

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "LazyRay: Dalio v2 country-risk report + Telegram send, after market-data-hub's second daily run" | Out-Null

Write-Host "Created task '$taskName' (13:40 Mon-Fri) -> $(Split-Path -Leaf $wrapper) $($runDalioArgs -join ' ')"
Write-Host "Verify with: Get-ScheduledTask -TaskName $taskName"
Write-Host "Logs in: $logFile"
