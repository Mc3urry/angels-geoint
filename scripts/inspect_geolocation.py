"""What the geolocation grid in a scene actually looks like.

    python scripts/inspect_geolocation.py                 # newest scene
    python scripts/inspect_geolocation.py --all
    python scripts/inspect_geolocation.py --path <scene.zip>

A DIAGNOSTIC, NOT A FIX

check() refused a Chesapeake slice at 69 m and reported that convergence could
not be confirmed, which leaves two quite different explanations standing:

    the grid is too small to test at 4x spacing
    the error is not shrinking as h^2 -- the geometry has structure finer
    than the published grid

Those call for opposite responses, and a synthetic grid calibrated to
reproduce the 69 m did NOT reproduce the refusal, so the cause is a property
of this scene that the synthetic does not have. Guessing which is how a limit
gets raised for the wrong reason.

So this prints the grid itself: its shape, whether its lines are evenly
spaced, the holdout error at every spacing the grid can support, the ratios
between them, and where on the raster the error actually lives. Nothing here
changes any behaviour. It exists to make the next decision an informed one.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.adapters.maritime.geolocate import (
    GCPGrid, Geolocator, _haversine_m,
)
from angels.config import RAW

SAR = RAW / "sar"


def expand(pattern: Path) -> list[Path]:
    """A --path that may contain wildcards, on either platform.

    PowerShell does not expand wildcards in a program's arguments the way a
    POSIX shell does -- it hands the pattern through literally, and Python
    then tries to open a file with a '*' in its name and reports
    'OSError: Invalid argument', which says nothing about globbing.
    """
    raw = str(pattern)
    if not any(ch in raw for ch in "*?["):
        return [pattern]
    p = Path(raw)
    return sorted(p.parent.glob(p.name))


def spacing_report(vals: list[int], label: str) -> None:
    if len(vals) < 2:
        print(f"    {label}: {len(vals)} line(s) -- cannot interpolate")
        return
    gaps = [b - a for a, b in zip(vals, vals[1:])]
    lo, hi = min(gaps), max(gaps)
    even = "even" if hi - lo <= max(1, lo * 0.02) else "UNEVEN"
    print(f"    {label}: {len(vals)} lines, spacing {lo}-{hi} px ({even})")
    if even == "UNEVEN":
        print(f"      gaps: {gaps}")


def holdout_at(loc: Geolocator, step: int):
    """Error at `step` times the node spacing, plus where the worst one is."""
    grid = loc._grid
    rows, cols = grid.rows, grid.cols
    keep_r = set(rows[::step]) | {rows[-1]}
    keep_c = set(cols[::step]) | {cols[-1]}
    if len(keep_r) < 2 or len(keep_c) < 2:
        return None

    coarse = GCPGrid([g for g in loc.gcps
                      if g.row in keep_r and g.col in keep_c])
    worst = (0.0, None)
    errs = []
    for g in loc.gcps:
        if g.row in keep_r and g.col in keep_c:
            continue
        lon, lat = coarse.lonlat(g.col, g.row)
        e = _haversine_m(g.y, g.x, lat, lon)
        errs.append(e)
        if e > worst[0]:
            worst = (e, g)
    if not errs:
        return None
    return {
        "n": len(errs),
        "kept": f"{len(keep_r)}x{len(keep_c)}",
        "rms": math.sqrt(sum(e * e for e in errs) / len(errs)),
        "worst": worst[0],
        "where": (worst[1].row, worst[1].col) if worst[1] else None,
    }


def inspect(zpath: Path) -> None:
    import rasterio
    sys.path.insert(0, str(Path(__file__).parent))
    from detect_ships import vv_member

    uri = f"zip://{zpath.as_posix()}!/{vv_member(zpath)}"
    print(f"\n  {zpath.name[:66]}")

    with rasterio.open(uri) as src:
        gcps, _ = src.get_gcps()
        print(f"  raster {src.width:,} x {src.height:,} px, {len(gcps)} GCPs")

        loc = Geolocator(gcps)
        print(f"  method {loc.method}")
        if loc._grid is None:
            print("\n  NOT A LATTICE -- nothing below applies.\n")
            return

        rows, cols = loc._grid.rows, loc._grid.cols
        print(f"\n  grid {len(rows)} rows x {len(cols)} cols "
              f"= {len(rows) * len(cols)} nodes")
        spacing_report(rows, "azimuth (rows)")
        spacing_report(cols, "range (cols) ")

        # Node spacing in metres, which is what the error has to be read
        # against. A 69 m error between nodes 12 km apart is a different
        # statement from the same error between nodes 2 km apart.
        g00 = loc._grid.lonlat(cols[0], rows[0])
        g01 = loc._grid.lonlat(cols[1], rows[0])
        g10 = loc._grid.lonlat(cols[0], rows[1])
        print(f"    one range step  ~ "
              f"{_haversine_m(g00[1], g00[0], g01[1], g01[0]) / 1000:.1f} km")
        print(f"    one azimuth step~ "
              f"{_haversine_m(g00[1], g00[0], g10[1], g10[0]) / 1000:.1f} km")

        print(f"\n    {'step':>5}{'kept':>9}{'tested':>8}{'rms m':>9}"
              f"{'worst m':>9}{'ratio':>8}   worst at (row, col)")
        prev = None
        for step in (2, 3, 4, 5, 6):
            r = holdout_at(loc, step)
            if r is None:
                continue
            ratio = f"{r['rms'] / prev:.2f}x" if prev else "--"
            print(f"    {step:>5}{r['kept']:>9}{r['n']:>8}{r['rms']:9.1f}"
                  f"{r['worst']:9.1f}{ratio:>8}   {r['where']}")
            if step == 2:
                prev = r["rms"]
            elif step == 4:
                prev4 = r["rms"]

        # Where the error lives, as a coarse map over the node lattice. A
        # scene whose error is concentrated in a few cells is unevenly usable,
        # not unusable -- and the difference is only visible as a picture.
        field = loc.error_field()
        if field:
            # The lowest bucket is '.', NOT a space. A space means "no
            # measurement here", and the first version of this map used one
            # for both -- so a node measured at 2 m looked identical to a node
            # never tested, and three quarters of a healthy row read as
            # missing data. An error reported as an absence, in the tool
            # written to find errors reported as absences.
            scale = ".:-=+*#%@"
            hi = max(field.values()) or 1.0
            print(f"\n  error by node, 0 to {hi:.0f} m "
                  f"(range across, azimuth down):")
            for r in rows:
                line = "".join(
                    scale[min(len(scale) - 1,
                              int(field[(r, c)] / hi * len(scale)))]
                    if (r, c) in field else " "
                    for c in cols)
                print(f"    |{line}|")
            print(f"    scale: {'  '.join(scale)}   "
                  f"({hi / len(scale):.0f} m per step, max {hi:.0f} m)")
            print("    space = a node the coarse grid was BUILT from, so it")
            print("     carries no measurement -- bilinear reproduces those")
            print("     exactly, which says nothing. '.' is a measured node")
            print("     with a small error, and is not the same thing.")

        acc = loc.accuracy()
        print(f"\n  accuracy() reports:\n    {acc}")
        print(f"    best_m = {acc.best_m:.1f} m "
              f"({'extrapolated' if acc.implied_m else 'holdout, not extrapolated'})")

        print("\n  HOW TO READ THIS")
        print("    ratio near 4.0 between step 2 and step 4 means bilinear")
        print("    error is shrinking as h^2, and the step-2 figure can be")
        print("    divided by it to get the error that actually applies.")
        print("    A ratio near 1 means refining the grid does not help: the")
        print("    geometry has detail finer than the published nodes, and")
        print("    the step-2 figure is the honest answer.")
        print("    A worst case parked on row 0, the last row, col 0 or the")
        print("    last col is an EDGE effect, not a property of the scene --")
        print("    holdout clamps there instead of interpolating.\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--path", type=Path)
    args = ap.parse_args()

    if args.path:
        # PowerShell does not expand wildcards for a program's arguments the
        # way a POSIX shell does -- it hands the pattern through literally and
        # Python then opens a file with a '*' in its name. Expanding here
        # means --path behaves the same on both platforms, which is the whole
        # reason the pattern was typed.
        scenes = expand(args.path)
        if not scenes:
            print(f"\n  Nothing matches {args.path}\n")
            return 1
    else:
        scenes = sorted(SAR.glob("*.zip"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
        if not args.all:
            scenes = scenes[:1]

    if not scenes:
        print(f"\n  No scenes in {SAR}\n")
        return 1

    for s in scenes:
        try:
            inspect(s)
        except Exception as exc:
            print(f"\n  FAILED on {s.name[:50]}: {type(exc).__name__}: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
