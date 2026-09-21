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

    TWO FOOTPRINTS. -Aoi selects which one this command acts on:

        air     DC-Baltimore, every 30 s    -- depth. Fast enough to sample a
                                               turn, which orbit, loiter and
                                               the inversion all require.
        conus   continental US, every 10 min -- breadth. Coarse, but enough
                                               for gaps, identity and the
                                               distance-to-boundary work.

    They are separate scheduled tasks with separate logs, locks and archives,
    so installing one never disturbs the other. `status` with no -Aoi reports
    on both.

.EXAMPLE
    .\collector.ps1 install                 # register and start the DC box
    .\collector.ps1 install -Aoi conus      # and the national one
    .\collector.ps1 status                  # both, side by side
    .\collector.ps1 logs -Aoi conus         # tail one log
    .\collector.ps1 restart -Aoi air
    .\collector.ps1 uninstall -Aoi conus
#>

param(
    [Parameter(Position = 0)]
    [ValidateSet("install", "uninstall", "start", "stop", "restart", "status",
                 "logs", "clear-locks")]
    [string]$Action = "status",

    [ValidateSet("air", "conus")]
    [string]$Aoi = "air",

    # 0 means "use the AOI's own default" -- 30 s for air, 600 s for conus.
    # Spelled as a sentinel rather than a per-AOI default here because the
    # authoritative numbers live in angels/config.py, and having PowerShell
    # carry a second copy is how the two quietly drift apart.
    [int]$Interval = 0,
    [int]$FlushEvery = 0
)

$ErrorActionPreference = "Stop"

$Repo   = $PSScriptRoot
$Script = Join-Path $Repo "scripts\ingest_aviation.py"
# The scheduled task records an ABSOLUTE path to this interpreter and keeps
# using it for months. Taking whatever `python` means today would bake in the
# wrong one -- and on this machine that is ArcGIS Pro's 3.14, which cannot
# import the project. The collector would then fail silently at every logon.
. (Join-Path $PSScriptRoot "_env.ps1")
$Python = Assert-AngelsPython

# One place the two footprints are described on the PowerShell side. Keep the
# intervals in step with AOIS in angels/config.py.
#
# Flush is in POLLS, so what it costs you is Interval x Flush seconds of
# buffered data at risk. Ten polls is five minutes on the air box and a hundred
# minutes on the national one -- so the national box flushes more often in
# polls to lose less in time. Anything still in the buffer when the machine
# sleeps or the process is killed is gone, and unlike a gap it leaves no trace.
$Footprints = @{
    air   = @{ Task = "ANGELS Collector";       Dataset = "aviation";
               Log  = "collector.log";          Interval = 30;
               Label = "DC-Baltimore";          Credits = 1;  Flush = 10 }
    conus = @{ Task = "ANGELS Collector CONUS"; Dataset = "aviation-conus";
               Log  = "collector-conus.log";    Interval = 600;
               Label = "continental US";        Credits = 4;  Flush = 3 }
}

$AoiWasGiven = $PSBoundParameters.ContainsKey("Aoi")

$Fp       = $Footprints[$Aoi]
$TaskName = $Fp.Task
$LogFile  = Join-Path $Repo ("data\logs\" + $Fp.Log)
if ($Interval -le 0)   { $Interval = $Fp.Interval }
if ($FlushEvery -le 0) { $FlushEvery = $Fp.Flush }

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

    $args = "`"$Script`" --aoi $Aoi --interval $Interval --flush-every $FlushEvery --log-file `"$LogFile`""
    $action = New-ScheduledTaskAction -Execute $Python -Argument $args -WorkingDirectory $Repo

    # TWO triggers, and the second one is the one that matters.
    #
    # At logon alone, the collector gets exactly one chance per session to be
    # alive. If the process dies in a way Task Scheduler does not count as a
    # failure -- and 0xC000013A, the console-close exit, is exactly such a way
    # -- the task goes back to Ready and simply waits for the next logon. On a
    # laptop that stays logged in for a week, that is a week of silence with no
    # warning anywhere. This is not hypothetical: it cost nine days of the air
    # archive between 3 and 12 September 2026, and RestartCount did not fire.
    #
    # The heartbeat trigger closes it. Every fifteen minutes Windows tries to
    # start the task; MultipleInstances IgnoreNew means the attempt is discarded
    # if it is already running, and CollectorLock refuses a duplicate even if
    # that fails. So a dead collector costs at most fifteen minutes, and a live
    # one is never disturbed.
    $atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $revive = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
        -RepetitionInterval (New-TimeSpan -Minutes 15)

    # Every one of these matters on a laptop:
    #   RestartCount/Interval  -- a dropped wifi connection should cost a
    #                             minute, not the rest of the month
    #   ExecutionTimeLimit 0   -- default is 3 days, after which Windows would
    #                             silently kill it
    #   DontStopIfGoingOnBatteries / AllowStartIfOnBatteries
    #                          -- otherwise unplugging ends your collection
    #   StartWhenAvailable     -- catch up after a missed trigger
    #   MultipleInstances IgnoreNew
    #                          -- what makes the 15-minute revive trigger safe
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
        -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName $TaskName -Action $action `
        -Trigger @($atLogon, $revive) `
        -Settings $settings -Description "ANGELS: poll OpenSky into the archive ($Aoi)." | Out-Null

    Start-ScheduledTask -TaskName $TaskName

    $perDay = [math]::Round(86400 / $Interval) * $Fp.Credits

    Write-Host ""
    Write-Host "  Installed and started: $Aoi ($($Fp.Label))" -ForegroundColor Green
    $flushMin = [math]::Round($Interval * $FlushEvery / 60, 1)
    Write-Host "  Polling every ${Interval}s, writing a file every $FlushEvery polls (~$flushMin min)."
    Write-Host "  Quota: $($Fp.Credits) credit(s)/poll, about $perDay credits/day of 4000."
    Write-Host "  Log: $LogFile"
    Write-Host ""
    Write-Host "  Revive trigger: retried every 15 min if it is not running." -ForegroundColor DarkGray
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

function Show-One($key) {
    $fp    = $Footprints[$key]
    $name  = $fp.Task
    $task  = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    $ivl   = $fp.Interval

    Write-Host "  $key -- $($fp.Label)" -ForegroundColor Cyan

    if (-not $task) {
        Write-Host "    Task:      not installed" -ForegroundColor Yellow
        Write-Host "               .\collector.ps1 install -Aoi $key"
        Write-Host ""
        return
    }

    $info  = Get-ScheduledTaskInfo -TaskName $name
    $state = $task.State
    $color = if ($state -eq "Running") { "Green" } else { "Yellow" }
    Write-Host "    Task:      " -NoNewline
    Write-Host $state -ForegroundColor $color
    Write-Host "    Last run:  $($info.LastRunTime)  (result $($info.LastTaskResult))"

    # What actually landed on disk. The task saying "Running" is not the same
    # as data arriving, and this is the difference.
    $raw = Join-Path $Repo ("data\raw\" + $fp.Dataset)
    if (Test-Path $raw) {
        $files = Get-ChildItem $raw -Recurse -Filter *.parquet
        $hours = Get-ChildItem $raw -Directory | Sort-Object Name
        $mb    = [math]::Round(($files | Measure-Object Length -Sum).Sum / 1MB, 1)
        Write-Host "    Archive:   $($files.Count) files, $($hours.Count) hours, $mb MB"
        if ($hours.Count) {
            Write-Host "               $($hours[0].Name) to $($hours[-1].Name)"
        }
        $newest = $files | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($newest) {
            $mins  = [math]::Round(((Get-Date) - $newest.LastWriteTime).TotalMinutes)
            # Staleness is relative to how often this footprint flushes. The
            # national box writes every ten polls at ten minutes apart, so
            # "no file for an hour" is normal there and alarming for air.
            $expected = $ivl * $fp.Flush / 60          # minutes between files
            $stale = $mins -gt ($expected * 3)

            # Say it in the unit the number actually deserves. "13587 min ago"
            # is a number you skim past; "9.4 DAYS" is not, and this exact
            # outage went unnoticed for nine days behind the smaller phrasing.
            # Written as a plain block, not an inline if/elseif expression:
            # PowerShell ends the statement at the newline before `elseif`,
            # so the multi-line expression form silently parses as something
            # else entirely.
            if ($mins -ge 1440) {
                $ago = "{0:N1} days" -f ($mins / 1440)
            } elseif ($mins -ge 60) {
                $ago = "{0:N1} hours" -f ($mins / 60)
            } else {
                $ago = "$mins min"
            }

            if ($mins -ge 1440) {
                Write-Host "    Last file: $ago ago  -- NOT COLLECTING" -ForegroundColor Red
                Write-Host "               $([math]::Round($mins/60)) hours of archive lost and unrecoverable." -ForegroundColor Red
                Write-Host "               Fix: .\collector.ps1 install -Aoi $key" -ForegroundColor Red
            } elseif ($stale) {
                Write-Host "    Last file: $ago ago" -ForegroundColor Yellow
                Write-Host "               (stale -- expected one every $expected min. Check the log.)" -ForegroundColor Yellow
            } else {
                Write-Host "    Last file: $ago ago" -ForegroundColor Gray
            }
        }
    } else {
        Write-Host "    Archive:   empty" -ForegroundColor Yellow
    }
    Write-Host ""
}

function Show-Status {
    Write-Host ""
    # $AoiWasGiven is captured at script scope; $PSBoundParameters inside a
    # function describes the FUNCTION's arguments, not the script's, and
    # reading it here would always come back empty.
    if ($AoiWasGiven) {
        Show-One $Aoi
    } else {
        foreach ($k in @("air", "conus")) { Show-One $k }
    }

    $hb = Join-Path $Repo "data\raw\collector"
    if (Test-Path $hb) {
        $days = (Get-ChildItem $hb -Filter *.jsonl).Count
        Write-Host "  Heartbeat: $days day(s) logged"
    }

    # Which pollers actually hold a lock right now. Not answerable from the
    # scheduled tasks alone, because a poller started by hand in a terminal is
    # invisible to them.
    #
    # Two live pollers is now CORRECT, provided they are different footprints.
    # The lock is per collector name, so duplicates of one name cannot both
    # acquire -- what this check is really looking for is a stale lock left by
    # a crash, and an unexpected name.
    $live = & $Python -c @"
import sys
sys.path.insert(0,'.')
from angels.core.uptime import running_collectors
from angels.config import RAW
for c in running_collectors(RAW):
    print(f\"{c.get('collector','?')}|{c.get('pid')}|{c.get('session','?')}|{c.get('iso','?')}|{int(bool(c.get('alive')))}\")
"@ 2>$null

    Write-Host ""
    if (-not $live) {
        Write-Host "  Live pollers: none holding a lock" -ForegroundColor Yellow
    } else {
        $rows  = @($live)
        $alive = @($rows | Where-Object { ($_ -split "\|")[4] -eq "1" })
        Write-Host "  Live pollers: $($alive.Count)" -ForegroundColor $(if ($alive.Count) { "Green" } else { "Yellow" })
        foreach ($r in $rows) {
            $f  = $r -split "\|"
            $ok = $f[4] -eq "1"
            $tag = if ($ok) { "" } else { "  (STALE LOCK -- process gone)" }
            Write-Host "    $($f[0])  pid $($f[1])  session $($f[2])  since $($f[3])$tag" `
                -ForegroundColor $(if ($ok) { "Gray" } else { "Yellow" })
        }
        if ($rows.Count -ne $alive.Count) {
            Write-Host "    Clear them with: .\collector.ps1 clear-locks" -ForegroundColor DarkGray
        }

        $names = $alive | ForEach-Object { ($_ -split "\|")[0] }
        $dupes = $names | Group-Object | Where-Object { $_.Count -gt 1 }
        if ($dupes) {
            Write-Host ""
            Write-Host "    DUPLICATE COLLECTOR: $($dupes.Name). Double quota and" -ForegroundColor Red
            Write-Host "    duplicate rows. Stop all but one." -ForegroundColor Red
        }

        # The number that actually constrains you. Worth seeing every time,
        # because exhausting the bucket does not raise -- it just 429s the
        # rest of the day and leaves a hole at the same hour every day.
        # Collector name and dataset name are deliberately the same string in
        # both footprints ("aviation", "aviation-conus"), so this is a
        # straight lookup rather than a mapping to keep in step.
        $spend = 0
        foreach ($n in $names) {
            foreach ($k in $Footprints.Keys) {
                if ($Footprints[$k].Dataset -eq $n) {
                    $spend += [math]::Round(86400 / $Footprints[$k].Interval) * $Footprints[$k].Credits
                    break
                }
            }
        }
        if ($spend) {
            $color = if ($spend -gt 4000) { "Red" } elseif ($spend -gt 3600) { "Yellow" } else { "Gray" }
            Write-Host ""
            Write-Host "  Quota: about $spend credits/day of 4000" -ForegroundColor $color
        }
    }
    Write-Host ""
}

function Clear-StaleLocks {
    # A lock whose process is gone is already taken over automatically on the
    # next start -- acquire() checks liveness before believing it. This exists
    # only so `status` stops reporting a corpse, because a warning you have
    # learned to ignore is worse than no warning.
    $removed = & $Python -c @"
import sys
sys.path.insert(0,'.')
from pathlib import Path
from angels.core.uptime import running_collectors
from angels.config import RAW
for c in running_collectors(RAW):
    if not c.get('alive'):
        Path(c['lock']).unlink(missing_ok=True)
        print(c.get('collector','?'))
"@ 2>$null

    if (-not $removed) {
        Write-Host "No stale locks." -ForegroundColor Green
    } else {
        foreach ($r in @($removed)) {
            Write-Host "Cleared stale lock: $r" -ForegroundColor Green
        }
    }
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
    "status"      { Show-Status }
    "logs"        { Show-Logs }
    "clear-locks" { Clear-StaleLocks }
}
