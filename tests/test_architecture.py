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
