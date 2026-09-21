# Python environment

ANGELS runs in its own conda environment. Nothing is installed into ArcGIS
Pro's Python, and nothing is installed into a conda `base`.

## Create it

```powershell
conda create -p C:\Users\%USERNAME%\envs\angels -c conda-forge -y `
    python=3.12 rasterio numpy duckdb pyarrow httpx fastapi uvicorn `
    python-dotenv pytest shapely

conda activate C:\Users\%USERNAME%\envs\angels
cd C:\Users\%USERNAME%\angels-geoint
pip install -e ".[dev]"
.\tasks.ps1 test
```

`-p` with an explicit path, not `-n`, deliberately: `-n` puts the environment
wherever the *currently active* conda keeps its envs, and on this machine that
is inside the ArcGIS Pro install directory. An explicit path outside it cannot
drift into Esri's tree no matter which conda is driving.

Activate it in every terminal before touching this project.

## Why: the 15 September 2026 incident

`conda install -c conda-forge rasterio` was run without checking which
environment was active. It was this one:

```
base  *  C:\Users\mccul\AppData\Local\Programs\ArcGIS\Pro\bin\Python
```

ArcGIS Pro's conda **root**. Until that moment it contained only Esri's
`muconda` and `pkg-metadata` and had no `python.exe`. The install gave it
Python 3.14 (free-threaded), NumPy 2.5, MKL, GDAL and PROJ.

### What was actually damaged: almost nothing

Pro does not run from the root. It runs from a separate environment:

```
arcgispro-py3  C:\Users\mccul\...\Pro\bin\Python\envs\arcgispro-py3
```

Different prefix, untouched by the transaction. `conda list --revisions`
showed a single revision with every package marked `+`, confirming nothing was
replaced or downgraded -- a near-empty root was populated. The `ClobberError`
lines were conda *refusing* to overwrite three Esri files, which is the safety
mechanism working.

### What was actually damaged: the meaning of `python`

That directory sits first on `PATH`. Before the install it had no
`python.exe`, so the bare command `python` fell through to the standalone
Python 3.12 where ANGELS was installed with `pip install -e .`. Afterwards it
had one, and `python` silently began resolving to Python 3.14 instead.

Nothing announced this. The symptom was:

```
ModuleNotFoundError: No module named 'angels'
```

which reads as a broken project and was in fact a different interpreter that
had never heard of the project.

### The lesson, which is the project's own

An interpreter changing underneath you is the environment-level form of the
failure this whole system exists to detect: **a change that produces a
confident wrong answer rather than an error.** The archive was fine, the code
was fine, the tests were fine -- and the command reported a missing module.

`tasks.ps1` now refuses to run under an interpreter that cannot import
`angels`, and prints which python it found and where. An absence of complaints
from a tool means nothing unless the tool was capable of complaining.

## Rules

1. **Never** `conda install` without checking `conda info --envs` first. The
   `*` marks the target.
2. **Never** install into ArcGIS Pro's Python, root or `arcgispro-py3`. Esri's
   own guidance is to clone that environment, not modify it.
3. On Windows, prefer conda-forge over pip for `rasterio`, `gdal` and
   `shapely`. The pip wheels bundle their own GDAL and conflict.
4. The scheduled collectors record an ABSOLUTE path to the python that
   registered them. After changing environments, run
   `.\collector.ps1 status`, and reinstall from the new environment if they
   have stopped.

## Cleaning up the root (optional)

The extra packages in the ArcGIS conda root are inert as long as the project
environment is activated -- Pro does not use that prefix. Removing them risks
disturbing Esri's `muconda`, and gains nothing but tidiness. Leave them.

The one live consequence is `PATH` shadowing, and the environment guard in
`tasks.ps1` covers that.
