# PowerShell — install the filings-scraper service via Windows Task Scheduler.
#
# Use this INSTEAD of install_nssm_service.ps1 when you don't want to
# store your Windows password in NSSM. Task Scheduler with LogonType
# Interactive runs as your user without persisted credentials.
#
# Trade-off: the service runs only when you're logged on to Windows.
# After a reboot, the service starts the first time you log in and
# survives until you log out / reboot. For a personal desktop you sit
# at, this is what you want. If you need the service to run when no
# one is logged on (e.g. headless server), use install_nssm_service.ps1
# with a stored credential instead.
#
# Run as Administrator:
#   .\install_scheduled_task.ps1
#
# Uninstall:
#   .\install_scheduled_task.ps1 -Uninstall
#
# Requirements:
#   - WSL2 distro with the workspace at <RepoPath> (default
#     /home/natbanyat/.openclaw/workspace-investing).
#   - .env file at the repo root with FILINGS_SCRAPER_TOKEN set.
#   - You are logged on as the user the WSL distro belongs to.

param(
    [string]$TaskName     = "FilingsScraper",
    [string]$WslDistro    = "Ubuntu-24.04",
    [string]$WslUser      = "natbanyat",
    [string]$RepoPath     = "/home/natbanyat/.openclaw/workspace-investing",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

if ($Uninstall) {
    Write-Host "Stopping and removing scheduled task: $TaskName"
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed."
    exit 0
}

# Make sure the wsl launcher exists.
$wslLauncherPath = "$RepoPath/scripts/start.sh"
Write-Host "Verifying WSL launcher path: $wslLauncherPath"
$verifyResult = wsl -d $WslDistro -u $WslUser -- test -x $wslLauncherPath
if ($LASTEXITCODE -ne 0) {
    Write-Error "WSL launcher not found or not executable at $wslLauncherPath. Make sure the workspace is checked out and scripts/start.sh has +x."
}

$user = "$env:USERDOMAIN\$env:USERNAME"
Write-Host "Installing scheduled task: $TaskName"
Write-Host "  Run as:   $user (LogonType Interactive)"
Write-Host "  Distro:   $WslDistro"
Write-Host "  RepoPath: $RepoPath"

# Action: run the WSL launcher.
$action = New-ScheduledTaskAction `
    -Execute "C:\Windows\System32\wsl.exe" `
    -Argument "-d $WslDistro -u $WslUser -- bash -lc '$wslLauncherPath'"

# Trigger: at every logon for this user. Add a manual trigger via
# Start-ScheduledTask whenever you want to start it without re-logging in.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user

# Settings: long-running, restart on failure with backoff, ignore overlapping
# starts (we don't want two instances fighting for port 8876).
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

# Principal: run as the user, with LogonType Interactive (no stored password).
$principal = New-ScheduledTaskPrincipal `
    -UserId $user `
    -LogonType Interactive `
    -RunLevel Limited

# Replace any existing registration cleanly.
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Existing task found; replacing."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Filings-scraper aiohttp service running inside WSL2 ($WslDistro). Triggers at logon; restarts on crash with 1-minute backoff."

Write-Host ""
Write-Host "Installed."
Write-Host ""
Write-Host "Start now:        Start-ScheduledTask -TaskName $TaskName"
Write-Host "Status:           Get-ScheduledTaskInfo -TaskName $TaskName"
Write-Host "Stop:             Stop-ScheduledTask -TaskName $TaskName"
Write-Host "Uninstall:        .\install_scheduled_task.ps1 -Uninstall"
Write-Host ""
Write-Host "After Start, verify with:"
Write-Host "  curl.exe -s http://127.0.0.1:8876/health"
Write-Host "  curl.exe -s http://127.0.0.1:8876/version"
