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
