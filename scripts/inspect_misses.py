"""Look at the radar where a reported vessel was missed.

    python scripts/inspect_misses.py                  # every scene with a missed- file
    python scripts/inspect_misses.py --all-lengths    # small craft too
    python scripts/inspect_misses.py --half 200       # 4 km chips instead of 3

For each vessel the radar missed, cuts a chip of the Sentinel-1 image centred
on where AIS says the vessel was, and writes

    data/interim/misses/<scene>/<mmsi>.png   the chip, AIS position ringed,
                                             detections boxed
    data/interim/misses/misses.csv           one row per vessel, with numbers

WHY THIS EXISTS

The first two passes missed a 188 m gas carrier, a 200 m cargo ship and a
146 m government vessel, all in open, searched water, with no detection
within 2.5-4.5 km. A 188 m steel hull is about the brightest thing on the
sea. A miss like that is not a sensitivity problem, and turning the threshold
down would only buy more clutter. It is one of three different things, and
the chip tells them apart at a glance:

    a bright target NEAR the ring     the vessel is there and the detector
                                      did not fire -- a CFAR problem
    a bright target FAR from the ring the vessel is there and AIS put it
                                      somewhere else -- a timing or position
                                      problem, and a matching problem
    nothing bright at all             the vessel was not there when the radar
                                      looked -- its AIS was wrong, stale, or
                                      not its own

Only the first is fixed by tuning the detector. Tuning before looking is how
a project "fixes" the second and third by making the detector noisier.

The numbers in the CSV say the same thing without the picture: the
brightest pixel within 300 m of the AIS position, the brightest within the
whole chip, how far apart they are, and both as a multiple of the chip's
median, which is roughly the sea's background.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import EVENTS, INTERIM, RAW

SAR = RAW / "sar"
OUT = INTERIM / "misses"
PIXEL_M = 10.0
NEAR_M = 300.0


def pixel_of(loc, lon: float, lat: float, *, start=None,
             iters: int = 30) -> tuple[float, float]:
    """(col, row) for a lon/lat, by Newton's method on loc.lonlat.

    The geolocator only runs forward. It is smooth, so a few Newton steps
    from the nearest control point converge to well under a pixel.
    """
    if start is None:
        best = min(loc.gcps, key=lambda g: (g.x - lon) ** 2 + (g.y - lat) ** 2)
        col, row = float(best.col), float(best.row)
    else:
        col, row = start
    h = 1.0
    for _ in range(iters):
        x0, y0 = loc.lonlat(col, row)
        ex, ey = lon - x0, lat - y0
        if abs(ex) < 1e-8 and abs(ey) < 1e-8:
            break
        xc, yc = loc.lonlat(col + h, row)
        xr, yr = loc.lonlat(col, row + h)
        a, b = (xc - x0) / h, (xr - x0) / h
        c, d = (yc - y0) / h, (yr - y0) / h
        det = a * d - b * c
        if det == 0:
            break
        dcol = (d * ex - b * ey) / det
        drow = (-c * ex + a * ey) / det
        col, row = col + dcol, row + drow
    return col, row


def vv_member(zpath: Path) -> str:
    """The VV measurement raster inside a SAFE zip. Same rule as
    detect_ships.vv_member, repeated rather than imported so this script
    does not pull in the whole detector to read one band."""
    import zipfile
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            low = info.filename.lower()
            if "/measurement/" in low and "-vv-" in low \
                    and low.endswith((".tiff", ".tif")):
                return info.filename
    raise FileNotFoundError(f"no VV measurement raster inside {zpath.name}")


def scene_zip(scene: str) -> Path | None:
    hits = sorted(SAR.glob(f"{scene}*.zip"))
    return hits[0] if hits else None


def chip_png(arr, path: Path, marks_ais, marks_det, scale: int = 2) -> None:
    """Log-stretched greyscale, AIS ringed red, detections boxed green."""
    import numpy as np
    from PIL import Image, ImageDraw

    a = arr.astype("float64")
    a[a <= 0] = np.nan
    lg = np.log10(a)
    lo, hi = np.nanpercentile(lg, 2), np.nanpercentile(lg, 99.9)
    g = np.clip((lg - lo) / max(hi - lo, 1e-9), 0, 1)
    g = np.nan_to_num(g, nan=0.0)
    img = Image.fromarray((g * 255).astype("uint8")).convert("RGB")
    img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
    dr = ImageDraw.Draw(img)
    r_near = NEAR_M / PIXEL_M * scale
    for (c, r) in marks_ais:
        c, r = c * scale, r * scale
        dr.ellipse([c - r_near, r - r_near, c + r_near, r + r_near],
                   outline=(230, 40, 40), width=2)
        dr.line([c - 6, r, c + 6, r], fill=(230, 40, 40))
        dr.line([c, r - 6, c, r + 6], fill=(230, 40, 40))
    for (c, r) in marks_det:
        c, r = c * scale, r * scale
        dr.rectangle([c - 7, r - 7, c + 7, r + 7], outline=(40, 210, 90), width=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def inspect_scene(missed: Path, *, half: int, all_lengths: bool) -> list[dict]:
    import numpy as np
    import rasterio
    from rasterio.windows import Window

    from angels.adapters.maritime.geolocate import Geolocator

    stem = missed.name[len("missed-"):]
    sar_doc = json.loads((EVENTS / f"sar-{stem}").read_text(encoding="utf-8"))
    miss_doc = json.loads(missed.read_text(encoding="utf-8"))
    scene = sar_doc.get("properties", {}).get("scene") or \
        miss_doc.get("properties", {}).get("scene")
    z = scene_zip(scene) if scene else None
    if z is None:
        print(f"  {stem}: scene zip not in {SAR}, skipped")
        return []

    targets = [f for f in miss_doc.get("features", [])
               if all_lengths or f["properties"].get("length_class") == ">=25 m"]
    if not targets:
        return []

    dets = [f["properties"]["pixel"] for f in sar_doc.get("features", [])
            if "pixel" in f["properties"]]

    rows = []
    uri = f"zip://{z.as_posix()}!/{vv_member(z)}"
    with rasterio.open(uri) as src:
        gcps, crs = src.get_gcps()
        loc = Geolocator(gcps, crs)
        print(f"\n  {scene[:62]}  ({len(targets)} vessel(s))")
        for f in targets:
            p = f["properties"]
            lon, lat = f["geometry"]["coordinates"]
            col, row = pixel_of(loc, lon, lat)
            c0, r0 = int(round(col)) - half, int(round(row)) - half
            win = Window(max(c0, 0), max(r0, 0), 2 * half, 2 * half)
            arr = src.read(1, window=win).astype("float64")
            oc, orow = col - win.col_off, row - win.row_off
            if arr.size == 0:
                continue

            yy, xx = np.indices(arr.shape)
            dist_px = np.hypot(xx - oc, yy - orow)
            near = dist_px <= NEAR_M / PIXEL_M
            med = float(np.median(arr[arr > 0])) if (arr > 0).any() else float("nan")
            near_peak = float(arr[near].max()) if near.any() else float("nan")
            iy, ix = np.unravel_index(int(np.argmax(arr)), arr.shape)
            chip_peak = float(arr[iy, ix])
            peak_off_m = float(dist_px[iy, ix] * PIXEL_M)

            local = [(dc - win.col_off, dr - win.row_off) for dc, dr in dets
                     if win.col_off <= dc < win.col_off + arr.shape[1]
                     and win.row_off <= dr < win.row_off + arr.shape[0]]
            nearest_det_m = min((np.hypot(dc - oc, dr - orow) * PIXEL_M
                                 for dc, dr in local), default=float("nan"))

            name = (p.get("name") or "").strip() or p.get("mmsi")
            out = OUT / stem.replace(".geojson", "") / f"{p.get('mmsi')}.png"
            chip_png(arr, out, [(oc, orow)], local)

            verdict = classify(near_peak / med if med else float("nan"),
                               chip_peak / med if med else float("nan"),
                               peak_off_m)
            rows.append({
                "scene": scene, "mmsi": p.get("mmsi"), "name": name,
                "length_m": p.get("length_m"),
                "length_class": p.get("length_class"),
                "fix_uncertainty_m": p.get("fix_uncertainty_m"),
                "lon": round(lon, 5), "lat": round(lat, 5),
                "col": round(col, 1), "row": round(row, 1),
                "background_dn": round(med, 1),
                "near_peak_x_bg": round(near_peak / med, 1) if med else "",
                "chip_peak_x_bg": round(chip_peak / med, 1) if med else "",
                "chip_peak_offset_m": round(peak_off_m),
                "nearest_detection_m": ("" if nearest_det_m != nearest_det_m
                                        else round(nearest_det_m)),
                "verdict": verdict,
                "png": str(out.relative_to(OUT)),
            })
            print(f"    {str(name)[:24]:24} {p.get('length_m') or '?':>5} m  "
                  f"near x{near_peak / med:5.1f}  chip x{chip_peak / med:5.1f} "
                  f"at {peak_off_m:5.0f} m  -> {verdict}")
    return rows


def classify(near_x: float, chip_x: float, peak_off_m: float) -> str:
    """A first reading of the chip. The picture decides; this only sorts.

    Thresholds are deliberately round and generous: this separates the
    obvious cases so the picture can be spent on the unclear ones.
    """
    if near_x != near_x:
        return "no data"
    if near_x >= 8:
        return "bright at AIS position -- detector"
    if chip_x >= 8 and peak_off_m > NEAR_M:
        return "bright elsewhere -- position/timing"
    return "nothing bright -- not there?"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--half", type=int, default=150,
                    help="chip half-width in pixels (150 = 3 km chip)")
    ap.add_argument("--all-lengths", action="store_true",
                    help="include vessels under 25 m and of unknown length")
    args = ap.parse_args()

    files = sorted(EVENTS.glob("missed-*.geojson"))
    if not files:
        print(f"\n  No missed-*.geojson in {EVENTS}. Run match_maritime.py first.\n")
        return 1

    rows: list[dict] = []
    for m in files:
        try:
            rows += inspect_scene(m, half=args.half, all_lengths=args.all_lengths)
        except Exception as exc:                      # noqa: BLE001
            print(f"  {m.name}: FAILED -- {type(exc).__name__}: {exc}")

    if not rows:
        print("\n  Nothing to inspect.\n")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "misses.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    tally: dict[str, int] = {}
    for r in rows:
        tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1
    print("\n  first reading:")
    for k, n in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"    {n:3}  {k}")
    print(f"\n  wrote {len(rows)} chip(s) and misses.csv to {OUT}")
    print("  Open the PNGs: red ring = AIS position (300 m), green box = detection.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
