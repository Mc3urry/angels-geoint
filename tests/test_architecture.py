"""Architectural invariants, enforced by the build.

The rule these protect -- core must never import adapters -- is the entire
architectural claim of this project. A rule that lives only in a README is a
rule you will break in week fifteen. A rule that fails the build is one you
will not.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
CORE = ROOT / "angels" / "core"


def _imports(path: pathlib.Path) -> list[str]:
    """Every module name imported by a file, absolute and relative alike."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names.append(base)
            names += [f"{base}.{a.name}" for a in node.names]
    return names


def test_core_never_imports_adapters() -> None:
    """The load-bearing rule. Detection logic stays domain-blind."""
    offenders: list[str] = []
    for path in sorted(CORE.rglob("*.py")):
        for name in _imports(path):
            if "adapters" in name:
                offenders.append(f"{path.relative_to(ROOT)} imports {name}")
    assert not offenders, (
        "core must not depend on any adapter. Move the domain-specific part "
        "into the adapter's PlausibilityModel instead:\n  "
        + "\n  ".join(offenders)
    )


def test_core_never_imports_analysis_or_api() -> None:
    """Dependencies point inward. Core is the innermost layer."""
    offenders: list[str] = []
    for path in sorted(CORE.rglob("*.py")):
        for name in _imports(path):
            if any(x in name for x in ("angels.analysis", "angels.api",
                                       "angels.inversion", "angels.forensics")):
                offenders.append(f"{path.relative_to(ROOT)} imports {name}")
    assert not offenders, "core must not depend on outer layers:\n  " + \
        "\n  ".join(offenders)


def test_core_has_no_heavy_dependencies() -> None:
    """Core stays stdlib-only, so it is fast to import and trivial to test.

    Geopandas, rasterio, pyproj and friends belong in analysis and adapters.
    If you need ellipsoidal geodesy in core one day, that is a deliberate
    decision -- delete this test and say why in docs/core-changelog.md.
    """
    heavy = {"geopandas", "rasterio", "pyproj", "shapely", "pandas",
             "numpy", "duckdb", "fastapi", "sklearn", "torch"}
    offenders: list[str] = []
    for path in sorted(CORE.rglob("*.py")):
        for name in _imports(path):
            if name.split(".")[0] in heavy:
                offenders.append(f"{path.relative_to(ROOT)} imports {name}")
    assert not offenders, "core should stay stdlib-only:\n  " + "\n  ".join(offenders)


def test_every_package_has_an_init() -> None:
    """Namespace packages work until they suddenly do not. Be explicit."""
    missing: list[str] = []
    for d in sorted((ROOT / "angels").rglob("*")):
        if d.is_dir() and any(d.glob("*.py")) and not (d / "__init__.py").exists():
            missing.append(str(d.relative_to(ROOT)))
    assert not missing, "missing __init__.py in:\n  " + "\n  ".join(missing)


def test_every_module_imports() -> None:
    """Import every module in the package.

    Catches the class of break where a constant gets renamed in config.py and
    an importer three directories away is left pointing at the old name. That
    is invisible to every other test here, because nothing else touches those
    modules -- you find out when you run the script, which is the worst time.
    """
    import importlib

    broken: list[str] = []
    for path in sorted((ROOT / "angels").rglob("*.py")):
        rel = path.relative_to(ROOT).with_suffix("")
        mod = ".".join(rel.parts)
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        try:
            importlib.import_module(mod)
        except ImportError as exc:
            broken.append(f"{mod}: {exc}")

    assert not broken, "modules that fail to import:\n  " + "\n  ".join(broken)


def test_scripts_import() -> None:
    """Same guard for scripts/, which is where stale imports actually bit.

    Scripts are not part of the package, so they are loaded by path. Only
    import failures are reported -- a script that needs credentials or network
    at import time would be a separate design problem.
    """
    import importlib.util

    broken: list[str] = []
    for path in sorted((ROOT / "scripts").glob("*.py")):
        spec = importlib.util.spec_from_file_location(f"_script_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except ImportError as exc:
            broken.append(f"scripts/{path.name}: {exc}")
        except Exception:
            pass  # not an import problem; not this test's business

    assert not broken, "scripts that fail to import:\n  " + "\n  ".join(broken)


def test_every_collector_writes_a_heartbeat() -> None:
    """A collector that does not record being alive cannot be trusted later.

    core/uptime.py exists because a gap in an archive is ambiguous: an hour
    with nothing in it is either a quiet sky, a quiet sea, or a collector
    that was not running. Only a heartbeat written AT THE TIME can tell them
    apart, and the maritime reception grid makes exactly the same call the
    aviation one does.

    The aviation collector had this from the start; the maritime one shipped
    on 2026-09-23 without it, and nobody noticed, because the missing thing
    is invisible by construction -- its absence looks like an empty sea. A
    test is the only place that omission is visible before it matters.

    Structural on purpose. Whether the heartbeats are CORRECT is the
    collector's own tests' business; whether one exists at all is an
    invariant of being a collector.
    """
    offenders: list[str] = []
    for path in sorted((ROOT / "scripts").glob("ingest_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        built = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "HeartbeatLog"
            for node in ast.walk(tree)
        )
        if not built:
            offenders.append(f"scripts/{path.name}")

    assert not offenders, (
        "every ingest script must construct a HeartbeatLog, so that an empty "
        "hour can be told from an hour nobody collected:\n  "
        + "\n  ".join(offenders)
    )

