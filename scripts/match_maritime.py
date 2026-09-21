"""Pair SAR detections against AIS at the acquisition instant. THE RESULT.

    python scripts/match_maritime.py                  # newest detection file
    python scripts/match_maritime.py --all
    python scripts/match_maritime.py --k 4 -v

Reads data/events/sar-*.geojson (from detect_ships.py) and the clipped AIS
store (from clip_ais.py), and writes data/events/dark-<scene>.geojson.

WHAT COMES OUT, AND HOW TO READ IT

Two numbers, and neither means anything alone.

    detection rate    of the vessels that reported themselves here and then,
                      how many did the radar find?
    unmatched         detections no report explains

A run with 40 unmatched detections and a 90% detection rate is a finding. The
same 40 with a 30% detection rate is a description of the detector's blind
spots written in the language of evidence. So the summary prints them
together, always, and the confidence on every event is already scaled by the
rate -- a bad pass produces weak claims by construction rather than by
remembering to caveat them.

MISSES ARE WRITTEN OUT TOO

The reported-but-not-observed side goes to data/events/missed-<scene>.geojson.
It is not a by-product: it is the evidence that the detector works, and the
only way to say whether a dark-vessel count is a measurement. It also shows
WHICH vessels are missed -- if the misses are all under 40 m, the finding is
about large vessels and must say so.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.adapters.maritime.ais import AISReadError, tracks_at
from angels.adapters.maritime.searched import SearchedArea
from angels.config import AIS_FRONTIER, AOI_SEA, EVENTS, RAW
from angels.core.detectors import calibration, matching
from angels.core.models import Observation, Position

AIS_STORE = RAW / "maritime"


def read_detections(path: Path) -> tuple[list[Observation], dict]:
    """Detections back out of the GeoJSON detect_ships.py wrote."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    meta = doc.get("properties", {})
    obs: list[Observation] = []
    for f in doc.get("features", []):
        lon, lat = f["geometry"]["coordinates"]
        p = dict(f["properties"])
        obs.append(Observation(
            position=Position(
                lat=lat, lon=lon,
                t=datetime.fromisoformat(p["t"]),
                uncertainty_m=float(p.get("uncertainty_m", 50.0)),
            ),
            sensor=p.get("sensor", "sentinel1-vv-cfar"),
            attributes=p,
        ))
    return obs, meta


def scene_date(path: Path) -> date | None:
    """The acquisition date of a detection file, without parsing the geometry.

    Reads only the first feature's timestamp. These files run to thousands of
    features and this is called on every candidate before one is chosen.
    """
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        feats = doc.get("features") or []
        if not feats:
            return None
        return datetime.fromisoformat(feats[0]["properties"]["t"]).date()
    except (OSError, ValueError, KeyError, TypeError):
        return None


def is_matchable(path: Path) -> bool:
    """Is there published AIS for this scene's date?

    Unknown dates count as matchable: a file this cannot parse should reach
    run_one and fail there with a real message, not be silently dropped from
    the candidate list for a reason nobody sees.
    """
    d = scene_date(path)
    return d is None or d <= date.fromisoformat(AIS_FRONTIER)


def inside(lon: float, lat: float, poly) -> bool:
    """Ray casting. No dependency, and the polygon is 48 points."""
    n = len(poly)
    hit = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > lat) != (yj > lat) and \
                lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-30) + xi:
            hit = not hit
        j = i
    return hit


def in_footprint(tracks, t, poly):
    """Tracks whose position at t lies inside the scene the radar actually saw.

    THE DENOMINATOR, AND IT WAS WRONG.

    The first real run scored every slice against every vessel in AOI_SEA --
    6.2 degrees of longitude by 3.6 of latitude. All four slices reported the
    same ~1,227 tracks, which is impossible: slice 1 covers offshore Virginia
    at 36N and slice 4 covers Pennsylvania at 40N. A slice was being charged
    with missing vessels 300 km outside its own swath, and the detection rate
    collapsed for reasons that had nothing to do with the detector.

    "Of the vessels that reported themselves HERE and THEN" only means
    something if HERE is the scene.

    STILL NOT ENOUGH -- see split_by_searched() below. The footprint is the
    ground the raster covers, not the water the detector examined, and on a
    coastal scene almost every vessel sits in the difference.
    """
    keep = []
    for tr in tracks:
        fix = tr.position_at(t)
        if fix is not None and inside(fix.lon, fix.lat, poly):
            keep.append(tr)
    return keep


def split_by_searched(tracks, t, area):
    """Split footprint tracks into searched, marginal and unsearched.

    THE DENOMINATOR, AND IT WAS WRONG A SECOND TIME.

    Restricting to the footprint took slice 2 of the 2024-09-25 pass from
    1,227 tracks to 314 and left the detection rate at 2%. Of those 314, 286
    had no detection anywhere within 5 km, and they were not scattered: they
    were a block at Norfolk International Terminals, at Lambert's Point and in
    the York River anchorage, ending at a hard edge where the detection field
    began. That edge is the land mask and the 605 m coastal blind zone. The
    radar never looked there, and 32 vessels over 200 m -- container ships and
    bulk carriers, the least missable objects in the scene -- were being
    counted as misses because they were tied up at a pier the detector had
    masked out.

    Three buckets, not two, because a 1.1 km cell on a shoreline is genuinely
    part searched and rounding it either way biases the headline number:

        searched    at or above the stated fraction. The denominator.
        marginal    partly searched. Reported, never silently assigned.
        unsearched  the mask removed it. Not evidence of anything.

    Returns (searched, marginal, unsearched). A track with no usable fix at t
    is not placed anywhere -- it cannot be located, so it cannot be said to
    have been searched for, and matching.py already excludes it from the rate.
    """
    from angels.adapters.maritime.searched import SEARCHED

    seen, margin, unseen = [], [], []
    for tr in tracks:
        fix = tr.position_at(t)
        if fix is None:
            continue
        frac = area.fraction(fix.lon, fix.lat)
        if frac >= SEARCHED:
            seen.append(tr)
        elif frac > 0:
            margin.append(tr)
        else:
            unseen.append(tr)
    return seen, margin, unseen


def summarise_misses(tracks, result) -> dict:
    """What the sensor failed to see, by size. The calibration's calibration.

    A detection rate is one number over a whole scene; it hides the thing that
    matters most. If every miss is a 15 m fishing boat, the pipeline is sound
    and its findings are about vessels above some size -- which the write-up
    must then state. If 300 m ships are being missed, something is wrong
    upstream and no dark-vessel count is safe.
    """
    return bucket_lengths(tracks[j] for j in result.unmatched_tracks)


def bucket_lengths(tracks) -> dict:
    """Vessel counts by length class.

    Shared by the miss summary and the unsearched summary so that the two
    cannot drift into different class edges and be compared anyway. The
    unsearched breakdown is the one that showed what was wrong: 32 vessels
    over 200 m are not a detector's blind spot, they are a denominator
    counting ships the detector was never shown.
    """
    buckets = {"<25 m": 0, "25-50 m": 0, "50-100 m": 0,
               "100-200 m": 0, ">200 m": 0, "unknown": 0}
    for tr in tracks:
        length = matching.vessel_length_m(tr)
        if length == matching.DEFAULT_VESSEL_LENGTH_M:
            buckets["unknown"] += 1
        elif length < 25:
            buckets["<25 m"] += 1
        elif length < 50:
            buckets["25-50 m"] += 1
        elif length < 100:
            buckets["50-100 m"] += 1
        elif length < 200:
            buckets["100-200 m"] += 1
        else:
            buckets[">200 m"] += 1
    return buckets


def run_one(path: Path, *, k: float, window_s: float, verbose: bool,
            min_snr: float = 0.0, min_pixels: int = 0
            ) -> tuple[int, "calibration.Calibration | None"]:
    """Match one scene. Returns (exit code, the scene's calibration or None
    when the scene could not be scored).

    min_snr / min_pixels gate the detections BEFORE matching, so the rate
    and the unmatched count are both computed on the gated set. Choose them
    with scripts/tune_detector.py, never by eye on the pass being reported.
    """
    obs, meta = read_detections(path)
    n_raw = len(obs)
    if min_snr or min_pixels:
        obs = [o for o in obs if o.attributes.get("snr", 0) >= min_snr
               and o.attributes.get("pixels", 0) >= min_pixels]
    if not obs:
        print(f"\n  {path.name}: no detections in this file"
              + (" after the gate" if n_raw else "") + "\n")
        return 0, None

    t = obs[0].position.t
    print(f"\n  {meta.get('scene', path.stem)[:58]}")
    print(f"  acquired     {t:%Y-%m-%d %H:%M:%S} UTC")
    print(f"  detections   {len(obs)}"
          + (f" of {n_raw} (gate: SNR >= {min_snr:g}, pixels >= "
             f"{min_pixels})" if len(obs) != n_raw or min_snr or min_pixels
             else ""))
    if "searched_km2" in meta:
        print(f"  searched     {meta['searched_km2']:,} km2 of water")

    # Checked BEFORE the store is opened, because the answer is already known
    # and the alternative message is wrong. A scene past the frontier finds no
    # AIS, and "no AIS at this instant -- check the date" then blames the data
    # for a fact about the calendar. The script knows which it is; it should
    # say which it is.
    frontier = date.fromisoformat(AIS_FRONTIER)
    if t.date() > frontier:
        late = (t.date() - frontier).days
        print(f"\n  NOT MATCHABLE. This scene is {late} days past the AIS")
        print(f"  frontier of {AIS_FRONTIER}, so no report for it exists to")
        print("  compare against -- not here, and not anywhere yet.")
        print("  The detections are real and keep their value; they are the")
        print("  forward half of the study.")
        print("\n  Run detect_ships.py on a scene from the observation window")
        print("  instead:  python scripts/detect_ships.py --path <2024 scene>\n")
        return 3, None

    try:
        tracks = tracks_at(AIS_STORE, t, window_s=window_s, bbox=AOI_SEA)
    except AISReadError as exc:
        print(f"\n  AIS unavailable: {exc}")
        print("\n  Not a sea with no vessels in it -- a store that could not")
        print("  be read. Download the national day for this date and run")
        print("  scripts/clip_ais.py before drawing any conclusion.\n")
        return 2, None

    poly = meta.get("footprint")
    marginal: list = []
    unsearched: list = []
    if poly:
        before = len(tracks)
        tracks = in_footprint(tracks, t, poly)
        print(f"  AIS tracks   {len(tracks)} inside this scene's footprint "
              f"({before} in AOI_SEA within {window_s / 60:.0f} min)")

        area = SearchedArea.from_json(meta.get("searched_grid"))
        if area is not None:
            tracks, marginal, unsearched = split_by_searched(tracks, t, area)
            print(f"  searched     {len(tracks)} of those were in water this "
                  f"scene actually examined")
            if marginal:
                print(f"               {len(marginal)} in part-searched cells "
                      f"on a shoreline -- excluded, not assigned")
            if unsearched:
                print(f"               {len(unsearched)} in water the land "
                      f"mask or the {meta.get('coast_blind_m', 0):.0f} m "
                      f"coastal blind zone removed")
                print("               (mostly berthed and anchored vessels; "
                      "the radar never looked there)")
        else:
            print("  NO SEARCHED GRID in this detection file -- it predates "
                  "the fix, so the")
            print("  denominator is the whole footprint including the land "
                  "mask and the")
            print("  coastal blind zone. On a coastal scene that is most of "
                  "the vessels,")
            print("  and the detection rate below is far too low. Re-run "
                  "detect_ships.py.")
    else:
        print(f"  AIS tracks   {len(tracks)} within {window_s / 60:.0f} min "
              f"either side")
        print("  NO FOOTPRINT in this detection file -- it predates the fix, "
              "so the")
        print("  denominator is the whole study area and the detection rate "
              "below is")
        print("  far too low. Re-run detect_ships.py on this scene.")
    if not tracks:
        if marginal or unsearched:
            # A different statement from an empty store, and the difference
            # is the whole point: the vessels exist and reported themselves,
            # they are simply all in water this scene did not examine. There
            # is no denominator here, so there is no detection rate and no
            # dark vessel -- only an unverifiable pass.
            print(f"\n  Every one of the {len(marginal) + len(unsearched)} "
                  f"vessels in this footprint was")
            print("  outside the searched water. This pass supports no "
                  "detection rate and")
            print("  no dark-vessel claim: nothing here could have been "
                  "matched either way.\n")
            return 1, None
        print("\n  No AIS at this instant. Every detection would be")
        print("  'unmatched', which would be an artefact of an empty store")
        print("  rather than a sea full of dark vessels. Fetch and clip the")
        print(f"  AIS for {t:%Y-%m-%d}:")
        print(f"    python scripts/fetch_ais.py --date {t:%Y-%m-%d} --clip\n")
        return 1, None

    result = matching.associate(obs, tracks, t, k=k)
    events = matching.unmatched(obs, tracks, t, k=k)

    print()
    print(f"  {result}")
    print(f"  {result.n_tracks_without_fix} track(s) had no usable fix "
          f"(not counted as misses)")

    rate = result.detection_rate
    cal = calibration.calibrate(result, tracks, obs, t,
                                searched_km2=meta.get("searched_km2"), k=k)
    print_calibration(cal)

    misses = summarise_misses(tracks, result)
    if any(misses.values()):
        print("\n  missed reports by length:")
        for label, n in misses.items():
            if n:
                print(f"    {label:>10}  {n}")

    if verbose and result.pairs:
        print(f"\n    {'mmsi':>10}{'sep m':>8}{'slack':>8}  vessel")
        for c in sorted(result.pairs, key=lambda c: -c.slack)[:10]:
            tr = tracks[c.track_index]
            name = getattr(tr.reports[0], "name", None) or ""
            flag = "  (not scored)" if c.track_index in cal.unscorable else ""
            print(f"    {tr.platform_id:>10}{c.distance_m:8.0f}"
                  f"{c.slack:8.2f}  {name[:28]}{flag}")

    EVENTS.mkdir(parents=True, exist_ok=True)
    stem = path.stem.replace("sar-", "")

    dark = EVENTS / f"dark-{stem}.geojson"
    dark.write_text(json.dumps({
        "type": "FeatureCollection",
        "properties": {
            **meta,
            "detection_rate": None if rate != rate else round(rate, 4),
            "n_detections": result.n_observations,
            "n_reports": result.n_tracks,
            "n_reports_with_fix": result.n_tracks - result.n_tracks_without_fix,
            "n_matched": len(result.pairs),
            # Everything the footprint contained that the scene did not
            # search. Carried into the product because a reader has to be
            # able to tell a detection rate computed over 20 vessels from one
            # computed over 306, and because the size breakdown is what makes
            # a coastal scene's low rate legible rather than alarming.
            "n_reports_unsearched": len(unsearched),
            "n_reports_marginal": len(marginal),
            "unsearched_by_length": bucket_lengths(unsearched),
            "k_sigma_match": k,
            "gate_min_snr": min_snr,
            "gate_min_pixels": min_pixels,
            "n_detections_before_gate": n_raw,
            "ais_window_s": window_s,
            # The rate as it has to be read: by length class, with the
            # tracks too loosely located to score set aside. See
            # angels/core/detectors/calibration.py.
            "calibration": cal.to_json(),
        },
        "features": [e.to_geojson() for e in events],
    }, indent=1), encoding="utf-8")

    missed = EVENTS / f"missed-{stem}.geojson"
    missed.write_text(json.dumps({
        "type": "FeatureCollection",
        "properties": {"scene": meta.get("scene"), "by_length": misses},
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point",
                         "coordinates": [fix.lon, fix.lat]},
            "properties": {
                "mmsi": tracks[j].platform_id,
                "name": getattr(tracks[j].reports[0], "name", None),
                "length_m": matching.vessel_length_m(tracks[j]),
                "fix_uncertainty_m": round(fix.uncertainty_m, 1),
                "reports_in_track": len(tracks[j]),
                "length_class": calibration.length_class(tracks[j]),
                "scored": j not in cal.unscorable,
            },
        } for j in result.unmatched_tracks
            if (fix := tracks[j].position_at(t)) is not None],
    }, indent=1), encoding="utf-8")

    print(f"\n  wrote {dark.name}")
    print(f"  wrote {missed.name}")
    return 0, cal


def print_calibration(cal, *, pooled: bool = False) -> None:
    """The detection rate by length class, and the warning that goes with it.

    The warning is judged on vessels of at least DETECTABLE_LENGTH_M -- the
    ones Sentinel-1 can be expected to see -- and only once there are enough
    of them for the number to mean something.
    """
    L = calibration.DETECTABLE_LENGTH_M
    print()
    print("  detection rate by reported length" + (" (all passes pooled)"
                                                   if pooled else ""))
    for line in calibration.format_table(cal, indent="    "):
        print(line)
    if cal.n_unscorable:
        print(f"    not scored   {cal.n_unscorable} located too loosely for a "
              f"match to mean anything")
        if cal.max_scorable_radius_m:
            print(f"                 (radius over "
                  f"{cal.max_scorable_radius_m:,.0f} m at this detection "
                  f"density; {cal.n_unscorable_matched} of them 'matched')")
        else:
            print(f"                 ({cal.n_unscorable_matched} of them "
                  f"'matched')")
    if cal.expected_chance_matches >= 0.05:
        print(f"    about {cal.expected_chance_matches:.1f} of the scored "
              f"matches could be chance coincidences")

    head = cal.headline
    lo, hi = head.interval
    if head.scored < 10:
        print(f"\n  Only {head.scored} vessel(s) of {L:.0f} m or more were "
              f"scored{' in total' if pooled else ' on this scene'} -- too few")
        print("  to say how the detector does on the ships it should see."
              + ("" if pooled else " Pool more passes."))
    elif hi < 0.6:
        print(f"\n  WARNING: even for vessels of {L:.0f} m or more the radar "
              f"found {100 * head.rate:.0f}%")
        print(f"  (95% range {100 * lo:.0f}-{100 * hi:.0f}%). Below about 60% "
              f"an unmatched detection says")
        print("  more about the detector than about the sea. Fix detection "
              "before")
        print("  reporting dark vessels.")
    elif lo < 0.6:
        print(f"\n  For vessels of {L:.0f} m or more the radar found "
              f"{100 * head.rate:.0f}% (95% range "
              f"{100 * lo:.0f}-{100 * hi:.0f}%) --")
        print("  not yet distinguishable from the 60% line. More passes "
              "will say which side it is on.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--path", type=Path)
    ap.add_argument("--k", type=float, default=matching.K_SIGMA,
                    help="match radius in sigmas (3.0)")
    ap.add_argument("--window", type=float, default=1800.0,
                    help="seconds of AIS either side of the acquisition")
    ap.add_argument("--min-snr", type=float, default=0.0,
                    help="drop detections below this SNR before matching "
                         "(choose with tune_detector.py)")
    ap.add_argument("--min-pixels", type=int, default=0,
                    help="drop detections smaller than this many pixels")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.path:
        # PowerShell hands wildcards through literally; expand them here so
        # --path behaves the same on both platforms.
        raw = str(args.path)
        if any(ch in raw for ch in "*?["):
            q = Path(raw)
            files = sorted(q.parent.glob(q.name))
            if not files:
                print(f"\n  Nothing matches {raw}\n")
                return 1
        else:
            files = [args.path]
    else:
        files = sorted(EVENTS.glob("sar-*.geojson"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            print(f"\n  No detection files in {EVENTS}")
            print("  Run: python scripts/detect_ships.py\n")
            return 1

        # Prefer a scene that CAN be matched. The newest detection file is
        # the obvious default and the wrong one: the newest scenes are past
        # the AIS frontier by construction, so defaulting to recency picks
        # the one file guaranteed to produce nothing.
        matchable = [p for p in files if is_matchable(p)]
        if not args.all:
            if matchable:
                skipped = files.index(matchable[0])
                if skipped:
                    print(f"\n  skipping {skipped} newer scene(s) past the AIS"
                          f" frontier of {AIS_FRONTIER}")
                files = matchable[:1]
            else:
                print(f"\n  {len(files)} detection file(s), none of them from")
                print(f"  on or before {AIS_FRONTIER}, so none can be matched:")
                for p in files:
                    print(f"    {scene_date(p) or '?'}  {p.name[:52]}")
                print("\n  Detect on a scene inside the observation window:")
                print("    python scripts/fetch_sar.py --list")
                print("    python scripts/detect_ships.py --path <2024 scene>\n")
                return 1

    worst = 0
    cals = []
    for f in files:
        code, cal = run_one(f, k=args.k, window_s=args.window,
                            verbose=args.verbose, min_snr=args.min_snr,
                            min_pixels=args.min_pixels)
        worst = max(worst, code)
        if cal is not None:
            cals.append(cal)

    if len(cals) > 1:
        pooled = cals[0]
        for c in cals[1:]:
            pooled = pooled + c
        print(f"\n  ===== {len(cals)} scenes pooled =====")
        print_calibration(pooled, pooled=True)

    print("\n  Read the two numbers together. Unmatched detections are a")
    print("  finding only in proportion to how much of the reported traffic")
    print("  the sensor actually found.\n")
    return worst


if __name__ == "__main__":
    sys.exit(main())
