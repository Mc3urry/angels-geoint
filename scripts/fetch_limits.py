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
study is about. So the lines come from the agencies that publish the ones
the United States actually asserts, rather than being buffered out of a
coastline by this project.

CORRECTION, 2026-09-30

This docstring used to say all four lines come from NOAA's Office of Coast
Survey. Three do. The 3 nm line does not, and NOAA says so itself: the US
Maritime Limits and Boundaries FAQ states that people looking for a 3 nm
line "are actually looking for the Submerged Lands Act federal/state
boundary provided by BOEM". So the two automated SLA fetches in this script
were not broken and did not need better error handling -- they were pointed
at services that have never existed. Both guessed at an ArcGIS host by
pattern and returned HTTP 400/404 for the honest reason that there is
nothing there.

The real service is BOEM's, verified live on 2026-09-30:

    gis.boem.gov/server/rest/services/BOEM_BSEE/MMC_Layers/FeatureServer/8
    "Submerged Lands Act Boundary", esriGeometryPolyline, NAD83 (4269)

so the 3 nm line is an automated fetch after all, not the manual download
this file has claimed since 25 September.

TWO THINGS THE FETCH MUST REPORT, NOT ASSUME

BOEM's layer is a boundary layer, not a 3-nm-line layer: `BDRY_NAME_TEXT`
distinguishes the segments, and nothing downstream identifies a line by
anything but its FILENAME. A file called submerged_lands_act_3nm.geojson
whose contents are partly something else is exactly the failure this project
keeps finding, so the fetch prints the distinct boundary names it actually
received and the feature count, and refuses a response that is an ArcGIS
error object or has no features.

ArcGIS also truncates silently at maxRecordCount and says so only in
`exceededTransferLimit`. That flag is checked and reported, because a
partial line read as a whole line moves every band assignment.

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

# Verified live 2026-09-30. The two entries that used to be here --
# services2.arcgis.com/.../SubmergedLandsActBoundary and
# marinecadastre.gov/downloads/data/mc/SubmergedLandsActBoundary.zip -- were
# guessed from the pattern of the NOAA sources above and never existed; they
# are recorded in the docstring rather than silently replaced.
SLA_SOURCES = [
    # BOEM is the publisher of record. Polyline, the line itself.
    ("boem-sla-geojson",
     "https://gis.boem.gov/server/rest/services/BOEM_BSEE/MMC_Layers/"
     "FeatureServer/8/query"
     "?where=1%3D1&outFields=*&outSR=4326&returnGeometry=true&f=geojson",
     "submerged_lands_act_3nm.geojson"),
    # THERE IS DELIBERATELY NO POLYGON FALLBACK. See the note below.
]

# WHY NOAA's USStateSubmergedLands IS NOT A FALLBACK (2026-10-02)
#
# It was one, for about four hours, and it did real damage. BOEM answered
# HTTP 500, the fallback fetched 153 MB of 483 state-submerged-lands
# POLYGONS, and every downstream check passed it:
#
#   - describe_sla said OK. It checked that the JSON parsed, that there was
#     no error object, that features existed and that nothing was truncated.
#     It printed "BDRY_NAME_TEXT: None" -- the field does not exist on these
#     features -- and still said OK.
#   - the file was named submerged_lands_state_polygons.geojson precisely so
#     it would not claim to be the line. But boundary_analysis.limit_sets
#     labels any file whose stem contains "submerged", "3nm" or "sla" as the
#     3 nm limit, and read_lines turns polygon rings into runs.
#   - so 483 polygons tiling the whole US coast -- lateral boundaries,
#     state-to-state divisions, every ring segment -- became "the 3 nm state
#     seaward limit", and were folded into "any limit".
#
# The result: 548 detections moved into the within-2 nm band, 730 left the
# beyond-10 nm band, and the headline observed/expected ratio went from
# 0.396 to 5.938. The sign flipped and it flipped TOWARD the hypothesis.
#
# A polygon dataset cannot substitute for a line dataset in this pipeline,
# so it is not offered as one. If BOEM is down, the 3 nm line is missing,
# the analysis says it is missing, and that is the correct outcome.


def describe_sla(path: Path) -> tuple[bool, str]:
    """Say what actually arrived. A 200 with an error object in it is still
    an error, and a truncated line is not the line."""
    import json

    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:                          # noqa: BLE001
        return False, f"not JSON: {type(exc).__name__}"

    if "error" in doc:
        e = doc["error"]
        return False, f"ArcGIS error {e.get('code')}: {e.get('message')}"

    feats = doc.get("features") or []
    if not feats:
        return False, "0 features -- valid GeoJSON of nothing"

    names = sorted({str(f.get("properties", {}).get("BDRY_NAME_TEXT"))
                    for f in feats})
    kinds = sorted({str((f.get("geometry") or {}).get("type")) for f in feats})
    msg = (f"{len(feats)} features, geometry {'/'.join(kinds)}, "
           f"BDRY_NAME_TEXT: {', '.join(names[:6])}"
           + (f" (+{len(names) - 6} more)" if len(names) > 6 else ""))

    # A LEGAL LIMIT IS A LINE. Added 2026-10-02 after this function returned
    # OK for 483 polygons; see the note on SLA_SOURCES above.
    areal = [k for k in kinds if "Polygon" in k]
    if areal:
        return False, (f"{'/'.join(areal)}, not a line -- {msg}. Polygon "
                       f"rings read as a limit put an edge beside every "
                       f"point in the study area.")

    # The identifying field is what tells the seaward line from the lateral
    # state boundaries. Absent means this is not the layer it was asked for,
    # whatever the filename says.
    if names == ["None"]:
        return False, (f"no BDRY_NAME_TEXT on any feature -- {msg}. Nothing "
                       f"downstream can tell which boundary this is.")
    if doc.get("exceededTransferLimit") or doc.get("properties", {}).get(
            "exceededTransferLimit"):
        return False, ("TRUNCATED by maxRecordCount -- " + msg
                       + ". Re-fetch with paging; a partial line read as a "
                         "whole line moves every band assignment.")
    return True, msg

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

  THE 3 NM LINE IS A SECOND DOWNLOAD, AND NOT FROM NOAA. It is the
  Submerged Lands Act boundary, published by BOEM; NOAA's own FAQ says so.
  Open

      https://gis.boem.gov/server/rest/services/BOEM_BSEE/MMC_Layers/FeatureServer/8

  use the Query form with where=1=1, outFields=*, outSR=4326, format
  geoJSON, and save the result into the same folder as

      submerged_lands_act_3nm.geojson

  Check before you trust it: the layer is a boundary layer, so read the
  distinct BDRY_NAME_TEXT values and confirm they are the seaward line and
  not the lateral state boundaries. If the response carries
  exceededTransferLimit, it is truncated and has to be paged.
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
        if not ok:
            continue
        files = unpack(DEST / filename)
        if not files:
            continue
        if filename.endswith(".geojson"):
            good, what = describe_sla(DEST / filename)
            print(f"    {'OK  ' if good else 'FAIL'}  {what}")
            if not good:
                (DEST / filename).unlink(missing_ok=True)
                continue
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
