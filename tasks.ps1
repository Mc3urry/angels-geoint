<#
.SYNOPSIS
    Windows equivalent of the Makefile.

.DESCRIPTION
    make is not present on Windows by default. This mirrors the Makefile
    targets so the commands are the same on either platform. The Makefile
    stays for anyone who clones this on macOS or Linux -- keep the two in
    step when you add a target.

.EXAMPLE
    .\tasks.ps1 dev
    .\tasks.ps1 test
    .\tasks.ps1 serve
#>

param(
    [Parameter(Position = 0)]
    [string]$Task = "help"
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "_env.ps1")

function Show-Help {
    Write-Host ""
    Write-Host "  ANGELS tasks" -ForegroundColor Magenta
    Write-Host ""
    Write-Host "    .\tasks.ps1 install    " -NoNewline -ForegroundColor Cyan
    Write-Host "editable install"
    Write-Host "    .\tasks.ps1 dev        " -NoNewline -ForegroundColor Cyan
    Write-Host "editable install + test deps"
    Write-Host "    .\tasks.ps1 analysis   " -NoNewline -ForegroundColor Cyan
    Write-Host "phase 4 geospatial stack (heavy)"
    Write-Host ""
    Write-Host "    .\tasks.ps1 test       " -NoNewline -ForegroundColor Cyan
    Write-Host "run the suite"
    Write-Host "    .\tasks.ps1 lint       " -NoNewline -ForegroundColor Cyan
    Write-Host "ruff"
    Write-Host ""
    Write-Host "    .\tasks.ps1 serve      " -NoNewline -ForegroundColor Cyan
    Write-Host "API on :8000"
    Write-Host "    .\tasks.ps1 web        " -NoNewline -ForegroundColor Cyan
    Write-Host "front end on :5173"
    Write-Host "    .\tasks.ps1 ingest     " -NoNewline -ForegroundColor Cyan
    Write-Host "poll the DC box (30 s) into the archive"
    Write-Host "    .\tasks.ps1 ingest-conus " -NoNewline -ForegroundColor Cyan
    Write-Host "poll the whole country (10 min)"
    Write-Host "    .\tasks.ps1 status     " -NoNewline -ForegroundColor Cyan
    Write-Host "how much archive exists"
    Write-Host "    .\tasks.ps1 doctor     " -NoNewline -ForegroundColor Cyan
    Write-Host "is everything running? (after a reboot)"
    Write-Host ""
    Write-Host "    .\tasks.ps1 clean      " -NoNewline -ForegroundColor Cyan
    Write-Host "remove caches"
    Write-Host ""
    Write-Host "  serve, web and ingest each need their own terminal." -ForegroundColor DarkGray
    Write-Host ""
}

# Resolve the project interpreter ONCE, and use it explicitly everywhere
# below. Bare `python` is never invoked: on this machine it may be ArcGIS
# Pro's 3.14, which has never heard of this project.
$Py = $null
if ($Task.ToLower() -notin @("help", "clean")) {
    if ($Task.ToLower() -in @("install", "dev", "analysis")) {
        # Installing is how the project GETS into an interpreter, so it
        # cannot require one that already has it.
        $Py = Get-AngelsPython
        if (-not $Py) { $Py = "$env:USERPROFILE\envs\angels\python.exe" }
        if (-not (Test-Path $Py)) { $Py = (Get-Command python).Source }
    } else {
        $Py = Assert-AngelsPython
    }
    Write-Host "  python: $Py" -ForegroundColor DarkGray
}

switch ($Task.ToLower()) {

    "install"  { & $Py -m pip install -e . }
    "dev"      { & $Py -m pip install -e ".[dev]" }
    "analysis" { & $Py -m pip install -e ".[analysis]" }

    "test"     { & $Py -m pytest -q }
    "doctor"   { & $Py scripts\doctor.py }
    "lint"     { & $Py -m ruff check angels tests scripts }

    "serve"    { & $Py -m uvicorn angels.api.main:app --reload }
    "web"      {
        Write-Host "front end -> http://localhost:5173" -ForegroundColor Magenta
        Write-Host "the API must also be running (.\tasks.ps1 serve)" -ForegroundColor DarkGray
        & $Py -m http.server 5173 --directory web
    }

    # Foreground poll, for watching it work. The unattended one belongs in
    # .\collector.ps1, which survives a closed terminal and a reboot.
    "ingest"       { & $Py scripts\ingest_aviation.py --aoi air }
    "ingest-conus" { & $Py scripts\ingest_aviation.py --aoi conus }

    "status"   {
        # How much archive do we actually have? The single most useful thing
        # to check before wondering why the map is empty.
        $raw = "data\raw\aviation"
        if (-not (Test-Path $raw)) {
            Write-Host "no archive yet -- run .\tasks.ps1 ingest" -ForegroundColor Yellow
            break
        }
        $files = Get-ChildItem -Path $raw -Recurse -Filter *.parquet
        $hours = Get-ChildItem -Path $raw -Directory
        $mb = [math]::Round(($files | Measure-Object Length -Sum).Sum / 1MB, 1)
        Write-Host ""
        Write-Host "  archive: $($files.Count) file(s) across $($hours.Count) hour(s), $mb MB"
        if ($hours.Count) {
            $sorted = $hours | Sort-Object Name
            Write-Host "  from $($sorted[0].Name) to $($sorted[-1].Name)"
        }
        Write-Host ""
    }

    "clean"    {
        Get-ChildItem -Recurse -Directory -Filter __pycache__ |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force .pytest_cache, .ruff_cache -ErrorAction SilentlyContinue
        Write-Host "cleaned"
    }

    default    { Show-Help }
}
