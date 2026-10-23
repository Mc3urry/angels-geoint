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


def _footprints() -> dict[str, dict[str, str]]:
    """Every footprint and its string fields, read from collector.ps1."""
    block = re.search(r"\$Footprints\s*=\s*@\{(.*?)\n\}", _collector(), re.S).group(1)
    out: dict[str, dict[str, str]] = {}
    # Each entry runs from `name = @{` to the `}` that closes it.
    for m in re.finditer(r"^\s*'?([\w-]+)'?\s*=\s*@\{(.*?)\}\s*$", block, re.S | re.M):
        fields = dict(re.findall(r"(\w+)\s*=\s*(\"[^\"]*\"|'[^']*'|\d+)", m.group(2)))
        out[m.group(1)] = {k: v.strip("\"'") for k, v in fields.items()}
    return out


DEFAULT_SCRIPT = "scripts\\ingest_aviation.py"

# What each builder branch passes. Restated here so the test can say which
# flags a footprint receives -- and test_the_restated_branches_match_the_builder
# checks this restatement against the builder's own text, so it cannot drift.
SEA_FLAGS = {"--aoi", "--log-file"}
POLL_FLAGS = {"--aoi", "--interval", "--flush-every", "--log-file"}
WINDOWS_EXTRA = {"-v"}


def _flags_passed(fp: dict[str, str]) -> set[str]:
    if fp.get("ArgTemplate"):
        return set(re.findall(r"(--[\w-]+)", fp["ArgTemplate"])) | WINDOWS_EXTRA
    if fp.get("SeaAoi"):
        return SEA_FLAGS | WINDOWS_EXTRA
    return POLL_FLAGS | WINDOWS_EXTRA


def _flags_declared(script: str) -> set[str]:
    src = (ROOT / script.replace("\\", "/")).read_text(encoding="utf-8")
    return set(re.findall(r'add_argument\(\s*(?:"-\w",\s*)?"(--?[\w-]+)"', src)) | (
        {"-v"} if re.search(r'add_argument\(\s*"-v"', src) else set())


def test_every_footprint_script_exists() -> None:
    missing = [(k, fp.get("Script", DEFAULT_SCRIPT)) for k, fp in _footprints().items()
               if not (ROOT / fp.get("Script", DEFAULT_SCRIPT).replace("\\", "/")).is_file()]
    assert not missing, f"footprints naming a script that is not there: {missing}"


def test_every_flag_the_collector_passes_is_one_the_script_accepts() -> None:
    """THE COLLECTOR CONTRACT, checked from both ends. 2026-10-23.

    collector.ps1 starts each collector with flags; each script declares its
    own with argparse. Nothing compared the two. roads-wzdx was about to be
    registered with --log-file, which fetch_wzdx.py did not accept -- and an
    unknown flag is an argparse exit 2 on the first line of main(), at every
    logon, with nobody watching. `status` would have shown a collector that
    never ran, which is the nine silent days of September again.

    Two independent sources, neither a copy of the other: the PowerShell that
    builds the command line and the Python that parses it. That is the
    property test_tasks_targets' parity checks lack -- see mechanism Y.
    """
    bad = []
    for k, fp in _footprints().items():
        script = fp.get("Script", DEFAULT_SCRIPT)
        unknown = _flags_passed(fp) - _flags_declared(script)
        if unknown:
            bad.append((k, script, sorted(unknown)))
    assert not bad, (
        "collector.ps1 passes flags these scripts do not declare; the task "
        f"would exit on argparse at every start: {bad}")


def test_the_restated_branches_match_the_builder() -> None:
    """The flag sets above are a restatement, so they are checked against the
    builder's own lines. Edit the builder and this fails until the test agrees."""
    text = _collector()
    sea = re.search(r'\$args = "`"\$Script`" (--aoi[^\n]*)"\s*\n\s*\} else \{', text)
    poll = re.search(r'\} else \{\s*\n\s*\$args = "`"\$Script`" (--aoi[^\n]*)"', text)
    assert sea and poll, "could not find the two builder branches in Install-Collector"
    assert set(re.findall(r"(--[\w-]+)", sea.group(1))) == SEA_FLAGS
    assert set(re.findall(r"(--[\w-]+)", poll.group(1))) == POLL_FLAGS


def test_process_detection_is_derived_from_the_footprints() -> None:
    r"""A collector the process scan cannot match is one `status` cannot see.

    REWRITTEN 2026-10-23. This test used to read the literal alternation
    'ingest_(aviation|maritime|adsbfi)\.py' out of Find-CollectorProcesses and
    check every ingest_* script against it. It collected scripts with the
    pattern `ingest_\w+`, so a collector named anything else was invisible to
    the test exactly as it was invisible to the scan -- and roads-wzdx runs
    fetch_wzdx.py. Both passed while the new collector would have shown as
    stopped while running. A guard specified by the names in front of it is
    mechanism N.

    The scan now builds its pattern from $Footprints. This asserts the
    literal list has not come back, and that the pattern is built from the
    table rather than typed.
    """
    fn = re.search(r"function Find-CollectorProcesses \{(.*?)\n\}", _collector(), re.S).group(1)
    code = "\n".join(ln for ln in fn.splitlines() if not ln.lstrip().startswith("#"))
    assert not re.search(r"CommandLine -match '[^']*\(\w+\|", code), (
        "Find-CollectorProcesses matches a literal list of script names again; "
        "derive it from $Footprints so a new collector is covered by its row")
    assert "$leafOf" in code and "$Footprints[" in code


def test_process_detection_anchors_at_a_path_separator() -> None:
    """tests/test_fetch_wzdx.py exists, so an unanchored "fetch_wzdx.py"
    matches `python -m pytest tests/test_fetch_wzdx.py` and reports the
    collector running whenever someone runs that file."""
    fn = re.search(r"function Find-CollectorProcesses \{(.*?)\n\}", _collector(), re.S).group(1)
    assert r"""'(?:^|[\\/"\s])'""" in fn
    for k, fp in _footprints().items():
        leaf = fp.get("Script", DEFAULT_SCRIPT).split("\\")[-1]
        shadow = ROOT / "tests" / f"test_{leaf}"
        if shadow.exists():
            pat = r'(?:^|[\\/"\s])' + re.escape(leaf) + r'(?:"|\s|$)'
            assert not re.search(pat, f"python -m pytest tests\\test_{leaf} -q"), k


def test_a_footprint_without_an_aoi_flag_declares_its_own_arguments() -> None:
    """The two hard-coded branches both pass --aoi. A script that does not
    take one must say how it is to be started, or it falls into a branch that
    kills it."""
    for k, fp in _footprints().items():
        script = fp.get("Script", DEFAULT_SCRIPT)
        if "--aoi" not in _flags_declared(script):
            assert fp.get("ArgTemplate"), (
                f"{k} runs {script}, which takes no --aoi, and has no ArgTemplate")
