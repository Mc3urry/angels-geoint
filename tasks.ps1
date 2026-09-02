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
    Write-Host "poll OpenSky into the archive"
    Write-Host "    .\tasks.ps1 status     " -NoNewline -ForegroundColor Cyan
    Write-Host "how much archive exists"
    Write-Host ""
    Write-Host "    .\tasks.ps1 clean      " -NoNewline -ForegroundColor Cyan
    Write-Host "remove caches"
    Write-Host ""
    Write-Host "  serve, web and ingest each need their own terminal." -ForegroundColor DarkGray
    Write-Host ""
}

switch ($Task.ToLower()) {

    "install"  { pip install -e . }
    "dev"      { pip install -e ".[dev]" }
    "analysis" { pip install -e ".[analysis]" }

    "test"     { pytest -q }
    "lint"     { ruff check angels tests scripts }

    "serve"    { uvicorn angels.api.main:app --reload }
    "web"      {
        Write-Host "front end -> http://localhost:5173" -ForegroundColor Magenta
        Write-Host "the API must also be running (.\tasks.ps1 serve)" -ForegroundColor DarkGray
        python -m http.server 5173 --directory web
    }

    "ingest"   { python scripts\ingest_aviation.py --interval 30 }

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
