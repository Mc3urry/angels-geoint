"""Download the US maritime limit lines the boundary analysis needs.

    python scripts/fetch_limits.py
    python scripts/fetch_limits.py --list

Writes into data/reference/limits/. Nothing else in the pipeline needs these
files; the boundary analysis needs nothing else.

WHAT IS BEING FETCHED, AND WHY THE SOURCE MATTERS

The 3 nm state seaward limit, the 12 nm territorial sea, the 24 nm
contiguous zone and the 200 nm EEZ are LEGAL lines. They are not "so many
miles from the coastline on a map": they are measured from the official
baseline, which follows the low-water line except across bays and river
mouths, where closing lines cut straight across. In the Chesapeake and
Delaware that difference is tens of kilometres -- exactly the area this
study is about. So the lines come from NOAA's Office of Coast Survey, which
publishes the ones the United States actually asserts, rather than being
buffered out of a coastline by this project.

IF EVERY SOURCE FAILS

The script prints what to download by hand and where to put it. A capstone
that cannot reproduce its own boundary layer is not reproducible, so the
manual path is documented rather than assumed away.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import REFERENCE

DEST = REFERENCE / "limits"

# Tried in order. The ArcGIS services answer with GeoJSON, which needs no
# unpacking and no shapefile reader; the zip is the fallback and the
# canonical product.
# The 3 nm state seaward limit is NOT in the Maritime Limits product: it is
# the Submerged Lands Act boundary, a separate dataset, and the research
# question names 3 nm. Fetched alongside, and missing is not fatal -- the
# analysis reports which lines it actually has.
SOURCES = [
    ("noaa-mlb-geojson",
     "https://services2.arcgis.com/C8EMgrsFcRFL6LrL/ArcGIS/rest/services/"
     "MaritimeLimitsNBoundaries/FeatureServer/0/query"
     "?where=1%3D1&outFields=*&outSR=4326&f=geojson",
     "us_maritime_limits.geojson"),
    ("noaa-mlb-zip",
     "https://maritimeboundaries.noaa.gov/downloads/"
     "USMaritimeLimitsAndBoundariesSHP.zip",
     "us_maritime_limits_shp.zip"),
    ("marinecadastre-mlb-zip",
     "https://marinecadastre.gov/downloads/data/mc/"
     "MaritimeLimitsAndBoundaries.zip",
     "marinecadastre_limits.zip"),
]

SLA_SOURCES = [
    ("sla-3nm-geojson",
     "https://services2.arcgis.com/C8EMgrsFcRFL6LrL/ArcGIS/rest/services/"
     "SubmergedLandsActBoundary/FeatureServer/0/query"
     "?where=1%3D1&outFields=*&outSR=4326&f=geojson",
     "submerged_lands_act_3nm.geojson"),
    ("sla-3nm-zip",
     "https://marinecadastre.gov/downloads/data/mc/"
     "SubmergedLandsActBoundary.zip",
     "submerged_lands_act_3nm.zip"),
]

MANUAL = """
  MANUAL DOWNLOAD

  1. Open https://nauticalcharts.noaa.gov/data/us-maritime-limits-and-boundaries.html
     (or search "NOAA US Maritime Limits and Boundaries").
  2. Download the SHAPEFILE or GeoJSON package.
  3. Unzip it into:
         {dest}
     so that the .shp (or .geojson) files sit directly in that folder.

  The analysis needs the line layers only -- no attributes are read. It
  identifies which line is which by NAME, so keep the original filenames or
  put each limit in its own file named for it (for example
  territorial_sea_12nm.geojson).

  THE 3 NM LINE IS A SECOND DOWNLOAD. Search "Submerged Lands Act Boundary"
  on marinecadastre.gov and unzip it into the same folder, with a filename
  containing "submerged" or "3nm" so the analysis can name it.
"""


def download(url: str, out: Path, *, timeout: float = 120.0) -> tuple[bool, str]:
    import httpx

    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_suffix(out.suffix + ".part")
    try:
        with httpx.stream("GET", url, timeout=timeout,
                          follow_redirects=True) as r:
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            total = int(r.headers.get("content-length", 0))
            done = 0
            with part.open("wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        print(f"\r    {done / 1e6:6.1f}/{total / 1e6:.1f} MB",
                              end="")
    except Exception as exc:                          # noqa: BLE001
        part.unlink(missing_ok=True)
        return False, f"{type(exc).__name__}: {exc}"

    size = part.stat().st_size
    if size < 10_000:
        part.unlink(missing_ok=True)
        return False, f"only {size} bytes -- an error page, not a dataset"
    part.replace(out)
    return True, f"{size / 1e6:.1f} MB"


def unpack(path: Path) -> list[Path]:
    """A zip of shapefiles becomes shapefiles; anything else is left alone."""
    import zipfile

    if path.suffix.lower() != ".zip":
        return [path]
    out = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name.endswith("/"):
                continue
            target = DEST / Path(name).name
            with z.open(name) as src, target.open("wb") as dst:
                dst.write(src.read())
            out.append(target)
    return out


def has_sla() -> bool:
    """Is the 3 nm line already here? Judged by filename, the same way the
    analysis names it."""
    return any(any(t in p.name.lower() for t in ("submerged", "3nm", "sla"))
               for p in existing())


def existing() -> list[Path]:
    return sorted(p for p in DEST.glob("*")
                  if p.suffix.lower() in (".geojson", ".json", ".shp"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--list", action="store_true",
                    help="show the sources and what is already on disk")
    args = ap.parse_args()

    have = existing()
    if have:
        print(f"\n  already in {DEST}:")
        for p in have:
            print(f"    {p.name}  {p.stat().st_size / 1e6:.1f} MB")

    if args.list:
        print("\n  sources, in order of preference:")
        for name, url, _ in SOURCES:
            print(f"    {name:24} {url[:90]}")
        print()
        return 0

    if have and has_sla():
        print("\n  Nothing to do. Delete the folder to re-fetch.\n")
        return 0
    if have:
        print("\n  The maritime limits are already here; fetching only the "
              "3 nm state line.")

    print(f"\n  fetching maritime limits -> {DEST}")
    got_mlb = bool(have)
    for name, url, filename in ([] if have else SOURCES):
        print(f"\n  {name}")
        ok, msg = download(url, DEST / filename)
        print(f"\r    {'OK  ' if ok else 'FAIL'}  {msg}" + " " * 20)
        if not ok:
            continue
        files = unpack(DEST / filename)
        lines = [p for p in files
                 if p.suffix.lower() in (".shp", ".geojson", ".json")]
        if not lines:
            print("    nothing line-shaped inside; trying the next source")
            continue
        print(f"\n  {len(lines)} layer file(s):")
        for p in sorted(lines):
            print(f"    {p.name}")
        got_mlb = True
        break

    if not got_mlb:
        print(MANUAL.format(dest=DEST))
        return 1

    print("\n  now the 3 nm state seaward limit (Submerged Lands Act)")
    for name, url, filename in SLA_SOURCES:
        print(f"\n  {name}")
        ok, msg = download(url, DEST / filename)
        print(f"\r    {'OK  ' if ok else 'FAIL'}  {msg}" + " " * 20)
        if ok and unpack(DEST / filename):
            break
    else:
        print("\n  The 3 nm line could not be fetched. The analysis will "
              "run without it")
        print("  and say so; see the manual note in this script's "
              "docstring.")

    print("\n  Next: python scripts/boundary_analysis.py\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
