<#
.SYNOPSIS
    Run the collector permanently in the background, via Task Scheduler.

.DESCRIPTION
    The archive is only worth what it covers, and it cannot be backfilled --
    an hour you did not collect is gone. So the collector should not depend on
    you remembering to open a terminal.

    This registers a scheduled task that starts at logon, runs with no visible
    window, and restarts itself if it dies. Reboot, close every terminal, and
    it keeps going.

    Not a Windows Service, deliberately. A service runs as SYSTEM, which has a
    different HOME, a different PATH, and no access to your conda install --
    three separate ways to spend an evening. A logon task runs as you, with
    your environment, which is what the poller already expects.

.EXAMPLE
    .\collector.ps1 install     # register and start it
    .\collector.ps1 status      # is it running, and how much has it collected
    .\collector.ps1 logs        # tail the log
    .\collector.ps1 restart
    .\collector.ps1 uninstall
#>

param(
    [Parameter(Position = 0)]
    [ValidateSet("install", "uninstall", "start", "stop", "restart", "status", "logs")]
    [string]$Action = "status",

    [int]$Interval = 30,
    [int]$FlushEvery = 10
)

$ErrorActionPreference = "Stop"

$TaskName = "ANGELS Collector"
$Repo     = $PSScriptRoot
$Script   = Join-Path $Repo "scripts\ingest_aviation.py"
$LogFile  = Join-Path $Repo "data\logs\collector.log"
$Python   = (Get-Command python).Source

function Get-Task { Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }

# ---------------------------------------------------------------- install --

function Install-Collector {
    if (Get-Task) {
        Write-Host "Already installed. Reinstalling with current settings." -ForegroundColor Yellow
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }

    if (-not (Test-Path (Join-Path $Repo ".env"))) {
        Write-Host "WARNING: no .env found. The collector will start and fail." -ForegroundColor Yellow
    }

    $args = "`"$Script`" --interval $Interval --flush-every $FlushEvery --log-file `"$LogFile`""
    $action = New-ScheduledTaskAction -Execute $Python -Argument $args -WorkingDirectory $Repo

    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

    # Every one of these matters on a laptop:
    #   RestartCount/Interval  -- a dropped wifi connection should cost a
    #                             minute, not the rest of the month
    #   ExecutionTimeLimit 0   -- default is 3 days, after which Windows would
    #                             silently kill it
    #   DontStopIfGoingOnBatteries / AllowStartIfOnBatteries
    #                          -- otherwise unplugging ends your collection
    #   StartWhenAvailable     -- catch up after a missed logon trigger
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
        -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description "ANGELS: poll OpenSky into the archive." | Out-Null

    Start-ScheduledTask -TaskName $TaskName

    Write-Host ""
    Write-Host "  Installed and started." -ForegroundColor Green
    Write-Host "  Polling every ${Interval}s, flushing every $FlushEvery polls."
    Write-Host "  Log: $LogFile"
    Write-Host ""
    Write-Host "  It now starts at logon and restarts itself if it dies." -ForegroundColor DarkGray
    Write-Host "  It does NOT run while the machine is asleep -- nothing can" -ForegroundColor DarkGray
    Write-Host "  fix that, but the heartbeat log records the gap." -ForegroundColor DarkGray
    Write-Host ""
}

function Uninstall-Collector {
    if (-not (Get-Task)) { Write-Host "Not installed."; return }
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed. The archive and logs are untouched." -ForegroundColor Green
}

# ----------------------------------------------------------------- status --

function Show-Status {
    $task = Get-Task
    Write-Host ""
    if (-not $task) {
        Write-Host "  Not installed." -ForegroundColor Yellow
        Write-Host "  Run: .\collector.ps1 install"
        Write-Host ""
        return
    }

    $info  = Get-ScheduledTaskInfo -TaskName $TaskName
    $state = $task.State
    $color = if ($state -eq "Running") { "Green" } else { "Yellow" }

    Write-Host "  Collector: " -NoNewline
    Write-Host $state -ForegroundColor $color
    Write-Host "  Last run:  $($info.LastRunTime)  (result $($info.LastTaskResult))"
    Write-Host ""

    # What actually landed on disk. The task saying "Running" is not the same
    # as data arriving, and this is the difference.
    $raw = Join-Path $Repo "data\raw\aviation"
    if (Test-Path $raw) {
        $files = Get-ChildItem $raw -Recurse -Filter *.parquet
        $hours = Get-ChildItem $raw -Directory | Sort-Object Name
        $mb    = [math]::Round(($files | Measure-Object Length -Sum).Sum / 1MB, 1)
        Write-Host "  Archive:   $($files.Count) files, $($hours.Count) hours, $mb MB"
        if ($hours.Count) {
            Write-Host "             $($hours[0].Name) to $($hours[-1].Name)"
        }
        $newest = $files | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($newest) {
            $mins = [math]::Round(((Get-Date) - $newest.LastWriteTime).TotalMinutes)
            $stale = $mins -gt ($Interval * $FlushEvery / 60 * 3)
            Write-Host "  Last file: $mins min ago" -ForegroundColor $(if ($stale) { "Yellow" } else { "Gray" })
            if ($stale) { Write-Host "             (stale -- check the log)" -ForegroundColor Yellow }
        }
    } else {
        Write-Host "  Archive:   empty" -ForegroundColor Yellow
    }

    $hb = Join-Path $Repo "data\raw\collector"
    if (Test-Path $hb) {
        $days = (Get-ChildItem $hb -Filter *.jsonl).Count
        Write-Host "  Heartbeat: $days day(s) logged"
    }

    # Am I double-polling? The question you cannot answer by looking at the
    # task, because a poller started by hand in a terminal is invisible to it.
    Write-Host ""
    $live = & python -c @"
import json,sys
sys.path.insert(0,'.')
from angels.core.uptime import running_collectors
from angels.config import RAW
for c in running_collectors(RAW):
    if c.get('alive'):
        print(f\"{c.get('collector','?')}|{c.get('pid')}|{c.get('session','?')}|{c.get('iso','?')}\")
"@ 2>$null

    if (-not $live) {
        Write-Host "  Live pollers: none holding a lock" -ForegroundColor Yellow
    } else {
        $rows = @($live)
        $color = if ($rows.Count -gt 1) { "Red" } else { "Green" }
        Write-Host "  Live pollers: $($rows.Count)" -ForegroundColor $color
        foreach ($r in $rows) {
            $f = $r -split "\|"
            Write-Host "    $($f[0])  pid $($f[1])  session $($f[2])  since $($f[3])"
        }
        if ($rows.Count -gt 1) {
            Write-Host ""
            Write-Host "    MORE THAN ONE. You are burning quota twice and" -ForegroundColor Red
            Write-Host "    duplicating rows. Stop all but one." -ForegroundColor Red
        }
    }
    Write-Host ""
}

function Show-Logs {
    if (-not (Test-Path $LogFile)) { Write-Host "No log yet at $LogFile"; return }
    Write-Host "Tailing $LogFile  (Ctrl+C to stop)" -ForegroundColor DarkGray
    Get-Content $LogFile -Tail 30 -Wait
}

switch ($Action) {
    "install"   { Install-Collector }
    "uninstall" { Uninstall-Collector }
    "start"     { Start-ScheduledTask -TaskName $TaskName; Write-Host "Started." }
    "stop"      { Stop-ScheduledTask  -TaskName $TaskName; Write-Host "Stopped." }
    "restart"   { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
                  Start-Sleep -Seconds 2
                  Start-ScheduledTask -TaskName $TaskName
                  Write-Host "Restarted." }
    "status"    { Show-Status }
    "logs"      { Show-Logs }
}
