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
                 "logs", "clear-locks", "windows", "install-all")]
    [string]$Action = "status",

    [ValidateSet("air", "conus", "sea", "sea-conus", "air-indep")]
    [string]$Aoi = "air",

    # 0 means "use the AOI's own default" -- 30 s for air, 600 s for conus.
    # Spelled as a sentinel rather than a per-AOI default here because the
    # authoritative numbers live in angels/config.py, and having PowerShell
    # carry a second copy is how the two quietly drift apart.
    [int]$Interval = 0,
    [int]$FlushEvery = 0,

    # `windows` only: kill a collector that is already running by hand,
    # instead of refusing to start a second copy of it. Documented as lossy
    # because it is -- see Start-Windows.
    [switch]$Force,

    # Register the task to run as the SYSTEM account instead of as you.
    #
    # THE POINT OF THIS IS THAT IT NEEDS NO PASSWORD. A task registered as a
    # user runs only while that user is logged on, and making it survive a
    # reboot-with-nobody-logged-in means storing the account password in the
    # Task Scheduler. SYSTEM is a built-in account with no password to store,
    # it starts at boot before anyone logs in, and it keeps running across
    # logoff -- which is what "nothing but the battery stops it" actually
    # requires.
    #
    # What it costs: the process is not in your session, so it has no
    # console to Ctrl-C and does not appear in a plain Task Manager list
    # (tick "Show processes from all users"). Stop it with
    # `.\collector.ps1 stop -Aoi <box>`. Everything the collector touches --
    # the interpreter, the repo, .env, data\ -- is readable and writable by
    # SYSTEM, so nothing else changes.
    [switch]$AsSystem
)

$ErrorActionPreference = "Stop"

$Repo   = $PSScriptRoot
$Script = Join-Path $Repo "scripts\ingest_aviation.py"   # overridden per footprint below
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

    # THE SEA IS A DIFFERENT SHAPE OF COLLECTOR and the fields say so.
    #
    # Credits = 0: aisstream has no quota, so unlike the aviation boxes this
    # one cannot starve the archive by running. Interval = 0: it does not
    # poll at all -- it holds a socket open and is pushed to, so an interval
    # would be a fiction in a config table. What it costs instead is a
    # CONNECTION: aisstream allows three per account, and the API server
    # opens one of its own, so two collectors plus a viewer is the ceiling.
    'sea' = @{ Task = "ANGELS Collector SEA";   Dataset = "maritime-live";
               Log  = "collector-sea.log";      Interval = 0;
               Label = "Chesapeake-Delaware";   Credits = 0;  Flush = 0
               FlushSeconds = 120;
               Script = "scripts\ingest_maritime.py";        SeaAoi = "sea" }
    'sea-conus' = @{
               Task = "ANGELS Collector SEA CONUS";
               Dataset = "maritime-live-conus";
               Log  = "collector-sea-conus.log"; Interval = 0;
               Label = "US waters";              Credits = 0;  Flush = 0
               FlushSeconds = 120;
               Script = "scripts\ingest_maritime.py";        SeaAoi = "conus" }

    # THE INDEPENDENT AVIATION CHANNEL, and the reason it is a fifth collector
    # rather than a change to the first.
    #
    # The two aviation boxes above poll OpenSky, which returns
    # position_source = 0 -- ADS-B, self-reported -- on every row it has ever
    # given us: 152,040 of them across 224 hours. That is one side of a
    # two-sided question. adsb.fi labels each aircraft's position source, so
    # MLAT and TIS-B targets arrive alongside the cooperative ones FROM THE
    # SAME RECEIVER NETWORK. Mixing feeds instead would make every coverage
    # difference between two networks look like a behavioural one, which is
    # the mistake the searched-water denominator exists to prevent on the
    # maritime side.
    #
    # Credits = 0: adsb.fi has no quota. What it asks for instead is
    # courtesy -- one request a second, non-commercial use, attribution. We
    # poll every 30 s, and ingest_adsbfi.py refuses an interval under 5 s.
    'air-indep' = @{
               Task = "ANGELS Collector AIR INDEP";
               Dataset = "aviation-adsbfi";
               Log  = "collector-air-indep.log"; Interval = 30;
               Label = "DC-Baltimore, independent";
               Credits = 0;  Flush = 10;
               Script = "scripts\ingest_adsbfi.py" }
}

# ONE ordered list of footprints, used by status, process detection,
# install-all and windows. Four of those five places used to carry their own
# copy of @("air","conus","sea","sea-conus"); adding the fifth collector on
# 2026-09-25 would have had to find every one of them, and missing the
# process-detection copy would have made the new collector invisible to
# `status` and killable by `install-all` without ever appearing in a list.
$Order = @("air", "conus", "sea", "sea-conus", "air-indep")

$AoiWasGiven = $PSBoundParameters.ContainsKey("Aoi")

$Fp       = $Footprints[$Aoi]
$TaskName = $Fp.Task
if ($Fp.Script) { $Script = Join-Path $Repo $Fp.Script }
$IsSea    = [bool]$Fp.SeaAoi
$LogFile  = Join-Path $Repo ("data\logs\" + $Fp.Log)
if ($Interval -le 0)   { $Interval = $Fp.Interval }
if ($FlushEvery -le 0) { $FlushEvery = $Fp.Flush }

function Get-Task { Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }

# ---------------------------------------------------------------- install --

function Test-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Install-Collector {
    # CHECKED BEFORE THE OLD TASK IS TOUCHED.
    #
    # Registering a task under NT AUTHORITY\SYSTEM needs an elevated shell.
    # The first version checked nothing: it unregistered the existing task,
    # THEN failed on Register with "Access is denied", and left the box with
    # no collector at all -- while printing "Installed and started". Order
    # matters more than the check does. Nothing is removed until the thing
    # replacing it is known to be possible.
    if ($AsSystem -and -not (Test-Elevated)) {
        Write-Host ""
        Write-Host "  -AsSystem needs an elevated PowerShell." -ForegroundColor Red
        Write-Host "  Registering a task under the SYSTEM account is an"
        Write-Host "  administrative act; this shell is not elevated, so the"
        Write-Host "  install would fail AFTER removing the task you have."
        Write-Host ""
        Write-Host "  Start menu -> Windows PowerShell -> Run as administrator, then:" -ForegroundColor Cyan
        Write-Host "    cd '$Repo'"
        Write-Host "    .\collector.ps1 install-all -AsSystem"
        Write-Host ""
        Write-Host "  Nothing has been changed." -ForegroundColor Green
        Write-Host ""
        exit 1
    }

    if (Get-Task) {
        Write-Host "Already installed. Reinstalling with current settings." -ForegroundColor Yellow
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }

    if (-not (Test-Path (Join-Path $Repo ".env"))) {
        Write-Host "WARNING: no .env found. The collector will start and fail." -ForegroundColor Yellow
    }

    # The two collectors take different flags because they are different
    # kinds of thing: one polls on an interval, the other holds a socket and
    # flushes on rows or seconds.
    if ($IsSea) {
        $args = "`"$Script`" --aoi $($Fp.SeaAoi) --log-file `"$LogFile`""
    } else {
        $args = "`"$Script`" --aoi $Aoi --interval $Interval --flush-every $FlushEvery --log-file `"$LogFile`""
    }
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
    # AT STARTUP when running as SYSTEM, AT LOGON otherwise -- because a
    # user task cannot start before its user exists. This is the difference
    # between "collects whenever you are signed in" and "collects whenever
    # the machine is on".
    $atLogon = if ($AsSystem) {
        New-ScheduledTaskTrigger -AtStartup
    } else {
        New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    }
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
        -MultipleInstances IgnoreNew `
        -WakeToRun `
        -Priority 4
    # WakeToRun: a sleeping laptop is a stopped collector, and sleep is the
    #   most common way collection dies quietly overnight. This lets the
    #   15-minute revive trigger wake the machine rather than wait for it.
    # Priority 4: default is 7, which is below normal. These processes are
    #   idle almost all the time -- they wait on a socket or a timer -- so
    #   giving them ordinary priority costs nothing and stops a compile or a
    #   backup from starving a poll.
    #
    # NOT covered, and it cannot be covered from here: the task is registered
    # to run AS THE LOGGED-ON USER, so a reboot with nobody logging in leaves
    # every collector stopped. Making it survive that means "Run whether user
    # is logged on or not", which requires the account password stored in the
    # Task Scheduler. That is the user's to enter, in Task Scheduler, not
    # something a script should ask for. See the note printed after install.

    $principal = if ($AsSystem) {
        New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\SYSTEM" `
            -LogonType ServiceAccount -RunLevel Highest
    } else { $null }

    # Splatted rather than a long backtick-continued call, because the
    # Principal is present for SYSTEM and absent otherwise, and a parameter
    # you cannot pass as $null is awkward to make conditional any other way.
    $reg = @{
        TaskName    = $TaskName
        Action      = $action
        Trigger     = @($atLogon, $revive)
        Settings    = $settings
        Description = $(if ($IsSea) {
            "ANGELS: listen to aisstream into the archive ($Aoi)."
        } else {
            "ANGELS: poll OpenSky into the archive ($Aoi)."
        })
    }
    if ($principal) { $reg.Principal = $principal }

    # -ErrorAction is explicit because these are CIM cmdlets: their failures
    # did NOT honour $ErrorActionPreference = "Stop" at the top of this file,
    # so the script sailed past "Access is denied" and announced success.
    Register-ScheduledTask @reg -ErrorAction SilentlyContinue | Out-Null

    # VERIFIED, not assumed. The only claim worth printing is one that was
    # checked, and the check is free.
    if (-not (Get-Task)) {
        Write-Host ""
        Write-Host "  FAILED to install $Aoi ($($Fp.Label))." -ForegroundColor Red
        Write-Host "  The task is NOT registered and nothing is collecting"  -ForegroundColor Red
        Write-Host "  for this box." -ForegroundColor Red
        if ($AsSystem) {
            Write-Host "  Most likely: this shell is not elevated." -ForegroundColor Yellow
        }
        Write-Host ""
        return
    }

    Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

    Write-Host ""
    Write-Host "  Installed and started: $Aoi ($($Fp.Label))" -ForegroundColor Green
    if ($IsSea) {
        # No interval and no quota, so the aviation summary would be a
        # division by zero AND a sentence about credits this feed does not
        # spend. What it spends is one of three connections per account.
        Write-Host "  Listening on a socket -- pushed to, not polled. No quota."
        Write-Host "  Uses 1 of aisstream's 3 connections per account; the API"
        Write-Host "  server opens one more when you look at the sea."
        Write-Host "  Writes a file every 20,000 rows or 2 min, whichever first."
    } else {
        $perDay = [math]::Round(86400 / $Interval) * $Fp.Credits
        $flushMin = [math]::Round($Interval * $FlushEvery / 60, 1)
        Write-Host "  Polling every ${Interval}s, writing a file every $FlushEvery polls (~$flushMin min)."
        Write-Host "  Quota: $($Fp.Credits) credit(s)/poll, about $perDay credits/day of 4000."
    }
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

function Format-TaskResult($code) {
    # A BARE 2147946720 IS NOT A STATUS REPORT.
    #
    # Task Scheduler reports its last result as a large decimal, which is an
    # HRESULT printed in the one base that hides what it is. Unreadable is
    # worse than absent here: a number nobody decodes gets skimmed, and the
    # two that matter most on this machine look equally alarming and mean
    # opposite things.
    $hex = "0x{0:X8}" -f $code
    switch ($code) {
        0          { "$hex  ok" }
        267008     { "$hex  ready" }
        267009     { "$hex  currently running" }
        267010     { "$hex  DISABLED" }
        267011     { "$hex  has not run yet" }
        267014     { "$hex  stopped before it finished" }
        2147942401 { "$hex  the executable was not found" }
        2147943645 { "$hex  the service is not available (starting up or shutting down)" }
        2147946720 { "$hex  refused -- already running (normal: revive trigger)" }
        default    { $hex }
    }
}

function Show-One($key) {
    $fp    = $Footprints[$key]
    $name  = $fp.Task
    $ivl   = $fp.Interval

    Write-Host "  $key -- $($fp.Label)" -ForegroundColor Cyan

    # "CANNOT SEE IT" IS NOT "IT IS NOT THERE", and here the difference is
    # destructive.
    #
    # On 2026-09-23 a non-elevated window reported all four tasks as "not
    # installed" and helpfully printed the install command -- while all four
    # were registered, running as SYSTEM, and collecting. Following that
    # advice re-registers four working tasks, and the reinstall path
    # unregisters before it registers. A task owned by the SYSTEM account is
    # not readable from an ordinary shell, and an empty answer to a question
    # you were not allowed to ask is not an answer.
    $task    = $null
    $taskErr = $null
    try   { $task = Get-ScheduledTask -TaskName $name -ErrorAction Stop }
    catch { $taskErr = $_ }

    if (-not $task) {
        if ($taskErr -and $taskErr.CategoryInfo.Category -ne "ObjectNotFound") {
            Write-Host "    Task:      CANNOT READ -- $($taskErr.CategoryInfo.Category)" -ForegroundColor Red
            Write-Host "               Not the same as 'not installed'. Do not install over this." -ForegroundColor Red
        } elseif (-not (Test-Elevated)) {
            Write-Host "    Task:      not visible from this window" -ForegroundColor Yellow
            Write-Host "               A task registered to SYSTEM cannot be read by a"
            Write-Host "               non-elevated shell. This is NOT 'not installed' --"
            Write-Host "               re-run as administrator before installing anything."
        } else {
            Write-Host "    Task:      not installed" -ForegroundColor Yellow
            Write-Host "               .\collector.ps1 install -Aoi $key"
        }
        Write-Host ""
        return
    }

    $info  = Get-ScheduledTaskInfo -TaskName $name
    $state = $task.State
    $color = if ($state -eq "Running") { "Green" } else { "Yellow" }
    Write-Host "    Task:      " -NoNewline
    Write-Host $state -ForegroundColor $color
    Write-Host "    Last run:  $($info.LastRunTime)  $(Format-TaskResult $info.LastTaskResult)"

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
            # MINUTES BETWEEN FILES, and the two shapes of collector do
            # not compute it the same way.
            #
            # A poller writes every Flush polls at Interval seconds apart. A
            # socket collector has neither: both are 0 in the table, on
            # purpose, because an interval printed beside a pushed feed
            # would be a fiction. The multiplication therefore gave 0, and
            # `$mins -gt 0` called every sea file stale the minute after it
            # was written -- status has been crying wolf about both sea
            # boxes since the day they were installed, in the one line whose
            # whole job is to be believed. It is also why it offered to
            # expect "one every 0 min".
            #
            # A cadence of zero now means UNKNOWN, and an unknown cadence
            # makes no claim at all rather than the worst possible one.
            if ($fp.FlushSeconds) { $expected = $fp.FlushSeconds / 60 }
            elseif ($ivl -gt 0 -and $fp.Flush -gt 0) { $expected = $ivl * $fp.Flush / 60 }
            else { $expected = 0 }
            $stale = ($expected -gt 0) -and ($mins -gt ($expected * 3))

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
                Write-Host ("               (stale -- expected one every {0:N1} min. Check the log.)" -f `
                            $expected) -ForegroundColor Yellow
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
        foreach ($k in $Order) { Show-One $k }

        # The table above reads the Task Scheduler, which cannot see a
        # collector someone started in a terminal -- so a footprint can show
        # "not installed" while it is very much collecting.
        $hand = Find-CollectorProcesses
        if ($hand.Count) {
            Write-Host ""
            Write-Host "  Collector processes:" -ForegroundColor Cyan
            foreach ($k in $hand.Keys) {
                foreach ($p in $hand[$k]) {
                    # SYSTEM means the Task Scheduler started it. Anything
                    # else means a person did, and a person's copy running
                    # beside a task's is the duplicate this file exists to
                    # prevent. An owner that could not be read says so
                    # rather than picking one.
                    $sys = ($p.Owner -match 'SYSTEM$')
                    if (-not $p.Owner) { $who = "owner unreadable" }
                    elseif ($sys)      { $who = "scheduled task (SYSTEM)" }
                    else               { $who = "by hand -- $($p.Owner)" }
                    $col = if ($sys) { "Gray" } else { "Yellow" }
                    Write-Host ("    {0,-10} pid {1,-6} {2}" -f $k, $p.ProcessId, $who) `
                               -ForegroundColor $col
                }
            }
        }
        # Printed whether or not anything was found: a blind scan that
        # happens to match one collector is still blind about the rest.
        Show-ProcScanWarning
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

function Find-CollectorProcesses {
    # A SCHEDULED TASK IS NOT THE ONLY WAY ONE OF THESE RUNS.
    #
    # `stop` only reaches the Task Scheduler, so a collector started by hand
    # in a terminal is invisible to it -- and the maritime one was started
    # that way before it had a lock to refuse a duplicate. Opening a second
    # window for it would give two processes writing the same archive, which
    # errors nowhere and doubles every position.
    #
    # The command line is the only thing that tells one python.exe from
    # another, and it has to match the SCRIPT as well as the --aoi, because
    # air and conus share ingest_aviation.py and sea-conus passes "--aoi
    # conus" to a different script entirely.
    #
    # AND THE COMMAND LINE IS A PRIVILEGED READ.
    #
    # Win32_Process lists every process no matter who asks, but it hands back
    # CommandLine as $null for any process the caller cannot open: another
    # account's, SYSTEM's, or one running at a higher integrity level than
    # this shell. A non-elevated window therefore sees four python.exe, four
    # empty command lines, matches none of them, and reports that nothing is
    # running -- which is a lie told in the same words as the truth.
    #
    # On 2026-09-23 that is exactly what happened, and the caller took the
    # empty answer as permission to unregister all four scheduled tasks.
    # Confirmed the same day by running the identical query twice: four rows
    # from an elevated shell, zero from the conda prompt, same machine, same
    # minute, same four collectors.
    #
    # So the scan now reports being BLIND separately from finding NOTHING,
    # and Test-ProcScanBlind below is what callers must consult before they
    # act on an empty result.
    $found = @{}
    $script:LastProcScan = @{
        Python     = 0
        Visible    = 0
        Blind      = 0
        Matched    = 0
        Elevated   = (Test-Elevated)
        Enumerated = $false
    }

    try {
        $all = @(Get-CimInstance Win32_Process -ErrorAction Stop |
                 Where-Object { $_.Name -match '^pythonw?\.exe$' })
    } catch {
        # Not "no collectors". No answer at all.
        $script:LastProcScan.Blind = -1
        return $found
    }

    $script:LastProcScan.Enumerated = $true
    $script:LastProcScan.Python  = $all.Count
    $script:LastProcScan.Visible = @($all | Where-Object { $_.CommandLine }).Count
    $script:LastProcScan.Blind   = $all.Count - $script:LastProcScan.Visible

    $procs = @($all | Where-Object { $_.CommandLine -match 'ingest_(aviation|maritime|adsbfi)\.py' })
    $script:LastProcScan.Matched = $procs.Count

    foreach ($k in $Order) {
        $fp     = $Footprints[$k]
        # The script LEAF for this footprint, taken from the table rather than
        # assumed. This line read `if ($fp.Script) { "ingest_maritime.py" }`,
        # which was true while the only footprints with their own script were
        # the two sea ones -- and silently wrong the moment a third kind of
        # collector arrived.
        $script = if ($fp.Script) { Split-Path $fp.Script -Leaf }
                  else            { "ingest_aviation.py" }
        $aoiArg = if ($fp.SeaAoi) { $fp.SeaAoi } else { $k }
        $hit = @($procs | Where-Object {
            $_.CommandLine -like "*$script*" -and
            $_.CommandLine -match ("--aoi\s+" + [regex]::Escape($aoiArg) + "(\s|$)")
        })
        # WHO OWNS IT IS WHAT SAYS WHERE IT CAME FROM.
        #
        # A matching command line does not distinguish a collector someone
        # started in a terminal from one the Task Scheduler started -- they
        # are the same program with the same arguments. Until 2026-09-23
        # status printed every match under "Also running by hand (not
        # scheduled tasks)", which on this machine was four SYSTEM-owned
        # task processes being described as the one thing they were not.
        #
        # The owner settles it. A task registered -AsSystem runs as
        # NT AUTHORITY\SYSTEM, and nobody starts a collector by hand as
        # SYSTEM. It is also the same fact that explains the blindness: a
        # SYSTEM-owned process is exactly the kind whose command line a
        # non-elevated shell may not read.
        foreach ($h in $hit) {
            # Written long-hand: `$x = try {...} catch {...}` is a
            # PowerShell 7 expression and a PARSE ERROR in the Windows
            # PowerShell 5.1 this machine runs, which would take the whole
            # script down rather than this one line.
            $owner = ""
            try {
                $o = Invoke-CimMethod -InputObject $h -MethodName GetOwner -ErrorAction Stop
                if ($o.User) { $owner = "$($o.Domain)\$($o.User)" }
            } catch { }
            Add-Member -InputObject $h -NotePropertyName Owner `
                       -NotePropertyValue $owner -Force
        }
        if ($hit.Count) { $found[$k] = $hit }
    }
    return $found
}

function Test-ProcScanBlind {
    # True when the last scan could not see what it was asked to look for.
    #
    # ONE unreadable command line is enough to be blind. The hidden line is
    # precisely where a running collector would be hiding, and a check that
    # only worries once EVERY line is hidden would have passed cleanly on the
    # day this went wrong.
    $s = $script:LastProcScan
    if (-not $s)             { return $true }
    if (-not $s.Enumerated)  { return $true }
    return ($s.Blind -ne 0)
}

function Show-ProcScanWarning {
    if (-not (Test-ProcScanBlind)) { return }
    $s = $script:LastProcScan
    Write-Host ""
    Write-Host "  THIS CHECK IS BLIND, WHICH IS NOT 'NOTHING IS RUNNING'." -ForegroundColor Red
    if ($s.Enumerated) {
        Write-Host ("  {0} python process(es) running, {1} command line(s) unreadable." -f `
                    $s.Python, $s.Blind) -ForegroundColor Yellow
    } else {
        Write-Host "  The process list could not be read at all." -ForegroundColor Yellow
    }
    if (-not $s.Elevated) {
        Write-Host "  This window is not elevated. A process started by another"   -ForegroundColor Yellow
        Write-Host "  account -- SYSTEM, or an Administrator terminal -- hides its" -ForegroundColor Yellow
        Write-Host "  command line from it. Re-run from Windows PowerShell (Admin)." -ForegroundColor Yellow
    }
    Write-Host "  Look for yourself, elevated:" -ForegroundColor DarkGray
    Write-Host "    Get-CimInstance Win32_Process | ? { `$_.CommandLine -match 'ingest_' } |" -ForegroundColor Cyan
    Write-Host "      Select ProcessId, CommandLine" -ForegroundColor Cyan
    Write-Host ""
}

function Install-All {
    # ONE COMMAND: everything running by hand stops, every footprint becomes a
    # scheduled task, every one starts.
    #
    # Ordinary use is `install -Aoi <box>` once per footprint. This exists
    # because doing that by hand is one chance per box to forget one, and a
    # forgotten collector is not a visible failure -- it is a gap in an
    # archive that cannot be backfilled, discovered weeks later.
    #
    # COUNTED, NOT SPELLED. This list said "four" in six places and grew to
    # five on 2026-09-25 when the independent aviation channel was added. A
    # hardcoded count in a summary line is how "All four installed" gets
    # printed over five boxes with one missing.

    $order = $Order
    $n     = $order.Count

    Write-Host ""
    Write-Host ("  Clean restart: all {0} collectors as scheduled tasks" -f $n) -ForegroundColor Magenta
    Write-Host ""

    # CHECKED HERE TOO, NOT ONLY IN Install-Collector.
    #
    # Install-Collector refuses before it removes ITS task -- but by the time
    # it is reached, this function has already killed every collector running
    # by hand and deleted the locks. The check has to come before the first
    # destructive step in THIS function, not before the first one in the
    # function it calls.
    if ($AsSystem -and -not (Test-Elevated)) {
        Write-Host "  -AsSystem needs an elevated PowerShell." -ForegroundColor Red
        Write-Host "  Start menu -> Windows PowerShell -> Run as administrator, then:" -ForegroundColor Cyan
        Write-Host "    cd '$Repo'"
        Write-Host "    .\collector.ps1 install-all -AsSystem"
        Write-Host ""
        Write-Host "  Nothing has been changed." -ForegroundColor Green
        Write-Host ""
        return
    }

    # 1. Anything running in a terminal. These are invisible to the Task
    #    Scheduler, so install would happily start a second copy alongside.
    $hand = Find-CollectorProcesses
    if ($hand.Count) {
        Write-Host "  Stopping every collector process found:" -ForegroundColor Yellow
        foreach ($k in $hand.Keys) {
            foreach ($p in $hand[$k]) {
                if (-not $p.Owner) { $who = "owner unreadable" }
                elseif ($p.Owner -match 'SYSTEM$') { $who = "scheduled task" }
                else { $who = "by hand -- $($p.Owner)" }
                Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
                Write-Host ("    {0,-10} pid {1,-6} {2}" -f $k, $p.ProcessId, $who)
            }
        }
        # A killed process skips its final flush. Nothing on disk is touched
        # or deleted -- what is lost is only what had not been written yet:
        # at most one flush interval. For the sea collectors that is up to
        # 20,000 rows or two minutes; for the air ones, a few polls. The
        # archive already on disk is untouched.
        Write-Host "    (their unwritten buffers are lost -- nothing on disk is)" -ForegroundColor DarkGray
        Start-Sleep -Seconds 3
    } elseif (Test-ProcScanBlind) {
        # THE EMPTY RESULT IS NOT EVIDENCE HERE.
        #
        # Step 3 unregisters every scheduled task before recreating them.
        # Doing that on a scan that could not read the command lines is how
        # four collectors got killed and four tasks got destroyed in the same
        # breath on 2026-09-23. An answer this shell is not privileged to
        # give is not an answer to act on.
        Show-ProcScanWarning
        if (-not $Force) {
            Write-Host "  REFUSING TO CONTINUE." -ForegroundColor Red
            Write-Host ("  The next step unregisters all {0} scheduled tasks. It will" -f $n) -ForegroundColor Red
            Write-Host "  not do that on an answer this window could not read." -ForegroundColor Red
            Write-Host ""
            Write-Host "  Re-run from an elevated PowerShell:" -ForegroundColor Cyan
            Write-Host "    .\collector.ps1 install-all -AsSystem" -ForegroundColor Cyan
            Write-Host "  or pass -Force if you already know what is running." -ForegroundColor DarkGray
            Write-Host ""
            return
        }
        Write-Host "  -Force given: continuing on an answer that could not be read." -ForegroundColor Yellow
        Write-Host "  Any collector running by hand is still running, and will now" -ForegroundColor Yellow
        Write-Host "  double every position it writes with its scheduled twin." -ForegroundColor Yellow
    } else {
        Write-Host "  Nothing running by hand." -ForegroundColor DarkGray
    }

    # 2. Stale locks from the processes just killed. A lock whose PID is dead
    #    is taken over automatically, but clearing them makes the next step's
    #    output honest rather than "already running, taking over".
    $lockDir = Join-Path $Repo "data\raw\collector"
    if (Test-Path $lockDir) {
        Get-ChildItem $lockDir -Filter *.lock -ErrorAction SilentlyContinue |
            Remove-Item -Force -ErrorAction SilentlyContinue
    }

    # 3. Install each one. Re-invoking this script per footprint rather than
    #    looping inside, because $TaskName, $Script and $Fp are resolved at
    #    script scope from $Aoi -- a loop here would install N tasks that
    #    all pointed at the first footprint.
    Write-Host ""
    foreach ($k in $order) {
        if ($AsSystem) { & $PSCommandPath install -Aoi $k -AsSystem }
        else            { & $PSCommandPath install -Aoi $k }
    }

    # WHAT IS ACTUALLY THERE, NOT WHAT WAS ATTEMPTED.
    #
    # The previous version printed "All four installed and started" no matter
    # what happened above it, and on 2026-09-23 it printed exactly that after
    # unregistering four tasks and failing to register any of them. A summary
    # line that cannot report failure is worse than no summary line, because
    # it is read instead of the output above it.
    Write-Host ""
    $missing = @($order | Where-Object {
        -not (Get-ScheduledTask -TaskName $Footprints[$_].Task -ErrorAction SilentlyContinue)
    })
    if ($missing.Count) {
        Write-Host ("  {0} of {1} installed. NOT INSTALLED: {2}" -f `
                    ($n - $missing.Count), $n, ($missing -join ", ")) -ForegroundColor Red
        Write-Host "  Those boxes are collecting nothing. Read the errors above." -ForegroundColor Red
        Write-Host ""
        return
    }
    Write-Host ("  All {0} installed and started." -f $n) -ForegroundColor Green
    Write-Host ""
    if ($AsSystem) {
        Write-Host "  Running as SYSTEM: starts at boot, before any logon," -ForegroundColor Green
        Write-Host "  and keeps running across logoff. No password stored."   -ForegroundColor Green
        Write-Host "  These have no console -- stop one with:"                -ForegroundColor DarkGray
        Write-Host "    .\collector.ps1 stop -Aoi <box>"                      -ForegroundColor DarkGray
    } else {
        Write-Host "  THESE RUN AS YOU, which means they stop at logoff and" -ForegroundColor Yellow
        Write-Host "  do not start after a reboot until you log in."         -ForegroundColor Yellow
        Write-Host ""
        Write-Host "  To close that gap WITHOUT a password, reinstall as the"
        Write-Host "  SYSTEM account -- a built-in account with none to store:"
        Write-Host "    .\collector.ps1 install-all -AsSystem" -ForegroundColor Cyan
        Write-Host ""
        Write-Host "  (The other way is Task Scheduler -> Properties -> General"
        Write-Host "  -> 'Run whether user is logged on or not', which stores"
        Write-Host "  your Windows password. -AsSystem avoids needing it.)"
    }
    Write-Host ""
    Write-Host "  Everything else is covered: batteries, sleep (WakeToRun),"  -ForegroundColor DarkGray
    Write-Host "  crashes (restart every minute, 999 times), a missed start"  -ForegroundColor DarkGray
    Write-Host "  (revive trigger every 15 min), and the 3-day default"        -ForegroundColor DarkGray
    Write-Host "  execution limit (removed)."                                  -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  Check: .\collector.ps1 status" -ForegroundColor Cyan
    Write-Host ""
}

function Start-Windows {
    # FOUR VISIBLE WINDOWS, one per collector.
    #
    # The Scheduled Task install is the right answer for collection that must
    # survive a reboot and a closed session. This is the other mode: you are
    # watching, you want to see each feed's own log scroll, and you want to
    # stop one with Ctrl-C without touching the others.
    #
    # The two cannot both run. CollectorLock refuses a second copy per
    # collector name -- which is the point of it -- so a foreground window
    # would simply exit with "already running" while the task held the lock.
    # So any installed task for a footprint is stopped first, and said so.

    $order = $Order
    Write-Host ""
    Write-Host "  Opening one window per collector." -ForegroundColor Magenta
    Write-Host ""

    # Stop the scheduled copies first, all of them, before opening anything --
    # otherwise the first window starts while a later task still holds its
    # lock and you get a mix of running and refused with no obvious pattern.
    foreach ($k in $order) {
        $t = Get-ScheduledTask -TaskName $Footprints[$k].Task -ErrorAction SilentlyContinue
        if ($t -and $t.State -eq "Running") {
            Stop-ScheduledTask -TaskName $Footprints[$k].Task
            Write-Host "    stopped scheduled task: $($Footprints[$k].Task)" -ForegroundColor Yellow
        }
    }
    if (Get-ChildItem (Join-Path $Repo "data\raw\collector") -Filter *.lock -ErrorAction SilentlyContinue) {
        Start-Sleep -Seconds 2      # let the stopped tasks release
    }

    # Now the ones the Task Scheduler cannot see.
    $running = Find-CollectorProcesses
    $skip = @()
    foreach ($k in $running.Keys) {
        $pids = ($running[$k] | ForEach-Object { $_.ProcessId }) -join ", "
        if ($Force) {
            # LOSSY, and said so. Killing skips the final flush, and a push
            # feed has no backfill -- up to 20,000 buffered AIS rows go with
            # it. Ctrl-C in the window flushes; this does not.
            $running[$k] | ForEach-Object {
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            }
            Write-Host "    killed $k (pid $pids) -- its buffer was NOT flushed" -ForegroundColor Red
        } else {
            $skip += $k
            Write-Host "    $k is already running by hand (pid $pids)" -ForegroundColor Yellow
        }
    }
    if ($skip.Count) {
        Write-Host ""
        Write-Host "  Not opening a window for: $($skip -join ', ')" -ForegroundColor Yellow
        Write-Host "  A second copy would write the same archive twice." -ForegroundColor DarkGray
        Write-Host "  Ctrl-C those windows (which flushes), then run this again," -ForegroundColor DarkGray
        Write-Host "  or use -Force to kill them now and lose their buffers." -ForegroundColor DarkGray
        Write-Host ""
    }
    if ($Force) { Start-Sleep -Seconds 2 }

    foreach ($k in $order) {
        if ($skip -contains $k) { continue }
        $fp  = $Footprints[$k]
        $log = Join-Path $Repo ("data\logs\" + $fp.Log)
        $scr = Join-Path $Repo $(if ($fp.Script) { $fp.Script } else { "scripts\ingest_aviation.py" })
        if ($fp.SeaAoi) {
            $a = "`"$scr`" --aoi $($fp.SeaAoi) --log-file `"$log`" -v"
        } else {
            $a = "`"$scr`" --aoi $k --interval $($fp.Interval) --flush-every $($fp.Flush) --log-file `"$log`" -v"
        }
        Start-Process powershell -ArgumentList @(
            "-NoExit", "-Command",
            "`$host.UI.RawUI.WindowTitle = 'ANGELS collector - $k ($($fp.Label))'; " +
            "Set-Location '$Repo'; " +
            "Write-Host 'ANGELS collector: $k - $($fp.Label)' -ForegroundColor Magenta; " +
            "& '$Python' $a")
        Write-Host "    $k".PadRight(16) -NoNewline -ForegroundColor Cyan
        Write-Host $fp.Label
        Start-Sleep -Milliseconds 700    # so the four locks are taken in order
    }

    Write-Host ""
    Write-Host "  Ctrl-C in a window stops that collector only." -ForegroundColor DarkGray
    Write-Host "  Closing a window kills it -- anything buffered is lost, and" -ForegroundColor DarkGray
    Write-Host "  a push feed has no backfill. Prefer Ctrl-C, which flushes." -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  To go back to unattended collection:" -ForegroundColor DarkGray
    Write-Host "    Ctrl-C each window, then .\collector.ps1 install -Aoi <box>" -ForegroundColor DarkGray
    Write-Host ""
}

switch ($Action) {
    "windows"     { Start-Windows }
    "install-all" { Install-All }
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
