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


# -- every script switches interpreter BEFORE touching a third-party library --

def _third_party_imports_before_bootstrap(path: Path) -> list[str]:
    import ast
    std = set(sys.stdlib_module_names) | {"__future__"}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    early: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Try) and any(
                isinstance(n, ast.Import) and any(a.name == "_bootstrap" for a in n.names)
                for n in node.body):
            return early
        if isinstance(node, ast.Import):
            early += [a.name for a in node.names if a.name.split(".")[0] not in std]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] not in std:
                early.append(node.module)
    return []          # no bootstrap at all: a stub, checked elsewhere if ever needed


SCRIPTS = sorted(p for p in (Path(__file__).resolve().parents[1] / "scripts").glob("*.py")
                 if p.name != "_bootstrap.py")


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_third_party_import_runs_before_the_bootstrap(script: Path) -> None:
    """THE BUG THIS CAUGHT. `python scripts\\clip_ais.py` died with
    "No module named 'duckdb'" on 2026-09-22 because `import duckdb` sat above
    the bootstrap block. The bootstrap re-executes a script under the project
    interpreter -- but only once it is reached, and an import above it runs
    first, under whatever Python the user typed. Five scripts had the same
    ordering; the collector never showed it only because Task Scheduler
    launches it with the right interpreter directly."""
    early = _third_party_imports_before_bootstrap(script)
    assert not early, (f"{script.name} imports {early} before the bootstrap; "
                       f"move them below the `import _bootstrap` block")


def _has_bootstrap_block(path: Path) -> bool:
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, ast.Try) and any(
            isinstance(n, ast.Import)
            and any(a.name == "_bootstrap" for a in n.names)
            for n in node.body)
        for node in tree.body
    )


def _imports_project_code(path: Path) -> bool:
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(
                ".")[0] in ("angels", "scripts"):
            return True
        if isinstance(node, ast.Import) and any(
                a.name.split(".")[0] in ("angels", "scripts")
                for a in node.names):
            return True
    return False


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_a_script_that_imports_the_package_has_a_bootstrap(script: Path) -> None:
    """THE HOLE THIS CLOSES.

    `_third_party_imports_before_bootstrap` returns an empty list for a script
    with no bootstrap block at all, on the reasoning that such a file is a
    stub. The test above therefore PASSES such a script vacuously, and the
    comment says "checked elsewhere if ever needed". It was not checked
    elsewhere.

    `classify_candidates.py` shipped on 2026-09-25 with `sys.path.insert` in
    place of the bootstrap. Both guards were green: it imports `angels`
    successfully under a bare interpreter because the path hack puts the repo
    root on sys.path -- and then fails on the first real dependency, under
    whatever Python the user typed, which is exactly the failure mode the
    bootstrap exists to prevent. A path hack silences the symptom the
    bootstrap is watching for.

    So: a script that reaches into the package must switch interpreter first.
    A script that does not touch the package needs nothing and is exempt.
    """
    if not _imports_project_code(script):
        return
    assert _has_bootstrap_block(script), (
        f"{script.name} imports project code but has no `try: import "
        f"_bootstrap` block, so running it under a bare interpreter will fail "
        f"on the first dependency instead of re-executing under the project "
        f"environment. Do not substitute sys.path.insert -- that hides the "
        f"very failure the bootstrap detects."
    )


def test_the_bootstrap_puts_the_repo_root_on_sys_path() -> None:
    """THE BUG THIS CLOSES.

    Four scripts reuse each other -- `from scripts.boundary_analysis import
    limit_sets` and the like. That import needs the REPO ROOT on `sys.path`,
    and `python scripts/x.py` puts `scripts/` there instead; an editable
    install maps `angels` alone. So every one of them worked under
    `PYTHONPATH=.` and died when run the ordinary way.

    `test_scripts_import` above could not catch it: pytest.ini sets
    `pythonpath = ["."]`, so the root is already there when that test runs.
    The failure only appears in a child process started the way a person
    starts one, which is what this does.

    It surfaced on 2026-09-25 when `reproduce.py` -- whose entire purpose is
    to be run by somebody else -- spawned three of them and all three died
    with ModuleNotFoundError: No module named 'scripts'.
    """
    import os
    import subprocess

    root = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    # Stand in for the editable install: `angels` importable, root not on path.
    env["PYTHONPATH"] = str(root)
    probe = ("import sys, _bootstrap, pathlib;"
             "import scripts.boundary_analysis;"
             "print('ok')")
    r = subprocess.run([sys.executable, "-c", probe], cwd=root / "scripts",
                       capture_output=True, text=True, env=env)
    assert "ok" in r.stdout, (
        "a script in scripts/ cannot import scripts.* -- _bootstrap must put "
        f"the repo root on sys.path:\n{r.stderr[-500:]}")


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_a_script_reusing_another_declares_the_bootstrap_first(script: Path) -> None:
    """A `scripts.*` import must sit below the bootstrap, not above it.

    Same shape as the third-party rule: the bootstrap is what makes the
    import resolve, so anything depending on it that runs earlier runs
    before the thing it depends on exists.
    """
    import ast
    tree = ast.parse(script.read_text(encoding="utf-8"))
    seen_bootstrap = False
    for node in tree.body:
        if isinstance(node, ast.Try) and any(
                isinstance(n, ast.Import)
                and any(a.name == "_bootstrap" for a in n.names)
                for n in node.body):
            seen_bootstrap = True
            continue
        mod = (node.module if isinstance(node, ast.ImportFrom) else None) or ""
        names = ([a.name for a in node.names]
                 if isinstance(node, ast.Import) else [])
        if (mod.startswith("scripts")
                or any(n.startswith("scripts") for n in names)):
            assert seen_bootstrap, (
                f"{script.name} imports {mod or names} at module level before "
                f"the bootstrap block that makes it resolvable")
