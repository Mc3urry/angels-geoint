"""Look at the radar where a dark-vessel candidate is, and sort the chips.

    python scripts/inspect_candidates.py                  # 20, spread about
    python scripts/inspect_candidates.py --where inshore --n 20
    python scripts/inspect_candidates.py --where offshore --n 20
    python scripts/inspect_candidates.py --all --min-snr 30

Writes data/interim/candidates/<scene>/<id>.png and candidates.csv.

WHY THIS IS THE LAST EVIDENCE STEP

The headline finding is that unexplained returns outnumber AIS-explained
ones about five to one inside the bays and under two to one offshore. Two
readings fit that:

    small craft        vessels under the AIS carriage requirement, which is
                       a real statement about non-cooperative presence
    inshore clutter    wind streaks, wakes, shoals, fish weirs, crab pots,
                       and structures the persistence filter has not caught

The numbers lean towards the first -- inshore candidates are smaller and
weaker (median apparent length 35 m against 50 m, median SNR 20 against
25) -- but inshore clutter is also small and weak, so the numbers cannot
settle it. A person looking at forty chips can.

The sampling is deliberately BLIND to the answer: candidates are drawn at
even spacing through the list after sorting by position, not by SNR, so the
sample is not the brightest ones. Use --min-snr only to answer a separate
question about the strong tail.
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

sys.path.insert(0, str(Path(__file__).parent))
from inspect_misses import chip_png, pixel_of, vv_member  # noqa: E402

from angels.config import EVENTS, INTERIM, RAW  # noqa: E402

SAR = RAW / "sar"
OUT = INTERIM / "candidates"
PIXEL_M = 10.0
NEAR_M = 300.0

# Inshore means landward of the 12 nm territorial sea by more than this.
# Same split the boundary analysis reports, so the chips answer the question
# the table raised rather than a neighbouring one.
INSHORE_NM = 10.0


def scene_zip(scene: str) -> Path | None:
    hits = sorted(SAR.glob(f"{scene}*.zip"))
    return hits[0] if hits else None


def classify(near_x: float, chip_x: float, peak_off_m: float) -> str:
    """A first reading, to be overruled by the picture."""
    if near_x != near_x:
        return "no data"
    if near_x >= 8:
        return "bright target at the candidate"
    if chip_x >= 8 and peak_off_m > NEAR_M:
        return "bright elsewhere in the chip"
    return "nothing bright -- clutter?"


def load_candidates(where: str, min_snr: float):
    path = EVENTS / "candidates-scored.geojson"
    if not path.exists():
        path = EVENTS / "candidates.geojson"
    if not path.exists():
        raise SystemExit(f"\n  No candidate file in {EVENTS}. Run "
                         f"persistent_sites.py and ais_coverage.py first.\n")
    doc = json.loads(path.read_text(encoding="utf-8"))
    feats = [f for f in doc.get("features", [])
             if f["properties"].get("snr", 0) >= min_snr]

    if where != "all":
        from angels.adapters.maritime.limits import LineSet
        sys.path.insert(0, str(Path(__file__).parent))
        import boundary_analysis as ba

        sets = ba.limit_sets(bbox=ba.AOI_SEA)
        ts, cz = sets.get(ba.TS_NAME), sets.get(ba.CZ_NAME)
        if ts is None or cz is None:
            raise SystemExit("\n  Need the limit lines to split inshore from "
                             "offshore. Run scripts/fetch_limits.py.\n")
        keep = []
        for f in feats:
            lon, lat = f["geometry"]["coordinates"]
            band = ba.signed_band(ts.distance_m(lon, lat),
                                  cz.distance_m(lon, lat),
                                  outer_companion=True)
            inshore = band.startswith("landward") or band.startswith("-25")
            if (where == "inshore") == inshore:
                keep.append(f)
        feats = keep
    return feats, path.name


def sample(feats, n: int):
    """Evenly spaced through the list, sorted by position.

    NOT the brightest n: a sample chosen by strength answers "what do the
    strong ones look like", which is a different question and a flattering
    one.
    """
    feats = sorted(feats, key=lambda f: (round(f["geometry"]["coordinates"][1], 3),
                                         round(f["geometry"]["coordinates"][0], 3)))
    if n >= len(feats):
        return feats
    step = len(feats) / n
    return [feats[int(i * step)] for i in range(n)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--where", choices=["inshore", "offshore", "all"],
                    default="all")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--all", action="store_true", help="every candidate")
    ap.add_argument("--min-snr", type=float, default=0.0)
    ap.add_argument("--half", type=int, default=150,
                    help="chip half-width in pixels (150 = 3 km)")
    args = ap.parse_args()

    import numpy as np
    import rasterio
    from rasterio.windows import Window

    from angels.adapters.maritime.geolocate import Geolocator

    feats, source = load_candidates(args.where, args.min_snr)
    if not feats:
        print(f"\n  No candidates matched ({args.where}, SNR >= "
              f"{args.min_snr:g}).\n")
        return 1
    chosen = feats if args.all else sample(feats, args.n)
    print(f"\n  {len(feats):,} candidates in {source} ({args.where})")
    print(f"  chipping {len(chosen)}, evenly spaced by position\n")

    by_scene: dict[str, list] = {}
    for f in chosen:
        by_scene.setdefault(f["properties"].get("scene", ""), []).append(f)

    rows = []
    for scene, group in sorted(by_scene.items()):
        z = scene_zip(scene) if scene else None
        if z is None:
            print(f"  {scene[:48]}: scene zip not on disk, skipped")
            continue
        with rasterio.open(f"zip://{z.as_posix()}!/{vv_member(z)}") as src:
            gcps, crs = src.get_gcps()
            loc = Geolocator(gcps, crs)
            print(f"  {scene[17:32]}  ({len(group)} candidate(s))")
            for f in group:
                p = f["properties"]
                lon, lat = f["geometry"]["coordinates"]
                col, row = pixel_of(loc, lon, lat)
                win = Window(max(int(col) - args.half, 0),
                             max(int(row) - args.half, 0),
                             2 * args.half, 2 * args.half)
                arr = src.read(1, window=win).astype("float64")
                if arr.size == 0:
                    continue
                oc, orow = col - win.col_off, row - win.row_off
                yy, xx = np.indices(arr.shape)
                dist = np.hypot(xx - oc, yy - orow)
                near = dist <= NEAR_M / PIXEL_M
                med = float(np.median(arr[arr > 0])) if (arr > 0).any() else float("nan")
                near_peak = float(arr[near].max()) if near.any() else float("nan")
                iy, ix = np.unravel_index(int(np.argmax(arr)), arr.shape)
                chip_peak, off_m = float(arr[iy, ix]), float(dist[iy, ix] * PIXEL_M)

                name = f"{p.get('date', 'x')}_{lat:.4f}_{lon:.4f}".replace(".", "p")
                out = OUT / scene[17:32] / f"{name}.png"
                chip_png(arr, out, [(oc, orow)], [])
                verdict = classify(near_peak / med if med else float("nan"),
                                   chip_peak / med if med else float("nan"),
                                   off_m)
                rows.append({
                    "date": p.get("date"), "lon": round(lon, 5),
                    "lat": round(lat, 5), "snr": p.get("snr"),
                    "pixels": p.get("pixels"),
                    "length_m_approx": p.get("length_m_approx"),
                    "reception": p.get("reception"),
                    "background_dn": round(med, 1),
                    "near_peak_x_bg": round(near_peak / med, 1) if med else "",
                    "chip_peak_x_bg": round(chip_peak / med, 1) if med else "",
                    "chip_peak_offset_m": round(off_m),
                    "verdict": verdict,
                    "png": str(out.relative_to(OUT)),
                })
                print(f"    {p.get('date')}  {lat:7.4f} {lon:9.4f}  "
                      f"snr {p.get('snr'):6.1f}  len~ {p.get('length_m_approx'):4.0f} m"
                      f"  near x{near_peak / med:5.1f}  -> {verdict}")

    if not rows:
        print("\n  Nothing chipped.\n")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "candidates.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    tally: dict[str, int] = {}
    for r in rows:
        tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1
    print("\n  first reading (the pictures decide):")
    for k, n in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"    {n:3}  {k}")
    print(f"\n  wrote {len(rows)} chip(s) and candidates.csv to {OUT}")
    print("  Red ring = the candidate, 300 m. Look for a hull; a wind streak "
          "is diffuse\n  and a wake has no bright head.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
