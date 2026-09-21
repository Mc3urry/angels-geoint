"""Detect vessels in a Sentinel-1 scene. The observation channel, finally.

    python scripts/detect_ships.py                       # newest scene
    python scripts/detect_ships.py --all
    python scripts/detect_ships.py --k 5 --max-tiles 4   # a quick look
    python scripts/detect_ships.py --preview

Writes data/events/sar-<scene>.geojson -- one feature per detection, with the
evidence needed to argue with it later: SNR, cluster size, local clutter, and
the pixel it came from.

WHAT THIS PRODUCES, AND WHY IT IS THE POINT

Every detector in this project so far compares reports against themselves.
This one produces OBSERVATIONS: positions of vessels that were sensed, not
self-declared. A ship with its AIS transponder off is exactly as bright here
as one broadcasting normally -- it has no say in the matter.

That is the other half of the thesis, and until now the project has not had
it. matching.py can be written the moment this file produces output.

THREE THINGS THAT MAKE IT HONEST

  geolocation is MEASURED   Not against the control points the transform was
                            built from -- bilinear interpolation passes
                            through those exactly, so that check cannot fail
                            and therefore proves nothing. It is rebuilt from
                            half the geolocation grid and scored on the lines
                            left out. An offset transform produces detections
                            that look perfect and are uniformly in the wrong
                            place -- which matching.py would read as a sea
                            full of dark vessels.

  land is masked FIRST      From the WHOLE SCENE's histogram, not from each
                            tile's. A per-tile threshold inverts wherever a
                            tile is mostly land -- which is where it matters.
                            Unmasked, land produces detections by the million,
                            and a coastline inside a CFAR background window
                            inflates sigma and suppresses real detections for
                            hundreds of metres offshore.

  tiles OVERLAP             A 433 MP raster is processed in windows. A vessel
                            on a tile boundary would be cut in two and counted
                            twice, or fall in the margin where the background
                            statistics are incomplete and be missed. The halo
                            is discarded from the output for exactly that.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.adapters.maritime import cfar, water
from angels.adapters.maritime import searched as searched_mod
from angels.adapters.maritime.geolocate import Geolocator
from angels.config import AIS_FRONTIER, AOI_SEA, EVENTS, RAW
from angels.core.models import Observation, Position

SAR = RAW / "sar"

TILE = 2048
# Halo must exceed the background window, or pixels near a tile edge get their
# clutter estimated from an incomplete ring and threshold unpredictably.
HALO = cfar.BACKGROUND


def vv_member(zpath: Path) -> str:
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            low = info.filename.lower()
            if "/measurement/" in low and "-vv-" in low \
                    and low.endswith((".tiff", ".tif")):
                return info.filename
    raise FileNotFoundError(f"no VV measurement raster inside {zpath.name}")


def acquired_at(name: str) -> datetime:
    """The instant the scene represents -- the start of the acquisition.

    Everything in matching.py depends on this: AIS is interpolated TO this
    timestamp. A scene whose time is wrong produces confident, wrong
    discrepancies, so it is parsed from the filename rather than from the file
    modification time, which is when it was downloaded.
    """
    stamp = name.split("_")[4]              # 20260910T225729
    return datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(
        tzinfo=UTC)


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


def scene_footprint(loc, width: int, height: int, per_edge: int = 12):
    """The ground quadrilateral this raster covers, as [[lon, lat], ...].

    THE DENOMINATOR DEPENDS ON THIS.

    matching.py asks "of the vessels that reported themselves HERE and THEN,
    how many did the radar find?" -- and "here" has to mean the part of the
    sea this scene actually looked at. Scored against the whole study area
    instead, a slice covering offshore Virginia is charged with missing every
    vessel in Delaware Bay 300 km away, and the detection rate collapses for
    reasons that have nothing to do with the detector.

    Edges are SAMPLED rather than taken as four corners, because a Sentinel-1
    swath is not a straight-sided quadrilateral on the ground -- the range
    direction curves. Twelve points an edge follows it closely enough that the
    error is far smaller than the AIS window already admits.

    Slightly generous: the illuminated swath is a parallelogram inside the
    rectangular raster, so the rectangle's corners sit in the zero-filled
    region. That errs toward counting a few vessels the sensor could not have
    seen, which depresses the detection rate -- the safe direction.
    """
    w, h = width - 1, height - 1
    pts = []
    for i in range(per_edge):                       # top, left to right
        pts.append(loc.lonlat(w * i / per_edge, 0))
    for i in range(per_edge):                       # right edge, down
        pts.append(loc.lonlat(w, h * i / per_edge))
    for i in range(per_edge):                       # bottom, right to left
        pts.append(loc.lonlat(w * (per_edge - i) / per_edge, h))
    for i in range(per_edge):                       # left edge, up
        pts.append(loc.lonlat(0, h * (per_edge - i) / per_edge))
    return [[round(lon, 5), round(lat, 5)] for lon, lat in pts]


def clutter_report(obs, k: float) -> dict:
    """Does this detection list contain a vessel population, or a clutter tail?

    THE CHECK THAT SAYS "THESE ARE NOT SHIPS".

    Sea clutter in SAR is K-distributed -- heavy-tailed, not Gaussian -- so a
    threshold set in sigmas admits a long tail of bright speckle. When that is
    all a scene contains, the SNR histogram decays monotonically from the
    threshold and stops a little above it. When real vessels are present they
    form a separate population reaching tens to hundreds of sigma, because a
    steel hull returns orders of magnitude more than water.

    Two numbers separate those cases without needing AIS:

      headroom    brightest SNR / k. Clutter-only runs land near 1.5-2.
                  A scene with vessels in it reaches 20 or more.
      at_floor    share of detections sitting at exactly min_pixels. Clutter
                  clusters are the smallest thing the detector will accept;
                  vessels are not.

    Measured on a real slice of open Atlantic with no traffic in the study
    area: headroom 1.8, at_floor 67%, 567 detections, not one of them a ship.
    """
    if not obs:
        return {"n": 0, "headroom": 0.0, "at_floor": 0.0, "verdict": "empty"}

    snrs = [o.attributes["snr"] for o in obs]
    pxs = [o.attributes["pixels"] for o in obs]
    floor = min(pxs)
    headroom = max(snrs) / k if k else 0.0
    at_floor = sum(1 for x in pxs if x == floor) / len(pxs)

    if headroom < 2.5 and at_floor > 0.4:
        verdict = "clutter"
    elif headroom < 5.0:
        verdict = "marginal"
    else:
        verdict = "vessels present"
    return {"n": len(obs), "headroom": headroom, "at_floor": at_floor,
            "max_snr": max(snrs), "verdict": verdict}


def _scene_date(path: Path) -> date | None:
    """Acquisition date from the SAFE name, or None if it cannot be read.

    Unknown counts as usable rather than being dropped: a scene this cannot
    parse should reach the detector and fail there with a real message, not
    vanish from the candidate list for a reason nobody sees.
    """
    try:
        return acquired_at(path.name).date()
    except (IndexError, ValueError):
        return None


def detect_scene(zpath: Path, *, k: float, max_tiles: int | None,
                 mask_land: bool, verbose: bool) -> tuple[list[Observation], dict]:
    import numpy as np
    import rasterio
    from rasterio.windows import Window

    member = vv_member(zpath)
    t = acquired_at(zpath.name)
    uri = f"zip://{zpath.as_posix()}!/{member}"

    print(f"\n  {zpath.name[:62]}")
    print(f"  acquired {t:%Y-%m-%d %H:%M:%S} UTC")

    with rasterio.open(uri) as src:
        gcps, crs = src.get_gcps()
        loc = Geolocator(gcps, crs)

        # Before anything else: is the transform good enough to trust?
        acc = loc.check()
        print(f"  geolocation  {acc}")
        print(f"  raster       {src.width:,} x {src.height:,} px")

        # One threshold for the whole scene, from the whole scene's histogram.
        # Built before any tile is read, so no tile can influence it.
        try:
            mask = water.from_raster(src) if mask_land else None
        except water.MaskError as exc:
            # Refused, not crashed. The message says which of the two
            # ambiguous cases this might be and what to do about each.
            print(f"\n  WATER MASK REFUSED\n  {exc}\n")
            raise
        if mask is None:
            print("  water        NOT MASKED -- land produces detections by "
                  "the million")
        else:
            print(f"  water        {mask}")

        tiles = [(r, c)
                 for r in range(0, src.height, TILE)
                 for c in range(0, src.width, TILE)]
        if max_tiles and max_tiles < len(tiles):
            # SPREAD, not the first N. The first N tiles are the top edge of
            # the swath -- one strip of one corner, and in a scene like this
            # one that happens to be mostly Maryland. A quick look that always
            # samples the same corner tells you about the corner.
            step = len(tiles) / max_tiles
            tiles = [tiles[int(i * step)] for i in range(max_tiles)]
        print(f"  processing   {len(tiles)} tiles of {TILE} px\n")

        obs: list[Observation] = []
        skipped = 0
        # A SECOND, INDEPENDENT GUARD. The water mask can fail -- it did, on a
        # slice covering inland Pennsylvania, where Otsu found one mode and the
        # fallback assumed ocean. 112,954 towns and silos came out formatted
        # exactly like vessels.
        #
        # The study area is a stated fact, not an inference from pixels, so a
        # detection outside AOI_SEA is out of scope whatever the mask believed.
        # Two guards that can fail independently is the point: the mask reasons
        # from brightness, this reasons from geography.
        outside = 0
        # How much sea was actually searched. Without this, a tile reporting
        # no detections is indistinguishable from a tile that was entirely
        # land -- and the second is not evidence of an empty sea. Every
        # statement this project makes about absence depends on being able to
        # say the thing could have appeared.
        searched_px = 0
        refused_px = 0
        dry_tiles = 0

        # The same question at ground coordinates, so that a later run with
        # neither the scene nor rasterio can still ask whether a given vessel
        # position was inside the searched water. searched_km2 alone cannot
        # answer that: it is a total, and the vessels that matter crowd into
        # the particular kilometres the mask removed.
        coverage: list[tuple[float, float, bool]] = []

        def sample_tile(core, r0: int, c0: int) -> None:
            """Geolocate a sparse lattice of this tile's core and record it.

            Both outcomes are recorded, not just the water: a cell that was
            sampled and found to be land has to be distinguishable from a cell
            that was never sampled, or every cell the swath touches looks
            fully searched.
            """
            s = searched_mod.SAMPLE_STRIDE
            for ii in range(0, core.shape[0], s):
                for jj in range(0, core.shape[1], s):
                    lon, lat = loc.lonlat(c0 + jj, r0 + ii)
                    coverage.append((lon, lat, bool(core[ii, jj])))

        for i, (r0, c0) in enumerate(tiles, 1):
            rr0, cc0 = max(0, r0 - HALO), max(0, c0 - HALO)
            rr1 = min(src.height, r0 + TILE + HALO)
            cc1 = min(src.width, c0 + TILE + HALO)
            arr = src.read(1, window=Window(cc0, rr0, cc1 - cc0, rr1 - rr0))

            if arr.size == 0 or not arr.any():
                continue

            # Exact zeros are outside the illuminated swath -- the corners of
            # the rectangle the parallelogram sits in. They are darker than
            # water, so a background ring straddling the edge reads as
            # improbably quiet and the first real pixel beyond it clears the
            # threshold easily. Excluding them is not tidiness; it removes a
            # line of false detections down the edge of every scene.
            valid = arr > 0
            if mask is not None:
                valid &= mask.tile(rr0, cc0, arr.shape[0], arr.shape[1])

            # Count only the tile proper, not its halo -- the halo belongs to
            # the neighbouring tile and would be counted twice.
            core = valid[r0 - rr0:r0 - rr0 + TILE, c0 - cc0:c0 - cc0 + TILE]
            sea_px = int(core.sum())
            if sea_px == 0:
                dry_tiles += 1
                if verbose:
                    print(f"    tile {i}/{len(tiles)}  rows {r0}-{r0+TILE}  "
                          f"no water -- not searched")
                continue

            try:
                found = cfar.detect(arr, k=k, valid=valid)
            except cfar.TooManyDetections as exc:
                skipped += 1
                # A tile the detector refused is NOT searched water. It used
                # to be counted as searched anyway, which made a refusal
                # indistinguishable from an empty sea in every rate the scene
                # supports -- an error reported as an absence, in the one
                # place the project has a standing rule against it.
                refused_px += sea_px
                if verbose:
                    print(f"    tile {i}: skipped -- {exc}")
                continue

            # Counted only now that the tile has actually been examined, so
            # searched_px and the grid below both mean "looked at", not
            # "attempted".
            searched_px += sea_px
            sample_tile(core, r0, c0)

            for d in found:
                # Discard the halo. A detection there either belongs to the
                # neighbouring tile (and would be counted twice) or sits where
                # the background ring ran off the edge.
                gr, gc = rr0 + d.row, cc0 + d.col
                if not (r0 <= gr < r0 + TILE and c0 <= gc < c0 + TILE):
                    continue

                lon, lat = loc.lonlat(gc, gr)
                if not (AOI_SEA[0] <= lon <= AOI_SEA[2]
                        and AOI_SEA[1] <= lat <= AOI_SEA[3]):
                    outside += 1
                    continue

                obs.append(Observation(
                    position=Position(
                        lat=lat, lon=lon, t=t,
                        # Geolocation residual and half the cluster extent,
                        # added in quadrature. Never optional, never a guess.
                        # PER DETECTION, not per scene. Measured across ten
                        # real slices, geolocation error is not uniform: a
                        # scene whose median node error was ~20 m had
                        # individual nodes at 240 m, and which of those a
                        # detection inherits depends on where in the swath it
                        # sits. A scene-wide figure makes the good majority
                        # look worse than they are and the few bad ones look
                        # better -- and the second half is how a displaced
                        # detection reaches matching.py wearing a small
                        # uncertainty and is counted as a dark vessel.
                        uncertainty_m=float(np.hypot(
                            loc.local_error_m(gc, gr), d.length_m / 2)),
                    ),
                    sensor="sentinel1-vv-cfar",
                    # Confidence lives in attributes, not on Observation.
                    # core.models.Observation has no confidence field and does
                    # not need one: SNR IS the confidence for a CFAR detection,
                    # it is sensor-specific, and `attributes` exists exactly so
                    # an adapter can carry what only it knows. Adding a field
                    # to core for one adapter's convenience is how a
                    # domain-blind core stops being domain-blind.
                    attributes={
                        "confidence": round(min(0.95, d.snr / 20.0), 3),
                        "snr": round(d.snr, 2),
                        "peak_dn": round(d.peak, 1),
                        "background_dn": round(d.background, 1),
                        "sigma_dn": round(d.sigma, 2),
                        "pixels": d.pixels,
                        "length_m_approx": round(d.length_m, 1),
                        "pixel": [round(gc, 1), round(gr, 1)],
                        "scene": zpath.stem,
                    },
                ))

            if verbose:
                print(f"    tile {i}/{len(tiles)}  rows {r0}-{r0+TILE}  "
                      f"{100 * sea_px / TILE**2:4.0f}% sea  {len(found)} raw")

    # TWO INDEPENDENT ROUTES TO THE SAME NUMBER. searched_px counts mask
    # pixels; the grid geolocates a 320 m lattice of them and sums cell areas.
    # They share the mask and nothing else, so agreement is evidence that the
    # geolocation and the sampling are both sound. A large disagreement is a
    # bug in one of them, and saying so here is cheaper than discovering it
    # later as an inexplicable detection rate.
    area = searched_mod.SearchedArea.from_samples(coverage)
    print(f"  searched     {area}")
    px_km2 = searched_px * 1e-4
    if px_km2 > 0:
        drift = abs(area.area_km2 - px_km2) / px_km2
        if drift > 0.15:
            print(f"  WARNING: the searched grid says {area.area_km2:,.0f} km2 "
                  f"but the pixel count says {px_km2:,.0f} km2 "
                  f"({100 * drift:.0f}% apart).")
            print("  They are computed from the same mask by different routes, "
                  "so this is")
            print("  a bug in the sampling or the geolocation, not a fact "
                  "about the sea.")
    if refused_px:
        print(f"  refused      {refused_px * 1e-4:,.1f} km2 of water in "
              f"{skipped} tile(s) the detector would not examine")
        print("  That water is not searched and is excluded from the "
              "denominator.")

    meta = {
        "scene": zpath.stem,
        "acquired": t.isoformat(),
        "geolocation_rms_m": round(acc.best_m, 1),
        "geolocation_holdout_m": round(acc.rms_m, 1),
        "geolocation_median_m": round(acc.median_m, 1),
        "geolocation_p90_m": round(acc.p90_m, 1),
        "geolocation_worst_m": round(acc.worst_m, 1),
        "geolocation_uneven": acc.uneven,
        "geolocation_convergence": (round(acc.ratio, 2)
                                    if acc.ratio else None),
        "geolocation_method": acc.method,
        # How that figure was obtained, not just what it was. A residual
        # against the transform's own control points and a holdout against
        # points it has never seen are different claims, and only one of them
        # can fail.
        "geolocation_validation": acc.validation,
        "tiles": len(tiles),
        "tiles_skipped": skipped,
        "k_sigma": k,
        "land_masked": mask_land,
        "water_fraction": round(mask.water_fraction, 3) if mask else 1.0,
        "land_sea_contrast": round(mask.contrast, 2) if mask else None,
        "coast_blind_m": cfar.BACKGROUND / 2 * 10.0 if mask else 0.0,
        # The denominator for every rate this scene supports. A count of
        # detections without the area searched is not a density, and a density
        # is what any claim about concentration needs.
        "searched_km2": round(searched_px * 1e-4, 1),
        # Water the detector refused rather than examined. Kept apart from
        # searched_km2 so that a scene which mostly failed cannot present
        # itself as a scene which mostly found nothing.
        "refused_km2": round(refused_px * 1e-4, 1),
        "tiles_without_water": dry_tiles,
        "outside_aoi": outside,
        "aoi_sea": list(AOI_SEA),
        # What this scene actually looked at. match_maritime.py uses it as the
        # denominator; without it every slice is scored against the whole
        # study area.
        "footprint": scene_footprint(loc, src.width, src.height),
        # What it actually LOOKED AT inside that outline. The footprint is the
        # raster's ground coverage; this is the footprint minus land, minus
        # the 605 m coastal blind zone, minus any tile the detector refused.
        # On slice 2 of the 2024-09-25 pass the difference was 286 of 306 AIS
        # vessels -- a solid block of them at Norfolk and in the York River
        # anchorage, none of which the radar could have seen.
        "searched_grid": area.to_json(),
    }
    return obs, meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--all", action="store_true", help="every scene on disk")
    ap.add_argument("--path", type=Path)
    ap.add_argument("--k", type=float, default=cfar.K_SIGMA)
    ap.add_argument("--max-tiles", type=int,
                    help="sample N tiles across the scene -- for a quick look")
    ap.add_argument("--no-land-mask", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.path:
        scenes = expand(args.path)
        if not scenes:
            print(f"\n  Nothing matches {args.path}\n")
            return 1
    else:
        scenes = sorted(SAR.glob("*.zip"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
        if not scenes:
            print(f"\n  No scenes in {SAR}")
            print("  Run: python scripts/fetch_sar.py --download 1\n")
            return 1

        if not args.all:
            # Prefer a scene whose AIS exists. Defaulting to the newest file
            # is the obvious choice and reliably picks one that cannot be
            # matched afterwards -- the newest scenes are past the AIS
            # frontier by construction. Detecting in them is still valid work;
            # it just must not be what happens when no scene was named.
            frontier = date.fromisoformat(AIS_FRONTIER)
            matchable = [p for p in scenes
                         if (d := _scene_date(p)) is None or d <= frontier]
            if matchable:
                if scenes.index(matchable[0]):
                    print(f"\n  preferring the newest scene with published "
                          f"AIS ({AIS_FRONTIER} or earlier);")
                    print(f"  {scenes.index(matchable[0])} newer scene(s) "
                          f"skipped -- name one with --path to use it")
                scenes = matchable[:1]
            else:
                print(f"\n  No scene on disk is from on or before "
                      f"{AIS_FRONTIER}, so nothing here can be matched")
                print("  against AIS afterwards. Detecting anyway:")
                scenes = scenes[:1]

    EVENTS.mkdir(parents=True, exist_ok=True)
    total = 0

    for zpath in scenes:
        try:
            obs, meta = detect_scene(
                zpath, k=args.k, max_tiles=args.max_tiles,
                mask_land=not args.no_land_mask, verbose=args.verbose)
        except Exception as exc:
            print(f"\n  FAILED on {zpath.name[:50]}: "
                  f"{type(exc).__name__}: {exc}\n")
            continue

        print(f"  {len(obs)} detections over {meta['searched_km2']:,} km2 "
              f"of searched water")
        if meta["outside_aoi"]:
            share = meta["outside_aoi"] / (meta["outside_aoi"] + len(obs))
            print(f"  {meta['outside_aoi']:,} discarded outside AOI_SEA "
                  f"({100 * share:.0f}% of everything found)")
            if share > 0.5:
                print("  MOST of this scene is outside the study area. Either")
                print("  the land mask failed, or this slice does not cover")
                print("  the sea box -- the outer slices of a pass often do")
                print("  not. Check scripts/inspect_geolocation.py --all for")
                print("  which slice is which.")
        if meta["tiles_without_water"]:
            print(f"  {meta['tiles_without_water']} tile(s) held no water and "
                  f"were not searched -- not an empty sea, an unsearched one")
        c = clutter_report(obs, meta["k_sigma"])
        if c["n"]:
            print(f"  brightest {c['max_snr']:.1f} sigma = "
                  f"{c['headroom']:.1f}x the k={meta['k_sigma']} threshold; "
                  f"{100 * c['at_floor']:.0f}% sit at the minimum cluster size")
            if c["verdict"] == "clutter":
                print()
                print("  THIS LOOKS LIKE CLUTTER, NOT VESSELS.")
                print("  The SNR histogram decays straight from the threshold")
                print("  and stops just above it, and most clusters are the")
                print("  smallest the detector will accept. A steel hull")
                print("  returns orders of magnitude more than water, so real")
                print("  traffic reaches 20x the threshold or more.")
                print("  Either this slice holds no vessels inside AOI_SEA, or")
                print("  k is set below the clutter tail. Check which by")
                print("  running a slice that covers the shipping lanes.")
            elif c["verdict"] == "marginal":
                print("  (marginal -- a few strong returns, mostly tail)")
        if meta["tiles_skipped"]:
            print(f"  {meta['tiles_skipped']} tiles skipped (too many hits -- "
                  f"land, or k too low)")

        meta["clutter_headroom"] = round(c["headroom"], 2) if c["n"] else None
        meta["clutter_at_floor"] = round(c["at_floor"], 3) if c["n"] else None
        meta["clutter_verdict"] = c["verdict"]

        if obs:
            strong = [o for o in obs if o.attributes["snr"] > 10]
            print(f"  {len(strong)} above 10 sigma\n")
            print(f"    {'lat':>9}{'lon':>10}{'snr':>8}{'len m':>8}{'px':>6}")
            for o in sorted(obs, key=lambda o: -o.attributes["snr"])[:10]:
                a = o.attributes
                print(f"    {o.position.lat:9.4f}{o.position.lon:10.4f}"
                      f"{a['snr']:8.1f}{a['length_m_approx']:8.0f}"
                      f"{a['pixels']:6d}")

            out = EVENTS / f"sar-{zpath.stem[:40]}.geojson"
            out.write_text(json.dumps({
                "type": "FeatureCollection",
                "properties": meta,
                "features": [{
                    "type": "Feature",
                    "geometry": {"type": "Point",
                                 "coordinates": [o.position.lon,
                                                 o.position.lat]},
                    "properties": {**o.attributes,
                                   "t": o.position.t.isoformat(),
                                   "sensor": o.sensor,
                                   "uncertainty_m": round(
                                       o.position.uncertainty_m, 1)},
                } for o in obs],
            }, indent=1), encoding="utf-8")
            print(f"\n  wrote {out}")
        total += len(obs)

    print(f"\n  {total} observations across {len(scenes)} scene(s).")
    print("  Next: matching.py -- pair these against AIS at the same instant.")
    print("  A detection with no AIS partner is a dark vessel.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
