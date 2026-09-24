<#
.SYNOPSIS
    Start the ANGELS viewer. One command, browser opens.

.DESCRIPTION
    ONE process. uvicorn serves both the API and the front end, so there is
    no second server, no second port, and no CORS.

    This is the part you start when you want to LOOK at something. It is
    completely independent of the collector, which runs permanently in the
    background under Task Scheduler -- see collector.ps1. Closing this window
    does not stop collection, and never should: the archive cannot be
    backfilled, but the viewer can be reopened any time.

.EXAMPLE
    .\app.ps1              # start both, open the browser
    .\app.ps1 -NoBrowser
    .\app.ps1 stop         # close the two windows this opened
#>

param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "status")]
    [string]$Action = "start",

    [switch]$NoBrowser,
    [int]$ApiPort = 8000,
    [int]$WebPort = 5173      # legacy, only used by `stop`
)

$ErrorActionPreference = "Stop"
$Repo = $PSScriptRoot

function Test-Port([int]$Port) {
    try {
        $c = New-Object Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", $Port); $c.Close(); return $true
    } catch { return $false }
}

function Start-App {
    Write-Host ""

    if (Test-Port $ApiPort) {
        Write-Host "  API already running on $ApiPort" -ForegroundColor DarkGray
    } else {
        Start-Process powershell -ArgumentList @(
            "-NoExit", "-Command",
            "Set-Location '$Repo'; Write-Host 'ANGELS API' -ForegroundColor Magenta; " +
            "uvicorn angels.api.main:app --reload --port $ApiPort"
        ) -WindowStyle Minimized
        Write-Host "  Serving  -> http://127.0.0.1:$ApiPort" -ForegroundColor Green
    }

    # Wait for the API to actually answer rather than sleeping a fixed amount
    # and hoping. uvicorn takes a variable second or two to bind, and opening
    # the browser early shows an error page for no reason.
    Write-Host "  Waiting for the API..." -NoNewline -ForegroundColor DarkGray
    $ready = $false
    foreach ($i in 1..30) {
        Start-Sleep -Milliseconds 400
        if (Test-Port $ApiPort) { $ready = $true; break }
        Write-Host "." -NoNewline -ForegroundColor DarkGray
    }
    Write-Host ""

    if (-not $ready) {
        Write-Host "  API did not come up. Check the minimised API window." -ForegroundColor Yellow
        return
    }

    # THE LITERAL ADDRESS, NOT "localhost". Two reasons, both real:
    #
    #   1. uvicorn binds 127.0.0.1 by default, and on Windows "localhost" can
    #      resolve to ::1 first -- which is a different address, nothing is
    #      listening on it, and the browser says "connection refused" about a
    #      server that is plainly running.
    #   2. A browser keys its HTTP cache by the hostname STRING, so
    #      localhost:8000 and 127.0.0.1:8000 are separate cache keyspaces.
    #      On 2026-09-23 the localhost one was holding an ES module from
    #      before the no-cache header existed, and the page died on a
    #      SyntaxError about a missing export -- a cache fault wearing a code
    #      fault's clothes. Pinning the launcher to one spelling means the
    #      viewer always opens where its cache is governed by that header.
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$ApiPort" }

    Write-Host ""
    Write-Host "  One window, minimised. Closing it stops the viewer." -ForegroundColor DarkGray
    Write-Host "  The collector is separate and keeps running regardless." -ForegroundColor DarkGray
    Write-Host ""
}

function Stop-App {
    # Only the processes holding these two ports -- deliberately narrow, so
    # this can never take down the collector or an unrelated python.
    foreach ($p in @($ApiPort, $WebPort)) {   # WebPort too, for old setups
        $conns = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
        foreach ($c in $conns) {
            Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
            Write-Host "  Stopped whatever was on port $p" -ForegroundColor Yellow
        }
    }
}

function Show-Status {
    Write-Host ""
    foreach ($x in @(@{n="ANGELS"; p=$ApiPort})) {
        $up = Test-Port $x.p
        Write-Host "  $($x.n.PadRight(8)) " -NoNewline
        Write-Host $(if ($up) { "running" } else { "stopped" }) `
                   -ForegroundColor $(if ($up) { "Green" } else { "Yellow" }) `
                   -NoNewline
        Write-Host "  :$($x.p)"
    }
    Write-Host ""
    Write-Host "  Collector status: .\collector.ps1 status" -ForegroundColor DarkGray
    Write-Host ""
}

switch ($Action) {
    "start"  { Start-App }
    "stop"   { Stop-App }
    "status" { Show-Status }
}
