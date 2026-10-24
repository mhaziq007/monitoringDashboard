<#
.SYNOPSIS
    Installs the HomeLab Dashboard agent on a Windows node as a scheduled task.

.DESCRIPTION
    Creates a virtualenv, installs psutil, writes a launcher that carries the
    agent configuration, and registers a Scheduled Task that runs the agent
    silently via pythonw. Run from an elevated PowerShell prompt inside a
    checkout of this repository:

        .\scripts\install-agent.ps1 -Server http://192.168.1.50:8000 -Key <agent-key>

    Configuration lives in the generated launcher, so agent.py is never edited
    and stays in sync with git.

.PARAMETER Vm
    Whether this node is a virtual machine: auto (default, detected by the
    agent), yes or no. VMs are left out of the power and cost totals.

.PARAMETER AtLogon
    Run the agent when the current user logs on, instead of at system startup
    as SYSTEM. Use this if the agent needs the interactive desktop session.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [Parameter(Mandatory = $true)][string]$Key,
    [int]$Interval = 5,
    [ValidateSet('auto', 'yes', 'no')][string]$Vm = 'auto',
    [switch]$AtLogon
)

$ErrorActionPreference = 'Stop'
$TaskName = 'HomeLab Dashboard Agent'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'This script must be run from an elevated PowerShell prompt.'
}

# Accept either a base URL or the full endpoint.
$reportUrl = $Server.TrimEnd('/')
if (-not $reportUrl.EndsWith('/api/report')) { $reportUrl = "$reportUrl/api/report" }

$repoRoot  = Split-Path -Parent $PSScriptRoot
$agentDir  = Join-Path $repoRoot 'agent'
$venvDir   = Join-Path $agentDir '.venv'
$launcher  = Join-Path $agentDir 'run-agent.cmd'

if (-not (Test-Path (Join-Path $agentDir 'agent.py'))) {
    throw "Could not find agent\agent.py under $repoRoot."
}

# Windows keeps an app execution alias that makes bare `python` resolve to the
# per-user Store build even when an all-users python.org install exists, and
# the py launcher often prefers the Store registration too. So when PATH points
# at a Store build, look for a conventional install on disk before giving up.
function Find-SystemPython {
    $candidates = @()

    $onPath = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($onPath -and $onPath -notlike '*\WindowsApps\*') { $candidates += $onPath }

    # All-users python.org installs, newest version first.
    $candidates += Get-ChildItem 'C:\Program Files\Python3*' -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        ForEach-Object { Join-Path $_.FullName 'python.exe' }

    if (Get-Command py -ErrorAction SilentlyContinue) {
        $viaLauncher = & py -3 -c 'import sys; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $viaLauncher) { $candidates += $viaLauncher.Trim() }
    }

    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c) -and $c -notlike '*\WindowsApps\*') { return $c }
    }
    return $onPath  # may be a Store build or null; caller decides
}

$python = Find-SystemPython
if (-not $python) { throw 'python not found. Install Python 3.10+ and re-run.' }
Write-Host "==> Using interpreter: $python"

# Microsoft Store Python is installed per-user under WindowsApps. The SYSTEM
# account cannot load another user's Store package, so a venv built on it
# produces a task that starts and dies instantly with no error anywhere.
$isStorePython = $python -like '*\WindowsApps\*'
if ($isStorePython -and -not $AtLogon) {
    Write-Error @"
This machine's Python is the Microsoft Store build:
    $python
A Store install is per-user, so the agent cannot run as SYSTEM. Pick one:

  1. Install Python from https://www.python.org/downloads/ ("Install for all
     users"), delete $venvDir, and re-run this script. The agent then runs at
     boot regardless of who is logged in. Recommended for an always-on node.

  2. Re-run this script with -AtLogon to run the agent as $($identity.Name)
     instead. Works with Store Python, but only while that user is logged in.
"@
    exit 1
}
if ($isStorePython) {
    Write-Warning "Using Microsoft Store Python; the agent only runs while $($identity.Name) is logged in."
}

Write-Host "==> Installing agent from $agentDir (reporting to $reportUrl)"

$pythonw = Join-Path $venvDir 'Scripts\pythonw.exe'
if (Test-Path $venvDir) {
    $stale = $false
    if (-not (Test-Path $pythonw)) {
        $stale = $true
    } else {
        # A venv records its base interpreter in pyvenv.cfg. If that no longer
        # matches the python being used now (e.g. after moving off Store
        # Python), reusing the directory keeps the old broken base.
        $cfg = Join-Path $venvDir 'pyvenv.cfg'
        $venvHome = (Get-Content $cfg -ErrorAction SilentlyContinue |
                     Select-String '^home\s*=' | Select-Object -First 1) -replace '^home\s*=\s*',''
        if ($venvHome -and (Split-Path $python -Parent) -ne $venvHome.Trim()) { $stale = $true }
    }
    if ($stale) {
        Write-Host "==> Removing stale virtualenv at $venvDir"
        Remove-Item -Recurse -Force $venvDir
    }
}

Write-Host "==> Creating virtualenv at $venvDir"
& $python -m venv $venvDir

$venvPython = Join-Path $venvDir 'Scripts\python.exe'
if (-not (Test-Path $venvPython)) { throw "venv creation failed: $venvPython was not created." }
if (-not (Test-Path $pythonw))    { throw "venv creation failed: $pythonw was not created." }

# Invoked via python -m, so pip can replace itself without a file lock.
& $venvPython -m pip install --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) { Write-Warning 'Could not upgrade pip; continuing with the bundled version.' }

& $venvPython -m pip install --quiet -r (Join-Path $agentDir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Failed to install agent dependencies.' }

& $venvPython -c 'import psutil' 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'psutil did not install correctly; the agent cannot run.' }

# The launcher carries the config, since Scheduled Tasks have no equivalent
# of systemd's EnvironmentFile.
Write-Host "==> Writing $launcher"
@"
@echo off
set HOMELAB_SERVER_URL=$reportUrl
set HOMELAB_AGENT_KEY=$Key
set HOMELAB_INTERVAL=$Interval
set HOMELAB_IS_VM=$Vm
"$pythonw" "$(Join-Path $agentDir 'agent.py')"
"@ | Set-Content -Path $launcher -Encoding ASCII

# The launcher holds the shared agent key: restrict it to admins and SYSTEM.
icacls $launcher /inheritance:r /grant:r 'BUILTIN\Administrators:(F)' 'NT AUTHORITY\SYSTEM:(F)' | Out-Null

Write-Host "==> Registering scheduled task '$TaskName'"
$action = New-ScheduledTaskAction -Execute $launcher -WorkingDirectory $agentDir

if ($AtLogon) {
    $trigger   = New-ScheduledTaskTrigger -AtLogOn
    $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -RunLevel Highest
} else {
    $trigger   = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
}

# Restart on failure, and never stop a long-running agent.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

# Stop an agent that is already running first: with MultipleInstances
# IgnoreNew a second start is ignored, which would leave the old code running.
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*agent.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 8

# Verify an agent process is actually alive. The task reporting "Ready" only
# means the launcher exited, which is what a crashed agent looks like too.
$running = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*agent.py*' }

Write-Host ''
if ($running) {
    Write-Host "Agent running (pid $($running.ProcessId))."
    Write-Host 'This node should appear on the dashboard within a few seconds.'
} else {
    Write-Warning 'The scheduled task started but no agent process is running.'
    Write-Host 'Reproduce the failure with output visible:'
    Write-Host "    cd `"$agentDir`""
    Write-Host "    `"$venvPython`" -u agent.py"
    Write-Host '(set HOMELAB_SERVER_URL and HOMELAB_AGENT_KEY in that shell first)'
}
Write-Host ''
Write-Host "To stop it:   Stop-ScheduledTask -TaskName '$TaskName'"
Write-Host "To remove it: Unregister-ScheduledTask -TaskName '$TaskName'"
