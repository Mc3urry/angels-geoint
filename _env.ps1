<#
    Find the interpreter this project is installed into.

    WHY THIS EXISTS.

    On 15 September 2026 a conda install put a Python 3.14 into ArcGIS Pro's
    conda root, which sits first on PATH. The bare command `python` silently
    changed meaning, and `python scripts/inspect_scene.py` began reporting
    "No module named 'angels'" -- which reads as a broken project and was
    actually a different interpreter that had never heard of it.

    The first fix was a guard that refused to run and told you to activate.
    That is correct but still puts a step between you and the work, and a
    step you must remember is a step you will forget -- as happened twice
    within the hour, once when `pip install -e .` ran unactivated and spent
    ten minutes trying to compile duckdb from source against 3.14.

    So the scripts now FIND the right interpreter rather than requiring you
    to have selected it. Order of preference:

        1. $env:ANGELS_PYTHON        an explicit override
        2. .\.venv, then the conventional conda env path
        3. whatever `python` resolves to on PATH

    Each candidate must prove it can `import angels` before it is accepted.
    Nothing is assumed -- which is the same rule the detectors run on.
#>

function Get-AngelsPython {
    $candidates = @()

    if ($env:ANGELS_PYTHON) { $candidates += $env:ANGELS_PYTHON }
    $candidates += (Join-Path $PSScriptRoot ".venv\Scripts\python.exe")
    $candidates += "$env:USERPROFILE\envs\angels\python.exe"
    $candidates += "$env:USERPROFILE\miniforge3\envs\angels\python.exe"
    $candidates += "$env:USERPROFILE\miniconda3\envs\angels\python.exe"

    $onPath = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($onPath) { $candidates += $onPath }

    foreach ($c in $candidates) {
        if (-not $c -or -not (Test-Path $c)) { continue }
        & $c -c "import angels" 2>$null
        if ($LASTEXITCODE -eq 0) { return $c }
    }
    return $null
}

function Assert-AngelsPython {
    $py = Get-AngelsPython
    if ($py) { return $py }

    $onPath = (Get-Command python -ErrorAction SilentlyContinue).Source
    Write-Host ""
    Write-Host "  NO INTERPRETER HAS THIS PROJECT INSTALLED." -ForegroundColor Red
    Write-Host ""
    if ($onPath) {
        $ver = & $onPath -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null
        Write-Host "  python on PATH is:" -ForegroundColor Yellow
        Write-Host "    $onPath  ($ver)"
        Write-Host "  and it cannot import 'angels'."
        Write-Host ""
    }
    Write-Host "  Looked in:" -ForegroundColor DarkGray
    Write-Host "    `$env:ANGELS_PYTHON, .\.venv, ~\envs\angels," -ForegroundColor DarkGray
    Write-Host "    ~\miniforge3\envs\angels, ~\miniconda3\envs\angels, PATH" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  Create the environment -- see docs/environment.md:" -ForegroundColor Cyan
    Write-Host "    conda create -p `$env:USERPROFILE\envs\angels -c conda-forge -y ``"
    Write-Host "        python=3.12 rasterio numpy duckdb pyarrow httpx fastapi ``"
    Write-Host "        uvicorn python-dotenv pytest shapely"
    Write-Host "    `$env:USERPROFILE\envs\angels\python.exe -m pip install -e `".[dev]`""
    Write-Host ""
    Write-Host "  Or point at an existing one:" -ForegroundColor Cyan
    Write-Host "    `$env:ANGELS_PYTHON = 'C:\path\to\python.exe'"
    Write-Host ""
    exit 1
}
