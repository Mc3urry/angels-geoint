"""Every third-party import this project makes is declared in pyproject.toml.

WHY THIS TEST EXISTS, AND WHY `test_tasks_targets.py` COULD NOT DO IT

`tests/test_tasks_targets.py` holds the extras in `pyproject.toml` and the
targets in `tasks.ps1` in parity, so no extra can be added without a guarded
install path. It is a good test and it passed every run for three days while
`gtfs-realtime-bindings` -- installed 2026-10-03, load-bearing for four
published measurements -- appeared in **neither** file.

Parity was exact, because a thing missing from both sides of a comparison is
missing from the comparison too. That is mechanism Y: a consistency check
between two descriptions mistaken for a completeness check against the thing
described. The two files were written by one hand in one sitting, so
absent-from-both is the correlated failure that authoring makes likely, and no
amount of running the parity test would ever have shown it.

So this test compares `pyproject.toml` against something nobody wrote down by
hand: **the import statements in the source.** `ast` reads them, and
`sys.stdlib_module_names` separates standard library from third party.

LAZY IMPORTS ARE NOT EXEMPT, AND THAT IS THE WHOLE POINT

`scripts/probe_transit_parse.py` imports `google.transit` inside a function
and prints `gtfs-realtime-bindings is not installed` rather than raising. That
kindness is why a clean clone still collected and passed the suite for three
days, and therefore why nobody found the omission. **The defect survived
because the code around it was careful.**

A dependency reached for inside a function is still a dependency. A dependency
with a helpful message when it is absent is still a dependency. If something
is genuinely optional it belongs in an extra -- which is still a declaration.
There are no exemptions here, deliberately, because every exemption is a place
this test would have gone quiet exactly where it was needed.

WHAT IT FOUND ON FIRST RUN, 2026-10-06

Two more instances of the same shape, neither of them the roads extra:

  pydantic     angels/api/routes/labels.py imports BaseModel at module scope
               and arrives only because fastapi requires it.
  websockets   angels/adapters/maritime/aisstream.py calls websockets.connect
               in the live maritime collector and arrives only because
               `uvicorn[standard]` happens to pull it in.

Both worked. Neither was declared. `uvicorn[standard]`'s contents are not a
contract -- that extra's membership has changed before -- so the maritime
collector was one upstream packaging decision away from failing at connect
time, inside a long-running process, for a reason no traceback would explain.
"""

from __future__ import annotations

import ast
import pathlib
import sys
import tomllib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Where the project's own code lives. Anything importable from here is first
# party and declares nothing.
PACKAGES = ("angels",)
SOURCE_GLOBS = ("scripts/*.py", "angels/**/*.py", "tests/*.py")

# An import name and its distribution name are often not the same string, and
# there is no programmatic way to relate them without querying the installed
# environment -- which would make this test depend on what happens to be
# installed, i.e. on the very thing it is checking. So the mapping is explicit
# and adding to it is a deliberate act.
#
# Keys are normalised import names; values are normalised distribution names.
IMPORT_TO_DISTRIBUTION = {
    "google": "gtfs-realtime-bindings",   # google.transit.gtfs_realtime_pb2
    "dotenv": "python-dotenv",
    "sklearn": "scikit-learn",
    "pil": "pillow",
    "cv2": "opencv-python",
    "yaml": "pyyaml",
}


def normalise(name: str) -> str:
    """PEP 503 name normalisation, so pyarrow and PyArrow are one thing."""
    return "".join("-" if c in "-_." else c for c in name).lower()


# --------------------------------------------------------------------------
# what the project declares
# --------------------------------------------------------------------------

def declared() -> set[str]:
    """Every distribution named in `[project]`, core and extras alike.

    Read with `tomllib` rather than a regular expression. The recon pass that
    preceded this test used a regex and swept in `["E", "F", "I", "UP", "B"]`
    from ruff's `select`, which made five single letters look like declared
    dependencies -- a parser that finds things in the wrong table is how a
    completeness check acquires false negatives.
    """
    pj = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pj["project"]

    specs: list[str] = list(project.get("dependencies", []))
    for extra_specs in project.get("optional-dependencies", {}).values():
        specs.extend(extra_specs)

    out = set()
    for spec in specs:
        # "uvicorn[standard]>=0.29" -> uvicorn ; "esda>=2.5" -> esda
        head = spec.split(";")[0].strip()
        for sep in ("[", "<", ">", "=", "!", "~", " "):
            head = head.split(sep)[0]
        if head:
            out.add(normalise(head))
    return out


def extras() -> dict[str, set[str]]:
    pj = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return {name: {normalise(s.split(";")[0].split("[")[0].split(">")[0]
                              .split("<")[0].split("=")[0].split("~")[0].strip())
                   for s in specs}
            for name, specs in
            pj["project"].get("optional-dependencies", {}).items()}


# --------------------------------------------------------------------------
# what the project imports
# --------------------------------------------------------------------------

def source_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for pattern in SOURCE_GLOBS:
        files.extend(sorted(ROOT.glob(pattern)))
    return files


def top_level_imports(path: pathlib.Path) -> set[str]:
    """Every top-level module name this file imports, at any nesting depth.

    `ast.walk` rather than iterating the module body, so an import inside a
    function, a method, a `try` block or an `if TYPE_CHECKING` is counted the
    same as one at the top. See the docstring: the exemption is the bug.

    Relative imports (`from .x import y`, level > 0) are first party by
    definition and contribute nothing.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def first_party() -> set[str]:
    """Names that resolve inside this repository rather than to a package.

    Three separate ways a name can be local, and the first run of this test
    missed one of them:

    * a top-level directory of this repo. `from scripts.probe_transit_parse
      import summarise` has top-level module name `scripts`, and `conftest.py`
      puts the root on `sys.path` so it resolves. The first version of this
      function collected the *stems of files inside* `scripts/` and never the
      directory itself, so it reported `scripts` as an undeclared distribution
      imported by 28 files.
    * a module file in one of those directories. `scripts/` is on `sys.path`
      when a script is run directly, so `import _bootstrap` and
      `import probe_transit_parse` both resolve to files in this tree.
    * a subpackage with an `__init__.py`.

    A test that cries wolf about the project's own modules is a rule 16
    failure and would be switched off inside a week, so this is deliberately
    generous: a false negative here costs one undeclared dependency, a false
    positive costs the whole test.
    """
    local = set(PACKAGES) | {"tests", "conftest"}
    for d in ("scripts", "tests", "angels", "notebooks", "web"):
        directory = ROOT / d
        if not directory.is_dir():
            continue
        local.add(d)                       # the directory, as an import name
        for p in directory.glob("*.py"):
            local.add(p.stem)
        for p in directory.iterdir():
            if p.is_dir() and (p / "__init__.py").exists():
                local.add(p.name)
    return local


def third_party_imports() -> dict[str, list[str]]:
    """Normalised distribution name -> the files that import it."""
    local = first_party()
    out: dict[str, list[str]] = {}
    for f in source_files():
        for name in top_level_imports(f):
            if name in sys.stdlib_module_names or name in local:
                continue
            if name.startswith("_"):
                continue
            dist = IMPORT_TO_DISTRIBUTION.get(normalise(name), normalise(name))
            out.setdefault(dist, []).append(
                str(f.relative_to(ROOT)).replace("\\", "/"))
    return out


# --------------------------------------------------------------------------
# the test this file exists for
# --------------------------------------------------------------------------

def test_every_third_party_import_is_declared() -> None:
    have = declared()
    missing = {dist: files for dist, files in third_party_imports().items()
               if dist not in have}

    assert not missing, (
        "these distributions are imported but declared nowhere in "
        "pyproject.toml:\n"
        + "\n".join(f"    {dist:28} {', '.join(files)}"
                    for dist, files in sorted(missing.items()))
        + "\n\nAdd each to [project] dependencies, or to an extra (which also "
          "needs a tasks.ps1 target -- test_tasks_targets.py enforces that). "
          "If the import name differs from the distribution name, add it to "
          "IMPORT_TO_DISTRIBUTION in this file instead.\n"
          "A lazy import inside a function is still a dependency. That is how "
          "gtfs-realtime-bindings went three days undeclared while carrying "
          "four published measurements."
    )


# --------------------------------------------------------------------------
# the test's own machinery, so a green run means something
# --------------------------------------------------------------------------

def test_the_interpreter_satisfies_requires_python() -> None:
    """The scan's answer depends on the interpreter running it, so pin it.

    `sys.stdlib_module_names` is version-dependent. `tomllib` entered the
    standard library in 3.11, and under 3.10 this very file's own
    `import tomllib` is reported as an undeclared third-party distribution --
    which is how the first run of the scan read it on a 3.10 box.

    That is a wrong answer with an entirely right shape: a real-looking name,
    a real file, a real-looking verdict. `pyproject.toml` says
    `requires-python = ">=3.11"`, so rather than exempting `tomllib` by hand
    -- which would weaken the guard to accommodate the wrong environment --
    the floor is asserted and a 3.10 run fails here, by name, instead of
    producing a plausible lie two tests later.
    """
    pj = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    spec = pj["project"]["requires-python"]
    floor = tuple(int(n) for n in spec.lstrip(">=~^ ").split(",")[0].split("."))
    assert sys.version_info[:len(floor)] >= floor, (
        f"pyproject.toml requires Python {spec} and this is "
        f"{'.'.join(str(n) for n in sys.version_info[:3])}. The import scan in "
        f"this file reads sys.stdlib_module_names, which differs between "
        f"versions, so it would report standard-library modules as undeclared "
        f"dependencies. Run the suite with `.\\tasks.ps1 test`.")


def test_the_scan_actually_reads_a_substantial_amount_of_source() -> None:
    """A glob that matched nothing would make the test above pass vacuously.

    This is the shape of mechanism A: "no undeclared imports" and "I looked at
    no files" returning the same verdict. The threshold is deliberately well
    below the current count so ordinary growth never trips it.
    """
    files = source_files()
    assert len(files) >= 60, (
        f"only {len(files)} source files found under {ROOT}; the globs "
        f"{SOURCE_GLOBS} are probably wrong, and an empty scan passes the "
        f"declaration test for the wrong reason")


def test_the_scan_finds_the_dependencies_we_know_are_there() -> None:
    """Positive control. If `ast` silently stopped yielding imports, the test
    above would go green and stay green."""
    found = third_party_imports()
    for dist in ("pyarrow", "httpx", "duckdb", "numpy"):
        assert dist in found, (
            f"{dist} is imported all over this project and the scan did not "
            f"see it, so the scan is broken rather than the project clean")


def test_a_lazy_import_is_counted() -> None:
    """The exemption that would have hidden sighting 35, asserted absent.

    `scripts/probe_transit_parse.py` imports `google.transit` inside a
    function. If this test ever fails, someone has taught the scan to skip
    function-scope imports, and the guard has been returned to the state that
    let a dependency go undeclared for three days.
    """
    names = top_level_imports(ROOT / "scripts" / "probe_transit_parse.py")
    assert "google" in names, (
        "the function-scope `from google.transit import gtfs_realtime_pb2` in "
        "probe_transit_parse.py was not seen; nesting is not an exemption")


@pytest.mark.parametrize("dist", ["pydantic", "websockets"])
def test_the_two_found_on_the_first_run_stay_declared(dist: str) -> None:
    """Rule 8: fixing an instrument does not retract its readings.

    These two were found by this file's first run on 2026-10-06 and declared
    in the same commit. `pydantic` is imported at module scope in
    `angels/api/routes/labels.py` and arrived only via fastapi. `websockets`
    is called in the live maritime collector and arrived only because
    `uvicorn[standard]` happened to include it -- and that extra's membership
    is an upstream packaging decision, not a contract.

    Named individually so that a future `pyproject.toml` edit which drops one
    fails with its own name rather than inside a set difference.
    """
    assert dist in declared()


def test_every_extra_is_non_empty() -> None:
    """An extra declared with no packages installs nothing and reads as if it
    did. `tasks.ps1 <name>` would succeed, print nothing, and leave the
    import that needed it still failing."""
    empty = [name for name, specs in extras().items() if not specs]
    assert not empty, f"extras declaring no packages: {sorted(empty)}"
