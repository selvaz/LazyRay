# LazyRay Dalio v2 refresh + Telegram report wrapper
# Requires environment variables:
#   TELEGRAM_BOT_TOKEN
#   TELEGRAM_CHAT_ID

param(
    [string[]]$RunDalioArgs = @('--csv')
)

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = 'C:\ProgramData\spyder-6\python.exe'

Set-Location $Root

function Import-PersistedEnvVar($Name) {
    if (Test-Path "Env:$Name") {
        return
    }
    $value = [Environment]::GetEnvironmentVariable($Name, "User")
    if (!$value) {
        $value = [Environment]::GetEnvironmentVariable($Name, "Machine")
    }
    if ($value) {
        Set-Item -Path "Env:$Name" -Value $value
        Write-Host "[$(Get-Date -Format s)] Loaded $Name from persisted environment."
    }
}

Import-PersistedEnvVar "MARKET_DATA_DB"
Import-PersistedEnvVar "LAZYRAY_DB"
Import-PersistedEnvVar "TELEGRAM_BOT_TOKEN"
Import-PersistedEnvVar "TELEGRAM_CHAT_ID"

Write-Host "[$(Get-Date -Format s)] Starting LazyRay Dalio v2 refresh: $($RunDalioArgs -join ' ')"
& $Python (Join-Path $Root 'run_dalio_v2.py') @RunDalioArgs
$runExit = $LASTEXITCODE
Write-Host "[$(Get-Date -Format s)] run_dalio_v2.py exit code: $runExit"

if ($runExit -eq 0) {
    Write-Host "[$(Get-Date -Format s)] Sending Telegram report"
    & $Python (Join-Path $Root 'send_telegram_report.py')
    $telegramExit = $LASTEXITCODE
    Write-Host "[$(Get-Date -Format s)] Telegram report exit code: $telegramExit"
} else {
    Write-Warning "Skipping Telegram send because run_dalio_v2.py failed (exit $runExit)."
}

exit $runExit
