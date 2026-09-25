"""Every optional extra has a guarded install path.

WHY THIS TEST EXISTS

`_env.ps1` resolves the right interpreter because the bare command `python`
on this machine is ArcGIS Pro's conda root -- a Python 3.14 free-threaded
build with wheels for almost nothing. `_bootstrap.py` does the same for
scripts. Both guards work.

Neither covers `pip`. On 2026-09-25, `pip install -e ".[ml]"` was run from a
bare prompt exactly as the documentation then suggested, went to the ArcGIS
interpreter, found no cp314t wheel for duckdb, tried to compile it and failed
on a missing `nmake`. Nothing was installed and the classifier stayed
blocked.

The fix is not a warning. It is that **no extra should ever need a hand-typed
pip command**: if `pyproject.toml` declares it, `tasks.ps1` runs it through
the resolved interpreter. This test holds the two in parity, so a new extra
cannot be added without the guarded path to install it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _extras() -> set[str]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"\[project\.optional-dependencies\](.*?)(\n\[|\Z)",
                      text, re.S)
    assert block, "pyproject.toml has no optional-dependencies section"
    return set(re.findall(r"^(\w[\w-]*)\s*=", block.group(1), re.M))


def _targets() -> set[str]:
    """Extras that `tasks.ps1` can install, by the target that installs them."""
    text = (ROOT / "tasks.ps1").read_text(encoding="utf-8")
    return {extra for _name, extra in _pairs(text)}


def _pairs(text: str) -> list[tuple[str, str]]:
    return re.findall(
        r'"([\w-]+)"\s*\{\s*&\s*\$Py\s+-m\s+pip\s+install\s+-e\s+'
        r'"\.\[([\w-]+)\]"', text)


def test_every_extra_has_a_tasks_target() -> None:
    missing = _extras() - _targets()
    assert not missing, (
        "these extras are declared in pyproject.toml but have no "
        f"`.\\tasks.ps1 <name>` target: {sorted(missing)}. Add one, so nobody "
        "has to type a bare `pip install` -- which on this machine reaches "
        "ArcGIS Pro's Python 3.14t and fails to build duckdb."
    )


def test_each_target_installs_the_extra_it_is_named_for() -> None:
    text = (ROOT / "tasks.ps1").read_text(encoding="utf-8")
    pairs = _pairs(text)
    wrong = [(name, extra) for name, extra in pairs if name != extra]
    assert not wrong, f"target name and extra disagree: {wrong}"


# --- collector.ps1 footprints ------------------------------------------------
#
# Same failure mode as the pyproject/tasks parity above, one file over: a list
# of collectors that has to be edited in several places when a collector is
# added. On 2026-09-25 the fifth one was added and four separate copies of
# @("air","conus","sea","sea-conus") had to be found. Missing the one inside
# `Find-CollectorProcesses` would have left the new collector invisible to
# `status` and killable by `install-all` without ever being listed.

def _collector() -> str:
    return (ROOT / "collector.ps1").read_text(encoding="utf-8")


def _footprint_keys(text: str) -> set[str]:
    block = re.search(r"\$Footprints\s*=\s*@\{(.*?)\n\}", text, re.S)
    assert block, "collector.ps1 has no $Footprints table"
    return set(re.findall(r"^\s*'?([\w-]+)'?\s*=\s*@\{", block.group(1), re.M))


def _order(text: str) -> list[str]:
    m = re.search(r"^\$Order\s*=\s*@\(([^)]*)\)", text, re.M)
    assert m, "collector.ps1 has no script-scope $Order list"
    return re.findall(r'"([\w-]+)"', m.group(1))


def test_every_footprint_is_in_the_order_list() -> None:
    text = _collector()
    missing = _footprint_keys(text) - set(_order(text))
    assert not missing, (
        f"footprints absent from $Order: {sorted(missing)}. status, "
        f"install-all, windows and process detection all walk $Order, so a "
        f"footprint missing from it is a collector nobody can see."
    )


def test_the_order_list_names_only_real_footprints() -> None:
    text = _collector()
    unknown = set(_order(text)) - _footprint_keys(text)
    assert not unknown, f"$Order names footprints that do not exist: {sorted(unknown)}"


def test_aoi_validateset_matches_the_footprints() -> None:
    text = _collector()
    m = re.search(r"\[ValidateSet\(([^)]*)\)\]\s*\n\s*\[string\]\$Aoi", text)
    assert m, "could not find the -Aoi ValidateSet"
    allowed = set(re.findall(r'"([\w-]+)"', m.group(1)))
    assert allowed == _footprint_keys(text), (
        f"-Aoi accepts {sorted(allowed)} but the footprints are "
        f"{sorted(_footprint_keys(text))}"
    )


def test_process_detection_knows_every_ingest_script() -> None:
    """A collector the process scan cannot match is one `status` cannot see."""
    text = _collector()
    block = re.search(r"\$Footprints\s*=\s*@\{(.*?)\n\}", text, re.S).group(1)
    scripts = set(re.findall(r'Script\s*=\s*"scripts\\(ingest_\w+)\.py"', block))
    scripts.add("ingest_aviation")            # the default for footprints with no Script
    m = re.search(r"CommandLine -match 'ingest_\(([^)]*)\)", text)
    assert m, "could not find the ingest script regex in Find-CollectorProcesses"
    known = {f"ingest_{alt}" for alt in m.group(1).split("|")}
    assert scripts <= known, (
        f"these ingest scripts are configured but unmatched by the process "
        f"scan: {sorted(scripts - known)}"
    )
