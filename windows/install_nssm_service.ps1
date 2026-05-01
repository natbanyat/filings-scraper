# PowerShell — install the filings-scraper service via NSSM.
#
# IMPORTANT: this path requires storing your Windows password in NSSM
# because LocalSystem cannot reach a per-user WSL distro
# (WSL_E_LOCAL_SYSTEM_NOT_SUPPORTED). If you don't want to store the
# password, use install_scheduled_task.ps1 instead — same outcome,
# different mechanism, no password.
#
# Prerequisites:
#   - NSSM installed: `winget install --id NSSM.NSSM` or download from
#     https://nssm.cc/download
#   - WSL2 distro 'Ubuntu-24.04' (or your distro name) with the
#     workspace at /home/<user>/.openclaw/workspace-investing
#   - .env file at the repo root with FILINGS_SCRAPER_TOKEN set
#   - The Windows password for the account the service will run as.
#     Pass via -Credential (Get-Credential prompt) or interactively.
#
# Run as Administrator:
#   .\install_nssm_service.ps1
#
# Uninstall:
#   .\install_nssm_service.ps1 -Uninstall

param(
    [string]$ServiceName  = "FilingsScraper",
    [string]$WslDistro    = "Ubuntu-24.04",
    [string]$WslUser      = "natbanyat",
    [string]$RepoPath     = "/home/natbanyat/.openclaw/workspace-investing",
    [System.Management.Automation.PSCredential]$Credential,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

# Resolve nssm.exe (winget puts it in WindowsApps; download puts it under
# C:\Program Files\NSSM\, etc.). Fail loudly if not on PATH.
$nssm = (Get-Command nssm.exe -ErrorAction SilentlyContinue).Source
if (-not $nssm) {
    Write-Error "nssm.exe not found on PATH. Install with 'winget install --id NSSM.NSSM' or add the install dir to PATH."
}

if ($Uninstall) {
    Write-Host "Stopping and removing service: $ServiceName"
    & $nssm stop $ServiceName 2>$null
    & $nssm remove $ServiceName confirm
    Write-Host "Removed."
    exit 0
}

# Build the wsl.exe invocation that runs the bash launcher.
$wslExe   = "C:\Windows\System32\wsl.exe"
$wslArgs  = "-d $WslDistro -u $WslUser -- bash -lc '$RepoPath/scripts/start.sh'"

Write-Host "Installing service: $ServiceName"
Write-Host "  Distro:   $WslDistro"
Write-Host "  User:     $WslUser"
Write-Host "  RepoPath: $RepoPath"

# Logs go to %LOCALAPPDATA%\filings-scraper\logs\.
$logDir = Join-Path $env:LOCALAPPDATA "filings-scraper\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# Acquire credentials interactively if not passed via -Credential.
if (-not $Credential) {
    Write-Host ""
    Write-Host "WSL distros are per-user, so the service must run as your account."
    Write-Host "LocalSystem WILL NOT WORK (errors with WSL_E_LOCAL_SYSTEM_NOT_SUPPORTED)."
    Write-Host "Enter the password for $env:USERDOMAIN\$env:USERNAME:"
    $Credential = Get-Credential -UserName "$env:USERDOMAIN\$env:USERNAME" `
        -Message "Windows password for the FilingsScraper service. Stored encrypted in NSSM registry."
    if (-not $Credential) {
        Write-Error "Credential entry cancelled. Aborting."
    }
}
$svcUser = $Credential.UserName
$svcPwd  = $Credential.GetNetworkCredential().Password

# Install or recreate.
& $nssm install $ServiceName $wslExe $wslArgs
& $nssm set $ServiceName Description "Filings-scraper aiohttp service running inside WSL2 ($WslDistro)."
& $nssm set $ServiceName Start SERVICE_AUTO_START
# ObjectName needs both username and password in a single NSSM call,
# otherwise NSSM logs "Setting ObjectName requires both a username and
# password!" and silently leaves the service running as LocalSystem,
# which cannot reach WSL.
& $nssm set $ServiceName ObjectName $svcUser $svcPwd
if ($LASTEXITCODE -ne 0) {
    Write-Error "NSSM set ObjectName failed (exit $LASTEXITCODE). Service will run as LocalSystem and crash. Fix manually with: nssm.exe edit $ServiceName"
}

# Restart policy: aggressive but capped.
& $nssm set $ServiceName AppStopMethodSkip 0
& $nssm set $ServiceName AppExit Default Restart
& $nssm set $ServiceName AppRestartDelay 5000           # 5 seconds between retries
& $nssm set $ServiceName AppThrottle 10000              # only count restarts <10s as failures

# Log redirection.
& $nssm set $ServiceName AppStdout (Join-Path $logDir "stdout.log")
& $nssm set $ServiceName AppStderr (Join-Path $logDir "stderr.log")
& $nssm set $ServiceName AppRotateFiles 1
& $nssm set $ServiceName AppRotateOnline 1
& $nssm set $ServiceName AppRotateBytes 10485760        # rotate at 10 MB

Write-Host ""
Write-Host "Installed. Start with: sc start $ServiceName"
Write-Host "Logs:   $logDir"
Write-Host ""
Write-Host "Verify after starting:"
Write-Host "  curl -s http://127.0.0.1:8876/health"
Write-Host '  curl -s -H "Authorization: Bearer $env:FILINGS_SCRAPER_TOKEN" http://127.0.0.1:8876/api/companies | Select-String "company_key" | Select-Object -First 5'
