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
    [string]$Task = "help",

    # `run` only: which script, and everything to hand it.
    [Parameter(Position = 1)]
    [string]$Script,

    # Collects the rest verbatim, including tokens beginning with a dash, so
    # `--aoi conus --at=-117.0,40.0` reaches Python unchanged. Not named
    # $Args: that is an automatic variable and shadowing it here would bite
    # somewhere else in this file.
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
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
    Write-Host "    .\tasks.ps1 ml         " -NoNewline -ForegroundColor Cyan
    Write-Host "scikit-learn, for the candidate classifier"
    Write-Host "    .\tasks.ps1 sar        " -NoNewline -ForegroundColor Cyan
    Write-Host "rasterio"
    Write-Host "    .\tasks.ps1 forensics  " -NoNewline -ForegroundColor Cyan
    Write-Host "pvlib, pillow"
    Write-Host ""
    Write-Host "    Install through these, never a bare pip: on this machine" -ForegroundColor DarkGray
    Write-Host "    `python` is ArcGIS Pro's 3.14t, which has no wheels." -ForegroundColor DarkGray
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
    Write-Host "    .\tasks.ps1 run <script> [args...]" -ForegroundColor Cyan
    Write-Host "        any script in scripts\, with the right interpreter." -ForegroundColor DarkGray
    Write-Host "        .\tasks.ps1 run adsb_coverage --aoi conus" -ForegroundColor DarkGray
    Write-Host "        .\tasks.ps1 run             lists them" -ForegroundColor DarkGray
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

    # Every extra declared in pyproject.toml gets a target here, and the
    # reason is the whole reason this file exists. On 2026-09-25 `pip install
    # -e ".[ml]"` was run from a bare prompt, went to ArcGIS Pro's Python
    # 3.14t because that is what `python` means on PATH, found no cp314t
    # wheel for duckdb, tried to build it from source and died on a missing
    # nmake. The interpreter guard protects scripts; it cannot protect a bare
    # `pip`. So there is no extra a person has to install by hand.
    # tests/test_tasks_targets.py holds this to parity.
    "install"    { & $Py -m pip install -e . }
    "dev"        { & $Py -m pip install -e ".[dev]" }
    "analysis"   { & $Py -m pip install -e ".[analysis]" }
    "ml"         { & $Py -m pip install -e ".[ml]" }
    "sar"        { & $Py -m pip install -e ".[sar]" }
    "forensics"  { & $Py -m pip install -e ".[forensics]" }

    "test"     { & $Py -m pytest -q }
    "doctor"   { & $Py scripts\doctor.py }
    "lint"     { & $Py -m ruff check angels tests scripts }

    "serve"    { & $Py -m uvicorn angels.api.main:app --reload }
    "web"      {
        Write-Host "front end -> http://127.0.0.1:5173" -ForegroundColor Magenta
        Write-Host "the API must also be running (.\tasks.ps1 serve)" -ForegroundColor DarkGray
        & $Py -m http.server 5173 --directory web
    }

    # Foreground poll, for watching it work. The unattended one belongs in
    # .\collector.ps1, which survives a closed terminal and a reboot.
    "ingest"       { & $Py scripts\ingest_aviation.py --aoi air }
    "ingest-conus" { & $Py scripts\ingest_aviation.py --aoi conus }

    # WHY THIS EXISTS. Every script in scripts\ needs the project's
    # interpreter, and typing `python scripts\x.py` gets ArcGIS Pro's 3.14,
    # which has never heard of this project. _env.ps1 already knows how to
    # find the right one; this just puts it one word away for EVERY script
    # rather than only the handful with their own task above.
    "run"      {
        $dir = Join-Path $PSScriptRoot "scripts"
        if (-not $Script) {
            Write-Host ""
            Write-Host "  scripts in $dir" -ForegroundColor Magenta
            Get-ChildItem -Path $dir -Filter *.py |
                Where-Object { $_.Name -ne "_bootstrap.py" } |
                Sort-Object Name |
                ForEach-Object { Write-Host "    $($_.BaseName)" }
            Write-Host ""
            Write-Host "  .\tasks.ps1 run <name> [args...]" -ForegroundColor DarkGray
            Write-Host ""
            break
        }

        # Accept adsb_coverage, adsb_coverage.py, or scripts\adsb_coverage.py
        # -- all three are things you will type, and none of them is wrong.
        $name = [IO.Path]::GetFileNameWithoutExtension($Script)
        $path = Join-Path $dir "$name.py"
        if (-not (Test-Path $path)) {
            Write-Host ""
            Write-Host "  No scripts\$name.py" -ForegroundColor Red
            $near = Get-ChildItem -Path $dir -Filter *.py |
                    Where-Object { $_.BaseName -like "*$name*" }
            if ($near) {
                Write-Host "  did you mean:" -ForegroundColor Yellow
                $near | ForEach-Object { Write-Host "    $($_.BaseName)" }
            } else {
                Write-Host "  .\tasks.ps1 run    lists them all" -ForegroundColor DarkGray
            }
            Write-Host ""
            exit 1
        }

        if ($Rest) { & $Py $path @Rest } else { & $Py $path }
    }

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
