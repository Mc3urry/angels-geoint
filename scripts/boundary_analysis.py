"""Do dark-vessel candidates concentrate at governance discontinuities?

    python scripts/boundary_analysis.py
    python scripts/boundary_analysis.py --reception heard,thin --trials 5000

THE QUESTION, STATED AS ARITHMETIC

Candidates are counted per band of distance to the nearest maritime limit
line, and compared against the SEARCHED WATER in the same band. A band with
twice the water should hold twice the candidates if nothing is going on.
Anything else is the finding -- or an artefact, and the three guards below
are what tell them apart.

    per km2, not per candidate   the denominator is the searched grid, cell
                                 by cell, the same one the detection rate
                                 uses. The radar does not search bands
                                 evenly and never will.

    stratified by pass           candidate density varies sixteen-fold
                                 between dates, almost all of it sea state.
                                 Expected counts are computed WITHIN each
                                 pass and then summed, so a rough day
                                 inflates a band only if it inflates that
                                 band more than its own other bands. This is
                                 the sea-state control, and it costs nothing
                                 but arithmetic.

    a permutation null           the p-value comes from re-throwing each
                                 pass's own candidates into its own searched
                                 water, thousands of times, not from a
                                 chi-square table whose assumptions
                                 (independent points, large expected counts)
                                 this data does not meet.

    a SHIFTED null as well       and re-throwing is not enough either.
                                 Candidates are clustered -- clutter comes in
                                 weather patches, vessels in lanes -- and a
                                 null that scatters them independently makes
                                 any clustered pattern look impossible,
                                 producing tiny p-values for nothing. The
                                 shift null instead slides the whole pattern
                                 of one pass by a random offset inside that
                                 pass's own searched water, preserving the
                                 clustering and destroying only the
                                 relationship to the lines. Both p-values are
                                 printed; where they disagree, the shifted
                                 one is the one to believe.

WHAT A POSITIVE RESULT WOULD AND WOULD NOT MEAN

It would mean unexplained radar returns sit closer to legal lines than the
searched water does. It would NOT by itself mean vessels are choosing those
lines: shipping lanes, fishing grounds, wind leases and traffic separation
schemes are also near boundaries, and clutter is not uniform either. The
honest next step after a positive result is to repeat it against matched
(AIS-reporting) vessels, which this script also reports for exactly that
reason -- if reporting traffic shows the same pattern, the pattern is about
where ships are, not about who is hiding.

THE LOCAL STEP TEST (--near-nm)

Across a whole study area the biggest spatial signal is not any boundary: it
is the shore. Bays are crowded, the open sea is empty, and both candidates
and reporting vessels follow that gradient. A test run over the full range
mostly measures it, and the extreme bands then carry the statistic.

--near-nm 10 keeps only the water within ten miles either side of the line.
Over that strip the shore gradient is mild and roughly symmetric, so what is
left is the thing the question is actually about: is there a STEP as the
line is crossed? Run it after the full version, and read the two together.

INSIDE OR OUTSIDE, NOT JUST NEAR

A boundary effect is a STEP: behaviour differs on one side. Unsigned
distance cannot see a step -- it folds 1 nm inside the territorial sea onto
1 nm outside it and reports the average. So each limit is also measured with
a sign, landward negative and seaward positive. The side is worked out from
the companion line (the 24 nm zone sits 12 nm outside the 12 nm one), which
needs no dataset this project does not already have.

ONE LINE OR SEVERAL

NOAA's file carries the 12 nm territorial sea, the 24 nm contiguous zone and
the 200 nm EEZ in one layer, told apart only by attributes. Distance to the
NEAREST of them answers a different question from distance to a particular
one -- a point can be 1 nm from the EEZ and 150 nm from the territorial sea.
So each limit is measured separately, and "any limit" is reported beside
them rather than instead of them. The 3 nm state seaward line is NOT in this
product; it belongs to the Submerged Lands Act boundaries, a separate
dataset. fetch_limits.py fetches it too, and a file whose name contains
"submerged", "3nm" or "sla" is picked up as that line. When it is missing
the analysis says so instead of quietly answering a question about two
limits when three were asked about.

THE CONTROL THAT DECIDES WHAT A RESULT MEANS

The same bands are computed for the vessels AIS DID explain. Reporting
traffic feels the same lanes, fishing grounds and leases as everything else
but by definition is not hiding, so if it shows the same pattern, the
pattern is about where ships are. Only a difference between the two curves
is about who is dark.

INPUTS

    data/events/candidates-scored.geojson   from ais_coverage.py
    data/events/sar-*.geojson               for the searched grids
    data/reference/limits/*                 from fetch_limits.py
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.adapters.maritime.limits import (LineSet, read_lines,
                                             read_shapefile_records)
from angels.adapters.maritime.searched import LEVELS, SEARCHED, SearchedArea
from angels.config import AOI_SEA, EVENTS, REFERENCE

LIMITS = REFERENCE / "limits"
NM_M = 1852.0

SLA_NAME = "3 nm state seaward limit"
TS_NAME = "12 nm territorial sea"
CZ_NAME = "24 nm contiguous zone"
EEZ_NAME = "200 nm EEZ"
ANY_NAME = "any limit"

# Distance-to-nearest-limit bands, in nautical miles. The first is the one
# the question is about; the rest are the comparison it needs. Fixed here,
# before the data is looked at.
BANDS_NM = (0.0, 1.0, 2.0, 5.0, 10.0, 25.0, float("inf"))


def band_of(d_m: float) -> str:
    if d_m == float("inf"):
        return "> 25 nm"
    d = d_m / NM_M
    for lo, hi in zip(BANDS_NM, BANDS_NM[1:]):
        if lo <= d < hi:
            return f"{lo:g}-{hi:g} nm" if hi != float("inf") else "> 25 nm"
    return "> 25 nm"


BAND_NAMES = [f"{lo:g}-{hi:g} nm" for lo, hi in zip(BANDS_NM, BANDS_NM[1:])
              if hi != float("inf")] + ["> 25 nm"]

# Signed bands, in nautical miles, landward negative. Narrow at the line,
# because a step is a local thing; fixed here before any data is seen.
SIGNED_EDGES_NM = (-25.0, -10.0, -5.0, -2.0, -1.0, 0.0, 1.0, 2.0, 5.0, 10.0,
                   25.0)
SIGNED_NAMES = (["landward > 25 nm"]
                + [f"{lo:g} to {hi:g} nm" for lo, hi in
                   zip(SIGNED_EDGES_NM, SIGNED_EDGES_NM[1:])]
                + ["seaward > 25 nm"])
# Separation between the 12 nm and 24 nm lines: the geometry the sign uses.
COMPANION_SEPARATION_NM = 12.0


def signed_band(d_self_m: float, d_comp_m: float, *, outer_companion: bool,
                separation_nm: float = COMPANION_SEPARATION_NM) -> str:
    """Which side of a limit a point is on, and how far, in nm.

    The companion line does the work. Take the 12 nm territorial sea, whose
    companion (the 24 nm zone) lies 12 nm OUTSIDE it:

        landward of 12 nm   the 24 nm line is further away than 12 nm AND
                            further than the 12 nm line is
        between them        the 24 nm line is within 12 nm
        beyond 24 nm        the 24 nm line is the nearer of the two

    For a limit whose companion lies INSIDE it the mirror needs a second
    clause, and the first version did not have it. "Seaward of the 24 nm
    line when the 12 nm line is at least 12 nm away" is true for a point at
    the head of the Chesapeake, which is 28 nm INSIDE the territorial sea:
    the 12 nm line runs across the mouth of the bay, not around its shore.
    That put 788 bay candidates in the outermost seaward band and made the
    24 nm test look significant when it was the inshore gradient wearing a
    different label. So seaward also requires the inner line to be NEARER
    than this one, which it only is out at sea.
    """
    sep_m = separation_nm * NM_M
    if outer_companion:
        seaward = d_comp_m <= sep_m or d_comp_m < d_self_m
    else:
        seaward = d_comp_m >= sep_m and d_comp_m > d_self_m
    d_nm = (d_self_m / NM_M) * (1 if seaward else -1)
    if d_nm <= SIGNED_EDGES_NM[0]:
        return SIGNED_NAMES[0]
    if d_nm >= SIGNED_EDGES_NM[-1]:
        return SIGNED_NAMES[-1]
    for i, (lo, hi) in enumerate(zip(SIGNED_EDGES_NM, SIGNED_EDGES_NM[1:])):
        if lo <= d_nm < hi:
            return SIGNED_NAMES[i + 1]
    return SIGNED_NAMES[-1]


def limit_sets(bbox=None, margin_deg: float = 2.0) -> dict[str, LineSet]:
    """Named LineSets from data/reference/limits, one per limit type.

    NOAA's shapefile flags each record with TS / CZ / EEZ; a record can
    carry more than one, and the same line then belongs to both sets. Files
    without attributes (a GeoJSON export, say) become one set named after
    the file -- unlabelled, and labelled as such.

    bbox trims to the study area plus a margin, which is the difference
    between 182,591 national segments and a few thousand local ones on
    every single query.
    """
    files = sorted(p for p in LIMITS.glob("*")
                   if p.suffix.lower() in (".geojson", ".json", ".shp"))
    if not files:
        return {}

    def inside(run) -> bool:
        if bbox is None:
            return True
        lomin, lamin, lomax, lamax = bbox
        return any(lomin - margin_deg <= x <= lomax + margin_deg
                   and lamin - margin_deg <= y <= lamax + margin_deg
                   for x, y in run)

    groups: dict[str, list] = defaultdict(list)
    for path in files:
        try:
            records = (read_shapefile_records(path)
                       if path.suffix.lower() == ".shp"
                       else [({}, read_lines(path))])
        except Exception as exc:                      # noqa: BLE001
            print(f"    {path.name}: skipped -- {exc}")
            continue

        for attrs, runs in records:
            runs = [r for r in runs if inside(r)]
            if not runs:
                continue
            names = [label for flag, label in (("TS", TS_NAME),
                                               ("CZ", CZ_NAME),
                                               ("EEZ", EEZ_NAME))
                     if _truthy(attrs.get(flag))]
            if (attrs.get("FEAT_TYPE") or "").lower() == "land boundary":
                continue
            if not names:
                low = path.stem.lower()
                if "submerged" in low or "3nm" in low or "sla" in low:
                    names = [SLA_NAME]
                else:
                    names = [path.stem if not attrs else "other limit"]
            for n in names:
                groups[n].extend(runs)

    out = {name: LineSet(runs, name) for name, runs in groups.items()}
    if out:
        every: list = []
        for runs in groups.values():
            every.extend(runs)
        out[ANY_NAME] = LineSet(every, ANY_NAME)
    return out


def _truthy(v) -> bool:
    """DBF numeric flags arrive as '1.00000000' or '0.00000000'."""
    try:
        return float(str(v).strip() or 0) != 0
    except ValueError:
        return False


def scene_cells(area: SearchedArea):
    """(lon, lat, km2) for every searched cell of one scene."""
    lat_km = area.cell_deg * 111.195
    for (col, row), level in area.cells.items():
        if level / LEVELS < SEARCHED:
            continue
        lat = area.lat0 + (row + 0.5) * area.cell_deg
        lon = area.lon0 + (col + 0.5) * area.cell_deg
        lon_km = area.cell_deg * 111.195 * math.cos(math.radians(lat))
        yield lon, lat, lat_km * lon_km * (level / LEVELS)


def permutation_p(observed: dict[str, int], per_pass: list[tuple[int, dict]],
                  trials: int, seed: int = 20240621) -> tuple[float, float]:
    """Chi-square statistic against a null that re-throws each pass's own
    candidates into its own searched water. Returns (statistic, p)."""
    def chi2(counts: dict[str, float], expected: dict[str, float]) -> float:
        return sum((counts.get(b, 0) - expected[b]) ** 2 / expected[b]
                   for b in expected if expected[b] > 0)

    expected: dict[str, float] = defaultdict(float)
    for n, weights in per_pass:
        total = sum(weights.values())
        if total <= 0:
            continue
        for b, w in weights.items():
            expected[b] += n * w / total
    stat = chi2({b: observed.get(b, 0) for b in expected}, expected)

    rng = random.Random(seed)
    worse = 0
    for _ in range(trials):
        sim: dict[str, float] = defaultdict(float)
        for n, weights in per_pass:
            bands = list(weights)
            w = [weights[b] for b in bands]
            if sum(w) <= 0:
                continue
            for b in rng.choices(bands, weights=w, k=n):
                sim[b] += 1
        if chi2(sim, expected) >= stat:
            worse += 1
    return stat, (worse + 1) / (trials + 1)


def shift_p(points_by_pass, area_by_pass, band_fn, searched_fn,
            expected: dict[str, float], stat: float, trials: int,
            max_shift_deg: float = 0.6, seed: int = 20240621
            ) -> tuple[float, int]:
    """p-value from sliding each pass's whole pattern, clustering intact.

    THE NULL THAT CLUSTERED DATA NEEDS. Scattering points independently
    treats 300 candidates in twenty weather patches as 300 independent
    draws, and any structure at all then looks astronomically unlikely. A
    shift keeps every clump exactly as it is and only moves it relative to
    the lines, which is the relationship being tested.

    Points that land outside that pass's searched water are dropped, and a
    shift that loses more than a third of them is redrawn: the comparison
    must stay inside the same water, not wander onto land.
    """
    def chi2(counts: dict[str, float]) -> float:
        return sum((counts.get(b, 0) - expected[b]) ** 2 / expected[b]
                   for b in expected if expected[b] > 0)

    rng = random.Random(seed)
    worse = 0
    used = 0
    for _ in range(trials):
        sim: dict[str, float] = defaultdict(float)
        kept_any = False
        for date, pts in points_by_pass.items():
            if not pts or date not in area_by_pass:
                continue
            for _attempt in range(25):
                dx = rng.uniform(-max_shift_deg, max_shift_deg)
                dy = rng.uniform(-max_shift_deg, max_shift_deg)
                moved = [(lon + dx, lat + dy) for lon, lat in pts]
                inside = [q for q in moved if searched_fn(date, *q)]
                if len(inside) >= 0.67 * len(pts):
                    break
            else:
                continue
            kept_any = True
            for lon, lat in inside:
                b = band_fn(lon, lat)
                if b is not None:
                    sim[b] += 1
        if not kept_any:
            continue
        used += 1
        # Scale the shifted counts back to the observed total, so a shift
        # that lost a few points off the edge does not look like a smaller
        # chi-square for that reason alone.
        n_sim = sum(sim.values())
        n_obs = sum(expected.values())
        if n_sim > 0 and n_obs > 0:
            sim = {b: v * n_obs / n_sim for b, v in sim.items()}
        if chi2(sim) >= stat:
            worse += 1
    return (worse + 1) / (used + 1), used


def gated_detections(path: Path, min_snr: float, min_pixels: int):
    """(date, matched, unmatched) point lists for one scene's detections.

    Matched means "some AIS track explained it" -- the exact complement of
    what went into the dark- file. It is the control group: ships that were
    where they said they were.
    """
    doc = json.loads(path.read_text(encoding="utf-8"))
    feats = [f for f in doc.get("features", [])
             if f["properties"].get("snr", 0) >= min_snr
             and f["properties"].get("pixels", 0) >= min_pixels]
    if not feats:
        return None, [], []
    date = feats[0]["properties"]["t"][:10]

    dark = EVENTS / f"dark-{path.name[len('sar-'):]}"
    if not dark.exists():
        return date, [], []
    unmatched = {(round(f["geometry"]["coordinates"][0], 5),
                  round(f["geometry"]["coordinates"][1], 5))
                 for f in json.loads(dark.read_text(encoding="utf-8"))
                 .get("features", [])}
    m, u = [], []
    for f in feats:
        lon, lat = f["geometry"]["coordinates"]
        (u if (round(lon, 5), round(lat, 5)) in unmatched else m).append(
            (lon, lat))
    return date, m, u


class SignedBander:
    """Signed distance-to-band for one limit, using its companion for side."""

    def __init__(self, lines: LineSet, companion: LineSet, *,
                 outer_companion: bool) -> None:
        self.lines, self.companion = lines, companion
        self.outer = outer_companion
        self._cache: dict[tuple[int, int], str] = {}

    def band(self, lon: float, lat: float) -> str:
        key = (int(round(lon * 100)), int(round(lat * 100)))
        hit = self._cache.get(key)
        if hit is None:
            hit = signed_band(self.lines.distance_m(lon, lat),
                              self.companion.distance_m(lon, lat),
                              outer_companion=self.outer)
            self._cache[key] = hit
        return hit


class Bander:
    """Distance-to-band, with the answer remembered per grid cell.

    The twelve passes retrace the same ground, and their slices overlap, so
    the same 0.01 deg cell is asked about many times. Caching turns a few
    million distance queries into a few hundred thousand.
    """

    def __init__(self, lines: LineSet) -> None:
        self.lines = lines
        self._cache: dict[tuple[int, int], str] = {}

    def band(self, lon: float, lat: float) -> str:
        key = (int(round(lon * 100)), int(round(lat * 100)))
        hit = self._cache.get(key)
        if hit is None:
            hit = band_of(self.lines.distance_m(lon, lat))
            self._cache[key] = hit
        return hit


def table(name: str, area: dict[str, float], obs: dict[str, int],
          expected: dict[str, float], control: dict[str, int],
          control_expected: dict[str, float], names=None) -> None:
    print(f"\n  {name}")
    print(f"    {'band':>18}{'searched km2':>14}{'cands':>8}{'per 1k':>8}"
          f"{'obs/exp':>9}   {'AIS-seen':>9}{'obs/exp':>9}")
    for b in (names or BAND_NAMES):
        a, o, e = area.get(b, 0.0), obs.get(b, 0), expected.get(b, 0.0)
        c, ce = control.get(b, 0), control_expected.get(b, 0.0)
        if a <= 0 and o == 0 and c == 0:
            continue
        dens = 1000 * o / a if a else float("nan")
        r = o / e if e else float("nan")
        cr = c / ce if ce else float("nan")
        print(f"    {b:>18}{a:14,.0f}{o:8,}{dens:8.2f}{r:9.2f}   "
              f"{c:9,}{cr:9.2f}")


def expectation(per_pass) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for n, weights in per_pass:
        total = sum(weights.values())
        if total > 0:
            for b, w in weights.items():
                out[b] += n * w / total
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--reception", default="heard",
                    help="comma-separated reception classes to count "
                         "(default: heard only)")
    ap.add_argument("--trials", type=int, default=2000,
                    help="re-throws for the scattered null")
    ap.add_argument("--shift-trials", type=int, default=400,
                    help="shifts for the cluster-preserving null")
    ap.add_argument("--stride", type=int, default=1,
                    help="use every Nth searched cell (a speed dial; the "
                         "denominator is scaled, not biased)")
    ap.add_argument("--min-snr", type=float, default=15.0)
    ap.add_argument("--min-pixels", type=int, default=6)
    ap.add_argument("--near-nm", type=float, default=None,
                    help="signed runs only: keep just the water within this "
                         "many nm of the line, on either side. THE LOCAL "
                         "STEP TEST -- see the docstring.")
    args = ap.parse_args()
    keep = {s.strip() for s in args.reception.split(",") if s.strip()}

    print("\n  limits")
    sets = limit_sets(bbox=AOI_SEA)
    if not sets:
        print(f"\n  No limit lines in {LIMITS}.")
        print("  Run: python scripts/fetch_limits.py\n")
        return 1
    for name, ls in sorted(sets.items()):
        print(f"    {name:28} {len(ls):7,} segments near the study area")
    if SLA_NAME not in sets:
        print(f"\n    NO 3 nm STATE LINE. The question names 3, 12 and 24 nm;"
              f" this run covers\n    12 and 24. Run scripts/fetch_limits.py,"
              f" or state the gap in the methods.")

    cand_path = EVENTS / "candidates-scored.geojson"
    if not cand_path.exists():
        cand_path = EVENTS / "candidates.geojson"
    if not cand_path.exists():
        print(f"\n  No candidate file in {EVENTS}.\n")
        return 1
    doc = json.loads(cand_path.read_text(encoding="utf-8"))
    cands = [(f["geometry"]["coordinates"][0], f["geometry"]["coordinates"][1],
              f["properties"].get("date", "?"), f)
             for f in doc.get("features", [])
             if not keep or f["properties"].get("reception", "heard") in keep]
    print(f"\n  {len(cands):,} candidates from {cand_path.name} "
          f"({', '.join(sorted(keep))})")

    # The control: detections AIS explained, from the same gated set.
    control_pts: list[tuple[float, float, str]] = []
    scenes: list[tuple[str, SearchedArea]] = []
    for p in sorted(EVENTS.glob("sar-*.geojson")):
        meta = json.loads(p.read_text(encoding="utf-8")).get("properties", {})
        area = SearchedArea.from_json(meta.get("searched_grid"))
        date, matched, _ = gated_detections(p, args.min_snr, args.min_pixels)
        if date is None or area is None:
            continue
        scenes.append((date, area))
        control_pts += [(lon, lat, date) for lon, lat in matched]
    print(f"  {len(control_pts):,} AIS-explained detections as the control")
    print(f"  {len(scenes)} scene(s) with a searched grid\n")

    areas_by_date: dict[str, list[SearchedArea]] = defaultdict(list)
    for date, area in scenes:
        areas_by_date[date].append(area)

    def searched(date: str, lon: float, lat: float) -> bool:
        return any(a.contains(lon, lat) for a in areas_by_date.get(date, ()))

    cand_by_pass: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for lon, lat, date, _f in cands:
        cand_by_pass[date].append((lon, lat))
    ctl_by_pass: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for lon, lat, date in control_pts:
        ctl_by_pass[date].append((lon, lat))

    companions = {TS_NAME: (CZ_NAME, True), CZ_NAME: (TS_NAME, False)}
    results: dict[str, dict] = {}

    def analyse(title: str, bander, names: list[str], store: dict,
                near_nm: float | None = None) -> None:
        keep_bands = set(names)
        if near_nm is not None:
            keep_bands = {b for b in names
                          if b in SIGNED_NAMES[1:-1]
                          and abs(float(b.split()[0])) <= near_nm + 1e-9
                          and abs(float(b.split()[-2])) <= near_nm + 1e-9}
            names = [b for b in names if b in keep_bands]

        def band_or_none(lon, lat):
            b = bander.band(lon, lat)
            return b if b in keep_bands else None
        obs: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        ctl: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for date, pts in cand_by_pass.items():
            for lon, lat in pts:
                b = band_or_none(lon, lat)
                if b is not None:
                    obs[date][b] += 1
        for date, pts in ctl_by_pass.items():
            for lon, lat in pts:
                b = band_or_none(lon, lat)
                if b is not None:
                    ctl[date][b] += 1

        area_by_pass: dict[str, dict[str, float]] = defaultdict(
            lambda: defaultdict(float))
        for date, area in scenes:
            for i, (lon, lat, km2) in enumerate(scene_cells(area)):
                if i % args.stride:
                    continue
                b = band_or_none(lon, lat)
                if b is not None:
                    area_by_pass[date][b] += km2 * args.stride

        area_total: dict[str, float] = defaultdict(float)
        obs_total: dict[str, int] = defaultdict(int)
        ctl_total: dict[str, int] = defaultdict(int)
        for d, bands in area_by_pass.items():
            for b, a_ in bands.items():
                area_total[b] += a_
        for d in obs:
            for b, n in obs[d].items():
                obs_total[b] += n
        for d in ctl:
            for b, n in ctl[d].items():
                ctl_total[b] += n

        per_pass = [(sum(obs[d].values()), dict(area_by_pass[d]))
                    for d in obs if area_by_pass.get(d)]
        per_pass_ctl = [(sum(ctl[d].values()), dict(area_by_pass[d]))
                        for d in ctl if area_by_pass.get(d)]
        exp, exp_ctl = expectation(per_pass), expectation(per_pass_ctl)

        table(title, area_total, obs_total, exp, ctl_total, exp_ctl, names)
        stat, p_iid = permutation_p(obs_total, per_pass, args.trials)
        stat_c, p_iid_c = permutation_p(ctl_total, per_pass_ctl, args.trials)
        p_shift, used = shift_p(cand_by_pass, area_by_pass, band_or_none,
                                searched, exp, stat, args.shift_trials)
        p_shift_c, _ = shift_p(ctl_by_pass, area_by_pass, band_or_none,
                               searched, exp_ctl, stat_c, args.shift_trials)
        print(f"    candidates      chi2 {stat:8.1f}   scattered p = "
              f"{p_iid:.4f}   SHIFTED p = {p_shift:.4f}")
        print(f"    AIS-seen ships  chi2 {stat_c:8.1f}   scattered p = "
              f"{p_iid_c:.4f}   SHIFTED p = {p_shift_c:.4f}")
        if p_shift > 0.05:
            print("    Nothing here survives moving the pattern around: the "
                  "clustering explains it.")
        elif p_shift_c <= 0.05:
            print("    Both survive the shift -- compare the two obs/exp "
                  "columns; only a DIFFERENCE is about reporting.")
        else:
            print("    Candidates survive the shift and AIS-seen ships do "
                  "not. That is the shape a\n    real reporting effect "
                  "would have.")

        store.update({
            "searched_km2_by_band": {b: round(area_total.get(b, 0.0), 1)
                                     for b in names},
            "candidates_by_band": {b: obs_total.get(b, 0) for b in names},
            "expected_by_band": {b: round(exp.get(b, 0.0), 2) for b in names},
            "ais_seen_by_band": {b: ctl_total.get(b, 0) for b in names},
            "ais_seen_expected": {b: round(exp_ctl.get(b, 0.0), 2)
                                  for b in names},
            "chi2": round(stat, 2), "p_scattered": p_iid, "p_shift": p_shift,
            "shift_trials_used": used,
            "chi2_control": round(stat_c, 2), "p_control_scattered": p_iid_c,
            "p_control_shift": p_shift_c,
            "by_pass": {d: {"candidates": dict(obs[d]),
                            "ais_seen": dict(ctl.get(d, {})),
                            "searched_km2": {k: round(v, 1) for k, v
                                             in area_by_pass.get(d, {}).items()}}
                        for d in sorted(obs)},
        })

    for name in [ANY_NAME] + sorted(n for n in sets if n != ANY_NAME):
        results[name] = {}
        analyse(name, Bander(sets[name]), BAND_NAMES, results[name])

        comp = companions.get(name)
        if comp and comp[0] in sets:
            other, outer = comp
            signed = SignedBander(sets[name], sets[other],
                                  outer_companion=outer)
            results[f"{name} (signed)"] = {}
            analyse(f"{name}  -- landward / seaward", signed,
                    list(SIGNED_NAMES), results[f"{name} (signed)"])
            if args.near_nm:
                key = f"{name} (within {args.near_nm:g} nm)"
                results[key] = {}
                analyse(f"{name}  -- STEP TEST, within "
                        f"{args.near_nm:g} nm of the line", signed,
                        list(SIGNED_NAMES), results[key],
                        near_nm=args.near_nm)

    out = EVENTS / "boundary-bands.json"
    out.write_text(json.dumps({
        "bands_nm": [b for b in BANDS_NM if b != float("inf")],
        "reception_kept": sorted(keep),
        "candidate_file": cand_path.name,
        "gate": {"min_snr": args.min_snr, "min_pixels": args.min_pixels},
        "trials": args.trials, "stride": args.stride,
        "note": ("The 3 nm state seaward line is not in NOAA's Maritime "
                 "Limits product; these are the 12, 24 and 200 nm lines."),
        "limits": results,
    }, indent=1), encoding="utf-8")
    (EVENTS / "candidates-banded.geojson").write_text(json.dumps(doc, indent=1),
                                                      encoding="utf-8")
    print(f"\n  wrote {out.name} and candidates-banded.geojson\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
