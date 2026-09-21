"""Look inside a downloaded Sentinel-1 scene before writing code against it.

    python scripts/inspect_scene.py                    # newest downloaded
    python scripts/inspect_scene.py --all
    python scripts/inspect_scene.py --preview          # also write a PNG
    python scripts/inspect_scene.py --prune            # find duplicate copies
    python scripts/inspect_scene.py --prune --yes      # and delete them

WHY THIS EXISTS, AGAIN

Twice now this project has written code against a data source whose shape was
assumed rather than checked, and twice the assumption was wrong in a way that
produced confident nonsense rather than an error. A GRD product is a zipped
SAFE directory with a particular internal layout, radar-geometry rasters, and
geolocation carried as GCPs rather than an affine transform. Every one of
those is a thing the detector will assume. So look first.

WHAT IT CHECKS, AND WHY EACH ONE MATTERS TO THE DETECTOR

  measurement rasters   which polarisations are present. Ship detection wants
                        VV: the sea returns least in VV, so hulls stand out
                        against the darkest possible background. VH is useful
                        for suppressing ambiguities later.

  dimensions & spacing  an IW GRDH scene is roughly 25000 x 17000 pixels at
                        10 m. That is ~400 megapixels and will not fit in
                        memory naively -- the detector must read in windows.
                        This prints the real numbers so that decision is made
                        on fact.

  GCPs                  a GRD raster is NOT north-up and has no simple affine
                        geotransform. Position comes from a grid of ground
                        control points, and converting a detected pixel to a
                        latitude and longitude means interpolating between
                        them. Getting this wrong puts every vessel in the
                        wrong place while raising no error at all -- the
                        maritime twin of the lon/lat swap.

  acquisition time      the single instant the scene represents. Everything in
                        matching.py depends on it: AIS gets interpolated TO
                        this timestamp via Track.position_at(). A scene whose
                        time you have wrong produces confident, wrong
                        discrepancies.
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

# Re-runs this script under the interpreter that has ANGELS installed, if the
# one invoking it does not. See scripts/_bootstrap.py.
#
# The name check matters. `import _bootstrap` only resolves when scripts/ is on
# sys.path, which is true when this file is RUN and false when the test suite
# IMPORTS it as scripts.<name>. Swallowing every ModuleNotFoundError here would
# also swallow the one _bootstrap raises about 'angels' itself -- turning a
# clear "wrong interpreter" message back into a confusing one.
try:
    import _bootstrap  # noqa: F401  (must precede the angels imports)
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import RAW

DEST = RAW / "sar"


def acquisition_key(name: str) -> str:
    """Fields 0..7 of the SAFE name: what identifies one acquisition.

    S1D_IW_GRDH_1SDV_<start>_<stop>_<orbit>_<datatake>_<productID>[_COG]
    |------------------ identity ------------------| |-- packaging --|

    Two files sharing this key are the same thirty seconds of the same water
    in two encodings, not two observations.
    """
    return "_".join(Path(name).name.split("_")[:8])


def duplicate_groups(paths: list[Path]) -> dict[str, list[Path]]:
    """Local files grouped by acquisition, COG first within each group."""
    groups: dict[str, list[Path]] = {}
    for p in paths:
        groups.setdefault(acquisition_key(p.name), []).append(p)
    for v in groups.values():
        v.sort(key=lambda p: ("_COG" not in p.name, p.name))
    return {k: v for k, v in groups.items() if len(v) > 1}


def find_scenes(newest_only: bool = True) -> list[Path]:
    zips = sorted(DEST.glob("*.zip"), key=lambda p: p.stat().st_mtime,
                  reverse=True)
    return zips[:1] if newest_only and zips else zips


def prune(paths: list[Path], *, confirm: bool = False) -> int:
    """Delete redundant non-COG copies of acquisitions we have twice.

    Dry run unless `confirm`. Deleting a gigabyte of someone's download on a
    guess is not something to do by default, and the whole point of this tool
    is that the two files are interchangeable -- so there is no hurry.
    """
    dupes = duplicate_groups(paths)
    if not dupes:
        print("\n  No duplicate acquisitions on disk.\n")
        return 0

    freed = 0
    print(f"\n  {len(dupes)} acquisition(s) present in more than one encoding:\n")
    for key, group in dupes.items():
        keep, *drop = group
        t = key.split("_")[4]
        print(f"    {t}")
        print(f"      keep  {keep.stat().st_size / 1e6:>6,.0f} MB  {keep.name[:56]}")
        for d in drop:
            freed += d.stat().st_size
            print(f"      drop  {d.stat().st_size / 1e6:>6,.0f} MB  {d.name[:56]}")

    print(f"\n  {freed / 1e9:.1f} GB recoverable.")
    print("  The kept copy is the COG: same pixels, internally tiled, so the")
    print("  detector can read a window without pulling the whole raster.\n")

    if not confirm:
        print("  Dry run. Add --yes to actually delete.\n")
        return 0

    for group in dupes.values():
        for d in group[1:]:
            d.unlink()
            print(f"    deleted {d.name[:60]}")
    print(f"\n  Freed {freed / 1e9:.1f} GB.\n")
    return 0


def describe_archive(zpath: Path) -> dict:
    """What is in the zip, without extracting any of it."""
    out: dict = {"measurements": [], "annotations": [], "total_mb": 0.0}
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            out["total_mb"] += info.file_size / 1e6
            name = info.filename
            low = name.lower()
            if "/measurement/" in low and low.endswith((".tiff", ".tif")):
                out["measurements"].append((name, info.file_size))
            elif "/annotation/" in low and low.endswith(".xml") \
                    and "/calibration/" not in low:
                out["annotations"].append(name)
    out["measurements"].sort(key=lambda t: -t[1])
    return out


def polarisation(name: str) -> str:
    m = re.search(r"-(vv|vh|hh|hv)-", name.lower())
    return m.group(1).upper() if m else "??"


def probe_raster(zpath: Path, member: str) -> dict:
    """Open the measurement raster in place and read its geometry.

    Uses rasterio's zip:// VSI path so the 800 MB tiff is never extracted --
    only the header is read. Extracting first would mean a gigabyte of disk
    and a minute of waiting to learn the image size.
    """
    try:
        import rasterio
    except ImportError:
        return {"error": "rasterio is not installed"}

    uri = f"zip://{zpath.as_posix()}!/{member}"
    try:
        with rasterio.open(uri) as src:
            gcps, crs = src.get_gcps()
            info = {
                "width": src.width,
                "height": src.height,
                "dtype": src.dtypes[0],
                "bands": src.count,
                "megapixels": src.width * src.height / 1e6,
                "n_gcps": len(gcps or []),
                "gcp_crs": str(crs) if crs else None,
                "crs": str(src.crs) if src.crs else None,
                "nodata": src.nodata,
            }
            if gcps:
                lons = [g.x for g in gcps]
                lats = [g.y for g in gcps]
                info["gcp_bounds"] = (min(lons), min(lats),
                                      max(lons), max(lats))
            return info
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def acquisition_time(name: str) -> str:
    m = re.search(r"_(\d{8}T\d{6})_(\d{8}T\d{6})_", name)
    if not m:
        return "?"
    a, b = m.group(1), m.group(2)
    fmt = lambda s: f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[9:11]}:{s[11:13]}:{s[13:15]}"
    return f"{fmt(a)} to {fmt(b)} UTC"


def preview(zpath: Path, member: str, out: Path, max_px: int = 1600) -> str:
    """Decimated PNG of the VV band, so you can SEE the water.

    Not decoration. The first thing that goes wrong in SAR work is looking at
    the wrong raster, or one that is all land, or one where the sea is
    roughened by wind into something no detector will handle. Thirty seconds
    of looking saves a day of tuning a threshold against an image that was
    never going to work.
    """
    try:
        import numpy as np
        import rasterio
        from rasterio.enums import Resampling
    except ImportError as exc:
        return f"skipped ({exc})"

    uri = f"zip://{zpath.as_posix()}!/{member}"
    try:
        with rasterio.open(uri) as src:
            scale = max(1, max(src.width, src.height) // max_px)
            w, h = src.width // scale, src.height // scale
            arr = src.read(1, out_shape=(1, h, w),
                           resampling=Resampling.average).astype("float32")

        # SAR amplitude is extremely long-tailed: a handful of bright metal
        # targets sit orders of magnitude above the sea. Linear scaling makes
        # the whole image black with a few white dots. Clipping at a high
        # percentile is what makes the sea surface visible at all.
        hi = float(np.percentile(arr[arr > 0], 99.0)) if (arr > 0).any() else 1.0
        img = np.clip(arr / max(hi, 1e-6), 0, 1)
        img = (img ** 0.5 * 255).astype("uint8")      # gamma, for the eye

        try:
            from PIL import Image
            Image.fromarray(img).save(out)
        except ImportError:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            plt.imsave(out, img, cmap="gray")
        return str(out)
    except Exception as exc:
        return f"failed ({type(exc).__name__}: {exc})"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--all", action="store_true", help="every downloaded scene")
    ap.add_argument("--preview", action="store_true", help="also write a PNG")
    ap.add_argument("--path", type=Path, help="a specific .zip")
    ap.add_argument("--prune", action="store_true",
                    help="find acquisitions downloaded in both encodings and "
                         "report the redundant copies (dry run)")
    ap.add_argument("--yes", action="store_true",
                    help="with --prune, actually delete them")
    args = ap.parse_args()

    if args.prune:
        return prune(find_scenes(newest_only=False), confirm=args.yes)

    scenes = [args.path] if args.path else find_scenes(not args.all)

    if not scenes:
        print(f"\n  No scenes in {DEST}")
        print("  Run: python scripts/fetch_sar.py --download 1\n")
        return 1

    for zpath in scenes:
        if not zpath.exists():
            print(f"\n  not found: {zpath}\n")
            return 1

        print(f"\n  {zpath.name}")
        print("  " + "=" * 70)
        print(f"    on disk       {zpath.stat().st_size / 1e6:,.0f} MB")
        print(f"    acquired      {acquisition_time(zpath.name)}")

        arc = describe_archive(zpath)
        print(f"    uncompressed  {arc['total_mb']:,.0f} MB")
        print(f"    annotations   {len(arc['annotations'])}")

        if not arc["measurements"]:
            print("\n    NO MEASUREMENT RASTERS. This is not a GRD product, or")
            print("    the download is truncated. Delete it and re-fetch.\n")
            continue

        print(f"\n    measurement rasters:")
        for name, size in arc["measurements"]:
            print(f"      {polarisation(name):<4}{size / 1e6:>8,.0f} MB   "
                  f"{Path(name).name[:46]}")

        vv = next((m for m in arc["measurements"]
                   if polarisation(m[0]) == "VV"), arc["measurements"][0])
        member, _ = vv
        print(f"\n    probing {polarisation(member)} band...")
        info = probe_raster(zpath, member)

        if "error" in info:
            print(f"      {info['error']}")
            if "rasterio" in info["error"]:
                print("\n      Install it -- the detector cannot be written")
                print("      without it:   conda install -c conda-forge rasterio")
            print()
            continue

        print(f"      size        {info['width']:,} x {info['height']:,} px "
              f"({info['megapixels']:,.0f} MP)")
        print(f"      dtype       {info['dtype']}  ({info['bands']} band)")
        print(f"      GCPs        {info['n_gcps']}  in {info['gcp_crs']}")
        if info.get("gcp_bounds"):
            b = info["gcp_bounds"]
            print(f"      covers      {b[0]:.2f}, {b[1]:.2f}  to  "
                  f"{b[2]:.2f}, {b[3]:.2f}")

        # The two facts that shape the detector.
        print()
        if info["megapixels"] > 100:
            print(f"      NOTE: {info['megapixels']:,.0f} MP. Reading this "
                  f"whole raster as float32 would need")
            print(f"      about {info['megapixels'] * 4 / 1024:,.1f} GB of RAM. "
                  f"The detector must work in windows.")
        if info["n_gcps"] and not info["crs"]:
            print("      NOTE: geolocation is by GCPs, not an affine transform.")
            print("      Pixel -> lat/lon must interpolate the GCP grid. Get")
            print("      this wrong and every vessel lands in the wrong place")
            print("      with no error raised.")

        if args.preview:
            out = zpath.with_suffix(".preview.png")
            print(f"\n    preview -> {preview(zpath, member, out)}")

        print()

    print("  Next: CFAR detection against this raster.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
