"""Non-cooperative aviation at the ADS-B obligation boundary.

    python scripts/air_discrepancy.py
    python scripts/air_discrepancy.py --since 20260925 --floor 200

Reads data/raw/aviation-adsbfi/, builds the MLAT coverage mask, and computes
the difference in differences pre-registered in docs/phase-f-prereg.md.
Writes data/events/air-bands.json.

THE QUESTION

14 CFR 91.225(d) requires ADS-B Out within 30 nm of an appendix D section 1
airport from the surface to 10,000 ft MSL, and at and above 10,000 ft
everywhere in the 48 states. So BELOW 10,000 ft the obligation switches off
at the veil boundary, and AT OR ABOVE 10,000 ft it applies on both sides.

That gives a treatment and a control over the same receivers, the same
distance gradient and the same days. The statistic is the step across the
boundary below 10,000 ft minus the step above it. A step equal in both is
confound, not regulation.

WHAT COUNTS AS INDEPENDENT, AND WHAT DOES NOT

`mlat` and `mode_s` only. See AMENDMENT 1 in the pre-registration: TIS-B is
uplinked from ground radar FOR THE BENEFIT of ADS-B-equipped aircraft in a
service volume, and its presence rises with cooperative density faster than
MLAT's does -- 28.4, 51.0, 72.2, 89.3 percent against 33.1, 59.6, 68.6, 78.6
across density bins. A service provisioned toward traffic cannot be the
independent observer in a ratio whose denominator is that traffic. TIS-B is
computed and reported in its own column and is never pooled.

THE MASK

An absence outside coverage is not an absence. A cell enters the denominator
only with direct evidence (an MLAT or Mode S fix) or inferred evidence (it
lies between two consecutive MLAT fixes of one aircraft, close enough in
time that the receivers solving both ends could have solved the middle).
Everything else is excluded and counted. This is the aviation unheard water.

SCOPE

Platforms and institutions, never persons. The output is rates per cell,
band and side. No ICAO hex, registration or individual track is written to
any artefact. The never-cooperative population is a count and never a roster.
"""

from __future__ import annotations

import argparse
import collections
import random
import glob
import json
import math
import sys
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import EVENTS, REFERENCE

RAW = Path("data/raw/aviation-adsbfi")
AIRPORTS = REFERENCE / "airspace" / "appendix-d-airports.json"

CELL_DEG = 0.05
VEIL_NM = 30.0
SPLIT_FT = 10_000
MAX_GAP_S = 360.0
NM_M = 1852.0

TRIALS = 2000          # scattered, as pre-registered
SHIFT_TRIALS = 400     # shift, as pre-registered
SEED = 20261002

INDEPENDENT = ("mlat", "mode_s")
TISB = ("tisb_icao", "tisb_trackfile", "tisb_other")
COOPERATIVE = ("adsb_icao", "adsb_other", "adsr_icao", "adsr_other")


def cell_of(lon: float, lat: float) -> tuple[int, int]:
    return (math.floor(lon / CELL_DEG), math.floor(lat / CELL_DEG))


def nm_between(lat1, lon1, lat2, lon2) -> float:
    latm = math.radians((lat1 + lat2) / 2)
    return math.hypot((lon1 - lon2) * 111_195.0 * math.cos(latm),
                      (lat1 - lat2) * 111_195.0) / NM_M


def altitude_ft(v) -> int | None:
    """alt_baro is a STRING column and carries the sentinel 'ground'.

    Reading it as a number dropped every real altitude once and reported
    every independent target as being at 0 ft -- a 100% figure that was a
    parsing bug, not a finding.
    """
    if v is None or v == "ground":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def load_airports(path: Path) -> list[tuple[float, float]]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("radius_nm") != VEIL_NM:
        raise SystemExit(f"\n  {path.name} declares radius_nm "
                         f"{doc.get('radius_nm')}, this script assumes "
                         f"{VEIL_NM}. One of them is wrong.\n")
    return [(a["lat"], a["lon"]) for a in doc["airports"]], doc.get("eff_date")


def signed_nm(lon: float, lat: float, airports) -> float:
    """Distance to the veil boundary: nearest appendix D airport minus 30 nm.

    Negative inside the union of circles, positive outside. Exact for an
    exterior point, and it is the natural variable because the obligation
    itself is defined by `min distance <= 30 nm`.
    """
    return min(nm_between(lat, lon, a, b) for a, b in airports) - VEIL_NM


def read(since: str | None):
    """(per-unit counts, mask evidence). One pass; nothing is re-derived."""
    import pyarrow.parquet as pq

    files = sorted(glob.glob(str(RAW / "hour=*" / "aircraft_*.parquet")))
    if since:
        files = [f for f in files if f.split("hour=")[1][:8] >= since]
    if not files:
        raise SystemExit(f"\n  No polls under {RAW}"
                         + (f" at or after {since}" if since else "") + "\n")

    per = collections.defaultdict(lambda: [0, 0, 0])   # coop, ind, tisb
    direct: set = set()
    tracks = collections.defaultdict(list)
    days: set = set()

    for f in files:
        days.add(f.split("hour=")[1][:8])
        t = pq.read_table(f, columns=["type", "hex", "lat", "lon",
                                      "alt_baro", "fetched_at"])
        cols = [t.column(c).to_pylist() for c in
                ("type", "hex", "lat", "lon", "alt_baro", "fetched_at")]
        for k, h, la, lo, ab, ts in zip(*cols):
            if la is None or lo is None:
                continue
            c = cell_of(lo, la)
            if k in INDEPENDENT:
                direct.add(c)
                # fetched_at is a datetime. float() raised on every row once,
                # and because `tracks[h]` creates the key before the argument
                # is evaluated, the dict reported 122 aircraft while holding
                # nothing. Sighting 26.
                if hasattr(ts, "timestamp"):
                    tracks[h].append((ts.timestamp(), lo, la))
            alt = altitude_ft(ab)
            if alt is None:
                continue
            band = "<10k" if alt < SPLIT_FT else ">=10k"
            key = (c, band, f.split("hour=")[1][:8])
            if k in INDEPENDENT:
                per[key][1] += 1
            elif k in TISB:
                per[key][2] += 1
            elif k in COOPERATIVE:
                per[key][0] += 1
    return per, direct, tracks, sorted(days), len(files)


def continuity(tracks) -> set:
    """Cells between two consecutive MLAT fixes of one aircraft."""
    out: set = set()
    for pts in tracks.values():
        pts.sort()
        for (t0, x0, y0), (t1, x1, y1) in zip(pts, pts[1:]):
            if not (0 < t1 - t0 <= MAX_GAP_S):
                continue
            steps = max(abs(x1 - x0), abs(y1 - y0)) / (CELL_DEG / 2)
            n = int(min(max(steps, 1), 400))
            for i in range(n + 1):
                out.add(cell_of(x0 + (x1 - x0) * i / n,
                                y0 + (y1 - y0) * i / n))
    return out


def difference_in_differences(units, sides, floor: int) -> dict:
    """`units` is keyed by (cell, band) or (cell, band, day) -- anything whose
    first two elements identify the cell and the altitude band.

    It takes the key as given and NEVER re-keys it. An earlier version passed
    the per-day units through `{(k[0], k[1]): v for k, v in units.items()}`,
    which silently overwrote every day but the last: 565 units became 78, the
    control group fell to 4, and the difference in differences flipped from
    -0.00007 to +0.00443 -- from refuted to not refuted, in the direction of
    the hypothesis. A dict comprehension that drops a dimension does not
    report that it dropped one.
    """
    g = collections.defaultdict(list)
    pooled = collections.defaultdict(lambda: [0, 0])
    for key, (cp, ind) in units.items():
        c, band = key[0], key[1]
        if cp < floor:
            continue
        s = sides[c]
        g[(band, s)].append(ind / (ind + cp) if (ind + cp) else 0.0)
        pooled[(band, s)][0] += cp
        pooled[(band, s)][1] += ind

    def mean(k):
        return sum(g[k]) / len(g[k]) if g[k] else float("nan")

    def pool(k):
        cp, ind = pooled[k]
        return ind / (ind + cp) if (ind + cp) else float("nan")

    out = {"floor": floor, "by_group": {}, "n_units": {}}
    steps = {}
    for band in ("<10k", ">=10k"):
        i, o = mean((band, "inside")), mean((band, "outside"))
        steps[band] = o - i
        out["by_group"][band] = {
            "inside": round(i, 6), "outside": round(o, 6),
            "step": round(o - i, 6),
            "pooled_inside": round(pool((band, "inside")), 6),
            "pooled_outside": round(pool((band, "outside")), 6)}
        out["n_units"][band] = {"inside": len(g[(band, "inside")]),
                                "outside": len(g[(band, "outside")])}
    out["did"] = round(steps["<10k"] - steps[">=10k"], 6)
    out["treatment_step"] = round(steps["<10k"], 6)
    out["control_step"] = round(steps[">=10k"], 6)
    return out


def nulls(units, sides, floor: int, observed: float,
          trials: int = TRIALS, shift_trials: int = SHIFT_TRIALS,
          seed: int = SEED) -> dict:
    """Two nulls, the same pair the maritime domain uses.

    SCATTERED re-assigns each cell's side at random, keeping the inside and
    outside counts. It destroys all spatial structure and is expected to
    reject: it measures that the two sides differ at all.

    SHIFT translates the cell-to-side map by a random offset, carrying the
    clustering of traffic intact onto a boundary in the wrong place. **This
    is the one that decides.** A difference a displaced boundary reproduces
    is a property of the pattern, not of the boundary.

    p is reported as k of n+1 because 400 trials cannot resolve finer, and a
    bare p-value hides that.
    """
    rng = random.Random(seed)
    cells = sorted(sides)
    n_out = sum(1 for c in cells if sides[c] == "outside")

    def did_with(m):
        return difference_in_differences(units, m, floor)["did"]

    scat = 0
    for _ in range(trials):
        shuffled = cells[:]
        rng.shuffle(shuffled)
        m = {c: ("outside" if i < n_out else "inside")
             for i, c in enumerate(shuffled)}
        if abs(did_with(m)) >= abs(observed):
            scat += 1

    cols = [c[0] for c in cells]; rows = [c[1] for c in cells]
    span_c, span_r = max(cols) - min(cols), max(rows) - min(rows)
    shift = 0
    for _ in range(shift_trials):
        dc = rng.randint(-span_c, span_c)
        dr = rng.randint(-span_r, span_r)
        m = {c: sides.get((c[0] + dc, c[1] + dr), sides[c]) for c in cells}
        if abs(did_with(m)) >= abs(observed):
            shift += 1

    return {"observed": round(observed, 6),
            "trials": trials, "shift_trials": shift_trials, "seed": seed,
            "scattered_k": scat, "scattered_p": (scat + 1) / (trials + 1),
            "shift_k": shift, "shift_p": (shift + 1) / (shift_trials + 1)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", help="first day to include, YYYYMMDD")
    ap.add_argument("--floor", type=int, default=200,
                    help="minimum cooperative fixes per unit (pre-registered "
                         "at 200; changing it is an amendment)")
    ap.add_argument("--out", type=Path, default=EVENTS / "air-bands.json")
    args = ap.parse_args()

    if not AIRPORTS.exists():
        print(f"\n  No {AIRPORTS}. It is tracked; check out the repo.\n")
        return 1
    airports, eff = load_airports(AIRPORTS)

    per, direct, tracks, days, n_polls = read(args.since)
    inferred = continuity(tracks) - direct

    coop_cells = collections.Counter()
    for (c, _b, _d), v in per.items():
        coop_cells[c] += v[0]
    have = {c for c, n in coop_cells.items() if n}
    mask = (direct | inferred) & have
    blind = have - mask

    sides = {c: ("outside" if signed_nm((c[0] + 0.5) * CELL_DEG,
                                        (c[1] + 0.5) * CELL_DEG,
                                        airports) >= 0 else "inside")
             for c in mask}

    day_unit, flat_unit = {}, collections.defaultdict(lambda: [0, 0])
    tisb_tot = ind_tot = coop_tot = 0
    for (c, band, day), (cp, ind, tb) in per.items():
        tisb_tot += tb; ind_tot += ind; coop_tot += cp
        if c not in mask:
            continue
        day_unit[(c, band, day)] = [cp, ind]
        flat_unit[(c, band)][0] += cp
        flat_unit[(c, band)][1] += ind

    day_sides = {c: sides[c] for c in mask}
    primary = difference_in_differences(day_unit, day_sides, args.floor)
    # the day dimension collapsed: a declared sensitivity, not an amendment
    sensitivity = difference_in_differences(flat_unit, day_sides, args.floor)

    print(f"\n  {n_polls:,} polls over {len(days)} day(s), "
          f"{days[0]} to {days[-1]}")
    print(f"  appendix D airports {len(airports)}, EFF_DATE {eff}, "
          f"veil radius {VEIL_NM:.0f} nm")
    print(f"\n  independent channel: {', '.join(INDEPENDENT)}   "
          f"({ind_tot:,} fixes)")
    print(f"  NOT pooled:          {', '.join(TISB)}   ({tisb_tot:,} fixes)")
    print(f"  cooperative:         {coop_tot:,} fixes")

    print(f"\n  coverage mask")
    print(f"    cells with cooperative traffic   {len(have):,}")
    print(f"    direct mlat/mode_s evidence      {len(direct & have):,}")
    print(f"    recovered by track continuity    {len(inferred & have):,}")
    print(f"    IN THE MASK                      {len(mask):,}  "
          f"({100 * len(mask) / len(have):.1f}%)")
    print(f"    blind, excluded                  {len(blind):,}  "
          f"({100 * len(blind) / len(have):.1f}%)")

    for name, r in (("PRE-REGISTERED unit (cell, band, day)", primary),
                    ("SENSITIVITY  day collapsed (cell, band)", sensitivity)):
        print(f"\n  {name}, floor {r['floor']}")
        print(f"    {'':9}{'inside':>11}{'outside':>11}{'step':>11}{'units':>9}")
        for band in ("<10k", ">=10k"):
            b = r["by_group"][band]; n = r["n_units"][band]
            print(f"    {band:9}{b['inside']:11.5f}{b['outside']:11.5f}"
                  f"{b['step']:+11.5f}{n['inside'] + n['outside']:9,}")
        print(f"    {'DiD':9}{'':11}{'':11}{r['did']:+11.5f}")

    print(f"\n  nulls on the pre-registered statistic "
          f"({TRIALS:,} scattered, {SHIFT_TRIALS} shift, seed {SEED})")
    nl = nulls(day_unit, day_sides, args.floor, primary["did"])
    print(f"    scattered  {nl['scattered_k']:5,} of {nl['trials']:,}"
          f"   p = {nl['scattered_p']:.4f}")
    print(f"    SHIFT      {nl['shift_k']:5,} of {nl['shift_trials']}"
          f"     p = {nl['shift_p']:.4f}   <- this is the one that decides")

    verdict = ("REFUTED: the difference is not positive"
               if primary["did"] <= 0 else
               ("REFUTED: positive, but a displaced boundary reproduces it "
                f"({nl['shift_k']} of {nl['shift_trials']} shifts, "
                f"p = {nl['shift_p']:.4f})"
                if nl["shift_p"] > 0.05 else
                "SUPPORTED: positive and survives the shift null"))
    print(f"\n  {verdict}")
    print("  Decision rule: docs/phase-f-prereg.md. Support requires a "
          "POSITIVE\n  difference surviving the shift null; a non-positive "
          "difference is refutation.")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "what": "non-cooperative aviation share across the ADS-B obligation "
                "boundary, 14 CFR 91.225(d)",
        "prereg": "docs/phase-f-prereg.md",
        "independent_types": list(INDEPENDENT),
        "tisb_types_excluded": list(TISB),
        "tisb_exclusion_reason": "AMENDMENT 1: TIS-B presence rises with "
                                 "cooperative density faster than MLAT's, so "
                                 "it is correlated with the denominator",
        "cooperative_types": list(COOPERATIVE),
        "cell_deg": CELL_DEG, "veil_nm": VEIL_NM, "split_ft": SPLIT_FT,
        "max_gap_s": MAX_GAP_S,
        "airports_eff_date": eff, "n_airports": len(airports),
        "days": days, "n_polls": n_polls,
        "fixes": {"cooperative": coop_tot, "independent": ind_tot,
                  "tisb_not_pooled": tisb_tot},
        "mask": {"cells_with_cooperative": len(have),
                 "direct": len(direct & have),
                 "inferred_by_continuity": len(inferred & have),
                 "in_mask": len(mask), "blind_excluded": len(blind)},
        "primary": primary, "sensitivity_day_collapsed": sensitivity,
        "nulls": nl,
        "verdict": verdict,
    }, indent=1), encoding="utf-8")
    print(f"\n  wrote {args.out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
