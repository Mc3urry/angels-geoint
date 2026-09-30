"""Fail loudly when the suite is started by the wrong interpreter.

WHAT THIS CATCHES, AND WHAT IT CANNOT

On this machine `python` is ArcGIS Pro's 3.14t. The project lives in
C:\\Users\\mccul\\envs\\angels. Scripts handle that themselves -- see
scripts/_bootstrap.py, which re-runs them under the right interpreter -- but
pytest has no such hook, so a bare `python -m pytest` has two ways to go
wrong:

  pytest is absent      "No module named pytest", and nothing in this
                        repository gets a chance to run. Python prints that
                        before any conftest is imported. It CANNOT be fixed
                        from here, only documented -- see README.
  pytest is present     but `angels` is not importable. Collection then fails
                        module by module with a wall of ImportError, which
                        reads like the project is broken rather than like the
                        wrong python was used. That is what this file stops.

The second is the dangerous one: a suite that errors for an environmental
reason, in language that blames the code. Run it in a hurry and the
conclusion is "the tests are broken", which is not what happened.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest


def pytest_configure(config) -> None:
    try:
        import angels  # noqa: F401
        return
    except ModuleNotFoundError as exc:
        if exc.name != "angels":
            return          # a missing dependency is a different problem

    here = Path(__file__).parent
    candidates = [
        here / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
        / "python",
        Path.home() / "envs" / "angels" /
        ("python.exe" if sys.platform == "win32" else "bin/python"),
    ]
    found = next((c for c in candidates if c.exists()), None)
    runner = ".\\tasks.ps1 test" if sys.platform == "win32" else "make check"

    pytest.exit(
        "\n\n"
        "  This is the wrong interpreter, not a broken test suite.\n\n"
        f"    running under : {sys.executable}\n"
        f"    cannot import : angels\n"
        + (f"    try instead   : {found}\n" if found else "")
        + f"\n  Use  {runner}  -- it picks the interpreter that has the\n"
        "  project installed. See docs/environment.md.\n",
        returncode=2)


def pytest_report_header(config) -> str:
    """Say which python is running, on every invocation.

    Cheap, and it turns "why did that behave differently" into one glance.
    """
    return f"interpreter: {sys.executable}"
