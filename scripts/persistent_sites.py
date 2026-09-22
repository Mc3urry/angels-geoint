"""Separate the furniture from the traffic, and write the candidate list.

    python scripts/persistent_sites.py
    python scripts/persistent_sites.py --min-snr 15 --min-pixels 6
    python scripts/persistent_sites.py --min-dates 4 --radius 400

Reads every data/events/sar-*.geojson (the detections) and its
dark-*.geojson (the ones no AIS report explained), clusters detections that
recur in the same place across passes, and writes

    data/events/persistent-sites.geojson   every site, with its verdict
    data/events/candidates.geojson         unmatched detections that are NOT
                                           at a fixed site -- the dark-vessel
                                           candidate list

WHY THE CANDIDATE LIST IS NOT THE UNMATCHED LIST

Wind turbines, platforms, met masts, bridge-tunnel islands and lighthouses
are bright, permanent, and never carry AIS. Every pass reports them as
unmatched. On the first two passes, 23 of 76 gated candidates in June had a
twin within 300 m in September -- and that was with two passes. They cluster
at fixed points, which is the same shape as the finding this project is
looking for, so leaving them in would not merely inflate the count: it would
manufacture the result.

The rule, the denominator and its limits are in
angels/core/detectors/persistence.py. In one line: seen on at least
--min-dates dates, on at least half the passes that actually searched the
spot, and never once explained by AIS.

THE GATE

Defaults to the gate chosen by scripts/tune_detector.py (SNR >= 15, >= 6
pixels). Run it with the same gate the matching used, or the candidate list
will not be the one the detection rate applies to.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.adapters.maritime.searched import SEARCHED, SearchedArea
from angels.config import EVENTS
from angels.core.detectors import persistence

KEY_DP = 5          # ~1 m; enough to pair a detection with its own event


def key(lon: float, lat: float) -> tuple[float, float]:
    return (round(lon, KEY_DP), round(lat, KEY_DP))


def gated(features, min_snr: float, min_pixels: int):
    for f in features:
        p = f["properties"]
        if p.get("snr", 0) >= min_snr and p.get("pixels", 0) >= min_pixels:
            yield f


def load_scene(path: Path, min_snr: float, min_pixels: int):
    """(date, detections, unmatched keys, searched area) for one scene."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    meta = doc.get("properties", {})
    feats = list(gated(doc.get("features", []), min_snr, min_pixels))
    if not feats:
        return None

    date = feats[0]["properties"]["t"][:10]
    dark = EVENTS / f"dark-{path.name[len('sar-'):]}"
    unmatched: set[tuple[float, float]] = set()
    if dark.exists():
        for f in json.loads(dark.read_text(encoding="utf-8")).get("features", []):
            lon, lat = f["geometry"]["coordinates"]
            unmatched.add(key(lon, lat))
    else:
        # No dark file means this scene was never matched. Its detections
        # cannot be called matched OR unmatched: they contribute to where
        # sites are, and to nothing else.
        unmatched = None

    return date, feats, unmatched, SearchedArea.from_json(meta.get("searched_grid"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--min-snr", type=float, default=15.0)
    ap.add_argument("--min-pixels", type=int, default=6)
    ap.add_argument("--radius", type=float, default=persistence.RADIUS_M,
                    help="how close two detections must be to be one site (m)")
    ap.add_argument("--min-dates", type=int, default=persistence.MIN_DATES)
    ap.add_argument("--min-fraction", type=float,
                    default=persistence.MIN_FRACTION)
    args = ap.parse_args()
    rule = {"min_dates": args.min_dates, "min_fraction": args.min_fraction}

    files = sorted(EVENTS.glob("sar-*.geojson"))
    if not files:
        print(f"\n  No detection files in {EVENTS}\n")
        return 1

    index = persistence.SiteIndex(radius_m=args.radius)
    areas: dict[str, list[SearchedArea]] = defaultdict(list)
    per_date: dict[str, int] = defaultdict(int)
    unscored_scenes = 0
    total = 0

    for p in files:
        loaded = load_scene(p, args.min_snr, args.min_pixels)
        if loaded is None:
            continue
        date, feats, unmatched, area = loaded
        if unmatched is None:
            unscored_scenes += 1
        if area is not None:
            areas[date].append(area)
        for f in feats:
            lon, lat = f["geometry"]["coordinates"]
            prop = f["properties"]
            index.add(lon, lat, date,
                      matched=(unmatched is not None
                               and key(lon, lat) not in unmatched),
                      snr=float(prop.get("snr", 0.0)))
            per_date[date] += 1
            total += 1

    # The denominator: which dates searched each site. Done after every
    # detection is in, because a site has no position until then.
    for date, grids in areas.items():
        index.mark_searched(
            date,
            lambda lon, lat, g=grids: any(a.fraction(lon, lat) >= SEARCHED
                                          for a in g))

    sites = index.sites
    fixed = [s for s in sites if s.is_fixed(**rule)]
    traffic = [s for s in sites if s.ever_matched]

    print(f"\n  {total:,} gated detections over {len(per_date)} date(s), "
          f"{len(files)} scene(s)")
    if unscored_scenes:
        print(f"  {unscored_scenes} scene(s) had no dark- file, so their "
              f"detections locate sites but are never called unmatched")
    print(f"  {len(sites):,} sites at {args.radius:.0f} m")
    print(f"  {len(fixed)} FIXED (>= {args.min_dates} dates, >= "
          f"{100 * args.min_fraction:.0f}% of the passes that searched them, "
          f"never matched)")
    print(f"  {len(traffic):,} explained by AIS at least once")

    if fixed:
        print(f"\n  the fixed sites, busiest first")
        print(f"    {'lon':>9}{'lat':>9}{'dates':>7}{'/searched':>10}"
              f"{'max snr':>9}")
        for s in sorted(fixed, key=lambda s: -s.n_dates)[:25]:
            print(f"    {s.lon:9.4f}{s.lat:9.4f}{s.n_dates:7d}"
                  f"{s.n_searched:10d}{s.max_snr:9.0f}")
        if len(fixed) > 25:
            print(f"    ... and {len(fixed) - 25} more")

    # -- the candidate list --------------------------------------------------

    kept, dropped = [], 0
    for p in files:
        loaded = load_scene(p, args.min_snr, args.min_pixels)
        if loaded is None:
            continue
        date, feats, unmatched, _ = loaded
        if unmatched is None:
            continue
        scene = json.loads(p.read_text(encoding="utf-8")).get("properties", {})
        for f in feats:
            lon, lat = f["geometry"]["coordinates"]
            if key(lon, lat) not in unmatched:
                continue
            if index.is_near_fixed(lon, lat, **rule):
                dropped += 1
                continue
            out = dict(f)
            out["properties"] = {**f["properties"], "date": date,
                                 "scene": scene.get("scene")}
            kept.append(out)

    EVENTS.mkdir(parents=True, exist_ok=True)
    (EVENTS / "persistent-sites.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "properties": {
            "radius_m": args.radius, "min_dates": args.min_dates,
            "min_fraction": args.min_fraction,
            "gate_min_snr": args.min_snr, "gate_min_pixels": args.min_pixels,
            "n_sites": len(sites), "n_fixed": len(fixed),
            "dates": sorted(per_date),
        },
        "features": [s.to_geojson(**rule) for s in sites
                     if s.n_dates >= 2 or s.is_fixed(**rule)],
    }, indent=1), encoding="utf-8")

    (EVENTS / "candidates.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "properties": {
            "what": ("gated detections no AIS report explained, with "
                     "detections at fixed structures removed"),
            "gate_min_snr": args.min_snr, "gate_min_pixels": args.min_pixels,
            "persistence": {"radius_m": args.radius, **rule},
            "n_candidates": len(kept), "n_dropped_as_fixed": dropped,
            "dates": sorted(per_date),
            "caveat": ("NOT dark vessels yet: AIS reception offshore has not "
                       "been measured. See coverage."),
        },
        "features": kept,
    }, indent=1), encoding="utf-8")

    print(f"\n  unmatched detections at fixed sites   {dropped:,}  removed")
    print(f"  candidates                            {len(kept):,}")
    print(f"\n  wrote persistent-sites.geojson and candidates.geojson to "
          f"{EVENTS}")
    print("  These are CANDIDATES, not dark vessels: nothing here yet says "
          "AIS\n  could have been heard at that distance offshore.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
