"""Tests for the script bootstrap.

It decides which interpreter every script in scripts/ actually runs under, so
a bug here is a bug in all of them -- and the symptom would be the one this
project keeps relearning: a confident wrong answer instead of an error.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from scripts import _bootstrap


# -- candidate selection ---------------------------------------------------

def test_an_explicit_override_wins(monkeypatch) -> None:
    monkeypatch.setenv("ANGELS_PYTHON", "/some/where/python")
    assert str(_bootstrap.candidates()[0]).replace("\\", "/") == "/some/where/python"


def test_path_is_not_consulted(monkeypatch, tmp_path) -> None:
    """PATH is where we already are, and it is what failed. Offering it back
    would mean re-running the same broken thing and calling it a fix.

    The first version of this test asserted that no candidate equals
    sys.executable -- and failed on a correctly configured machine, because
    when you ARE in ~/envs/angels, the right answer is legitimately also the
    one you are running. The test was asserting "the list excludes the
    current interpreter" when the property worth having is "the list is not
    derived from PATH". Those are different claims, and only the second one
    is true or desirable.
    """
    monkeypatch.delenv("ANGELS_PYTHON", raising=False)
    fake = tmp_path / ("python.exe" if os.name == "nt" else "python")
    fake.write_text("")
    monkeypatch.setenv("PATH", str(tmp_path))

    assert not any(_bootstrap._same(c, fake)
                   for c in _bootstrap.candidates()), \
        "a python found only on PATH was offered as a candidate"


def test_the_running_interpreter_may_be_a_candidate(monkeypatch) -> None:
    """The complement, stated so nobody 'fixes' it back.

    Working inside ~/envs/angels means the conventional location and the
    running interpreter are the same file. That is success, not a bug --
    _same() and the SENTINEL handle the self-match, and candidates() has no
    business excluding a correct answer for being the current one.
    """
    monkeypatch.delenv("ANGELS_PYTHON", raising=False)
    assert _bootstrap.candidates()          # simply non-empty and unfiltered


def test_the_repo_venv_is_looked_for() -> None:
    joined = " ".join(str(c) for c in _bootstrap.candidates())
    assert ".venv" in joined


def test_conda_locations_are_looked_for() -> None:
    joined = " ".join(str(c).replace("\\", "/") for c in _bootstrap.candidates())
    for env in ("envs/angels", "miniforge3/envs/angels",
                "miniconda3/envs/angels"):
        assert env in joined


# -- the symlink trap ------------------------------------------------------

def test_a_venv_python_is_not_mistaken_for_its_base(tmp_path) -> None:
    """THE REGRESSION TEST.

    _same() originally compared RESOLVED paths. A venv's python is a symlink
    to the base interpreter, so resolving made two genuinely different
    ENVIRONMENTS -- different site-packages, one of which has the project --
    compare equal. The candidate was then skipped as "already running it" and
    the bootstrap reported that no interpreter could be found, while sitting
    next to the one that worked.
    """
    base = tmp_path / "base_python"
    base.write_text("#!/bin/sh\n")
    venv = tmp_path / "venv_python"
    try:
        venv.symlink_to(base)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")

    assert not _bootstrap._same(venv, base), \
        "a venv python and its base binary are different environments"


def test_the_same_path_is_recognised(tmp_path) -> None:
    p = tmp_path / "python"
    assert _bootstrap._same(p, p)
    assert _bootstrap._same(p, Path(str(p)))


def test_comparison_is_case_insensitive_where_the_os_is(tmp_path) -> None:
    """Windows paths differing only in case are the same file; on POSIX they
    are not. normcase encodes exactly that difference."""
    a, b = Path("C:/Envs/Angels/python.exe"), Path("c:/envs/angels/PYTHON.EXE")
    assert _bootstrap._same(a, b) is (os.name == "nt")


# -- loop safety -----------------------------------------------------------

def test_the_sentinel_stops_a_second_re_run(monkeypatch) -> None:
    """Without this a misconfigured ANGELS_PYTHON would spawn subprocesses
    forever, each one certain the next will work."""
    monkeypatch.setenv(_bootstrap.SENTINEL, "1")
    monkeypatch.setitem(sys.modules, "angels", None)
    monkeypatch.delitem(sys.modules, "angels")

    called = []
    monkeypatch.setattr(_bootstrap.subprocess, "run",
                        lambda *a, **k: called.append(a) or pytest.fail(
                            "re-ran despite the sentinel"))

    # ensure() returns quietly when angels imports; force the failure path.
    import builtins
    real_import = builtins.__import__

    def fake(name, *a, **k):
        if name == "angels":
            raise ModuleNotFoundError("No module named 'angels'", name="angels")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(ModuleNotFoundError):
        _bootstrap.ensure()
    assert not called


def test_a_different_missing_module_is_not_swallowed(monkeypatch) -> None:
    """If rasterio is missing, that is not a wrong-interpreter problem and
    must not be reported as one."""
    import builtins
    real_import = builtins.__import__

    def fake(name, *a, **k):
        if name == "angels":
            raise ModuleNotFoundError("No module named 'rasterio'",
                                      name="rasterio")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(ModuleNotFoundError) as e:
        _bootstrap.ensure()
    assert e.value.name == "rasterio"
