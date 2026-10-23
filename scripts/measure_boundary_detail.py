"""How far could a published state boundary be from where its own vertices put it?

    python scripts/measure_boundary_detail.py                  # Maryland, 500 m
    python scripts/measure_boundary_detail.py --state 51       # Virginia
    python scripts/measure_boundary_detail.py --threshold-m 250

THE QUESTION G2b CANNOT START WITHOUT

The research question asks whether non-cooperative behaviour concentrates AT
governance discontinuities, so the land domain needs a distance-to-border
threshold measured in hundreds of metres. A boundary drawn coarsely enough to
sit a kilometre from the true line would move the discontinuity itself, and a
concentration measured against it would be a fact about cartography.

TIGERweb's `State_County` layer answered the probe on 2026-10-06 with GeoJSON
over plain HTTP. Whether its geometry is generalised was unknown.

WHY THIS MEASURES THE GEOMETRY RATHER THAN COMPARING IT

The backlog's first plan was to count vertices against the Census 1:20,000,000
cartographic file. That compares one proxy for resolution with another, and
the cartographic file needs a zip and a shapefile reader this project
deliberately kept out (DECISIONS, 2026-10-06). The quantity G2b needs is
directly computable from TIGERweb's own polygon:

    for a straight chord of length L standing in for a curve that turns
    through angle theta (radians), the largest gap between them is
    the sagitta, about L * theta / 8.

Each segment's turn is shared between its two end vertices, so its estimated
deviation is L * (turn_a + turn_b) / 16. Summed over the boundary that gives a
distribution, in metres, directly comparable to the threshold.

WHAT IT MEASURES AND WHAT IT DOES NOT

It estimates how far the published polyline could sit from the SMOOTHEST curve
consistent with its own vertices. That is a lower bound on positional
uncertainty from discretisation alone. It says nothing about survey error,
datum, or whether the legal boundary is where the vertices are. A legally
straight line correctly contributes almost nothing.

THE TRAP, AND WHY THE TOOL REFUSES TO ADJUDICATE IT

Maryland is a good test because it has both kinds of sharp vertex. Legally
straight lines meet at genuine corners -- Mason-Dixon, the Transpeninsular
Line, the north-south line with Delaware -- and those turn sharply at one
vertex because the law says so. But the Potomac is a river boundary, and heavy
generalisation can collapse a meander into a single sharp vertex too.

A rule that treated every sharp turn as a legal corner and excluded it would
exclude exactly the generalisation this tool exists to detect, and it would be
wrong in the direction that makes the boundary look better. That is mechanism
S. So the distribution is reported BOTH with and without vertices turning more
than SHARP_DEG, so the gap between them is visible, and the worst segments are
listed with coordinates so a person can look at them on a map. Telling a
legal corner from a collapsed meander is a judgement for someone who can see
the map, not for a threshold.

ONE MORE THING THE SAME REQUEST ANSWERS

The 2026-10-06 probe returned `NAME` as a 28-character string, and no state
name is 28 characters. This prints its `repr()`, which settles whether it is
padding before anything joins on it.

Stdlib only. No shapely, no geopandas: the measurement must not be the thing
that pulls the analysis stack into the land domain.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Re-runs this script under the interpreter that has ANGELS installed, if the
# one invoking it does not. See scripts/_bootstrap.py.
#
# The name check matters. `import _bootstrap` only resolves when scripts/ is on
# sys.path, which is true when this file is RUN and false when the test suite
# IMPORTS it as scripts.<name>. Swallowing every ModuleNotFoundError here would
# also swallow the one _bootstrap raises about 'angels' itself.
try:
    import _bootstrap  # noqa: F401  (must precede the angels imports)
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import REFERENCE

SERVICE = ("https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
           "State_County/MapServer/0/query")
USER_AGENT = ("angels-geoint/0.1 (senior capstone, GIS; contact via "
              "github.com/Mc3urry/angels-geoint)")
TIMEOUT_S = 90.0
CAP_BYTES = 128 * 1024 * 1024

EARTH_R_M = 6_371_008.8          # mean radius; the IUGG value
SHARP_DEG = 60.0                 # see THE TRAP in the module docstring
DEFAULT_THRESHOLD_M = 500.0
WORST_N = 10

OUT_DIR = REFERENCE / "roads"


# --------------------------------------------------------------------------
# geometry, all pure
# --------------------------------------------------------------------------

def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * math.asin(min(1.0, math.sqrt(a)))


def rings_of(geom: dict[str, Any]) -> list[list[list[float]]]:
    """Every ring of a Polygon or MultiPolygon, outer and inner alike.

    Both types arrive from this service in the same response -- the
    2026-10-06 probe saw them mixed inside three features -- so both are
    handled from the first line rather than after the first error.
    """
    t = geom.get("type")
    coords = geom.get("coordinates") or []
    if t == "Polygon":
        return list(coords)
    if t == "MultiPolygon":
        return [ring for poly in coords for ring in poly]
    return []


def open_ring(ring: list[list[float]]) -> tuple[list[tuple[float, float]], int]:
    """A ring as a cycle of distinct consecutive vertices.

    GeoJSON repeats the first vertex at the end; that closing copy is dropped
    so the cycle does not count a zero-length segment. Any OTHER consecutive
    duplicates are dropped too, and counted, because a turn angle at a
    repeated vertex is undefined -- and a duplicate vertex is itself a fact
    about how the geometry was produced, so it is reported, not hidden.
    """
    pts = [(float(p[0]), float(p[1])) for p in ring]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    out: list[tuple[float, float]] = []
    dupes = 0
    for p in pts:
        if out and p == out[-1]:
            dupes += 1
            continue
        out.append(p)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
        dupes += 1
    return out, dupes


def _local_xy(origin: tuple[float, float],
              p: tuple[float, float]) -> tuple[float, float]:
    """Equirectangular metres around `origin`. Accurate for the short
    distances between neighbouring vertices, which is all it is used for."""
    lon0, lat0 = origin
    x = math.radians(p[0] - lon0) * math.cos(math.radians(lat0)) * EARTH_R_M
    y = math.radians(p[1] - lat0) * EARTH_R_M
    return x, y


def turn_rad(a: tuple[float, float], b: tuple[float, float],
             c: tuple[float, float]) -> float:
    """Absolute change of direction at b, travelling a -> b -> c, in radians.

    0 means straight on; pi means doubling straight back.
    """
    ax, ay = _local_xy(b, a)
    cx, cy = _local_xy(b, c)
    ux, uy = -ax, -ay                 # direction a -> b
    vx, vy = cx, cy                   # direction b -> c
    nu, nv = math.hypot(ux, uy), math.hypot(vx, vy)
    if nu == 0 or nv == 0:
        return 0.0
    cosang = max(-1.0, min(1.0, (ux * vx + uy * vy) / (nu * nv)))
    return math.acos(cosang)


def segments(ring: list[list[float]]) -> tuple[list[dict[str, float]], int]:
    """Every segment of one ring with its length, end turns and deviation.

    deviation_m = length * (turn_at_start + turn_at_end) / 16. See the module
    docstring for the derivation, and for what it does and does not mean.
    """
    pts, dupes = open_ring(ring)
    n = len(pts)
    if n < 3:
        return [], dupes
    turns = [turn_rad(pts[i - 1], pts[i], pts[(i + 1) % n]) for i in range(n)]
    out = []
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        length = haversine_m(a[0], a[1], b[0], b[1])
        ta, tb = turns[i], turns[(i + 1) % n]
        out.append({
            "length_m": length,
            "turn_a_deg": math.degrees(ta),
            "turn_b_deg": math.degrees(tb),
            "deviation_m": length * (ta + tb) / 16.0,
            "mid_lon": (a[0] + b[0]) / 2,
            "mid_lat": (a[1] + b[1]) / 2,
        })
    return out, dupes


def quantiles(values: list[float]) -> dict[str, float] | None:
    """p10, median, p90, p99 and the extremes, by nearest rank.

    None for an empty list. A distribution of nothing is not a distribution of
    zeros, and the caller must be able to tell.
    """
    if not values:
        return None
    s = sorted(values)
    n = len(s)

    def q(p: float) -> float:
        return s[min(n - 1, max(0, math.ceil(p * n) - 1))]

    return {"n": n, "min": s[0], "p10": q(0.10), "median": q(0.50),
            "p90": q(0.90), "p99": q(0.99), "max": s[-1]}


def is_sharp(seg: dict[str, float], sharp_deg: float = SHARP_DEG) -> bool:
    return seg["turn_a_deg"] > sharp_deg or seg["turn_b_deg"] > sharp_deg


def measure(features: list[dict[str, Any]],
            sharp_deg: float = SHARP_DEG) -> dict[str, Any]:
    """Everything this tool reports about one set of features."""
    segs: list[dict[str, float]] = []
    rings = vertices = dupes = 0
    for f in features:
        for ring in rings_of(f.get("geometry") or {}):
            rings += 1
            got, d = segments(ring)
            dupes += d
            vertices += len(got)
            segs.extend(got)

    smooth = [s for s in segs if not is_sharp(s, sharp_deg)]
    worst = sorted(segs, key=lambda s: -s["deviation_m"])[:WORST_N]
    return {
        "rings": rings,
        "vertices": vertices,
        "duplicate_vertices_dropped": dupes,
        "perimeter_km": sum(s["length_m"] for s in segs) / 1000,
        "segment_length_m": quantiles([s["length_m"] for s in segs]),
        "deviation_m_all": quantiles([s["deviation_m"] for s in segs]),
        "deviation_m_without_sharp": quantiles([s["deviation_m"] for s in smooth]),
        "sharp_deg": sharp_deg,
        "segments_touching_sharp_vertex": len(segs) - len(smooth),
        "worst": [{k: round(v, 6 if "lon" in k or "lat" in k else 1)
                   for k, v in s.items()} for s in worst],
    }


# --------------------------------------------------------------------------
# what it means
# --------------------------------------------------------------------------

def verdict(m: dict[str, Any], threshold_m: float) -> str:
    """One sentence, five cases, five sentences (rule 13).

    Judged on the distribution INCLUDING sharp vertices, which is the
    conservative one. When excluding them would change the answer, the
    sentence says so, because that gap is exactly the corner-or-meander
    question the module docstring declines to settle by rule.
    """
    allq = m.get("deviation_m_all")
    if not allq:
        return "no geometry came back, so nothing was measured"
    p99 = allq["p99"]
    soft = m.get("deviation_m_without_sharp") or allq
    gap = (f" Excluding the {m['segments_touching_sharp_vertex']} segments at "
           f"vertices sharper than {m['sharp_deg']:.0f} degrees gives "
           f"{soft['p99']:.0f} m instead; look at the worst list on a map "
           f"before choosing which to believe.")
    if p99 <= threshold_m / 10:
        return (f"discretisation is not the limiting factor: the 99th "
                f"percentile deviation is {p99:.0f} m against a "
                f"{threshold_m:.0f} m threshold, a tenth of it or less.")
    if p99 <= threshold_m:
        return (f"usable with care: the 99th percentile deviation is "
                f"{p99:.0f} m, inside the {threshold_m:.0f} m threshold but "
                f"not negligible against it, and the worst "
                f"{allq['max']:.0f} m is where a border effect could be "
                f"cartographic.")
    if soft["p99"] <= threshold_m:
        return (f"too coarse at the 99th percentile ({p99:.0f} m against "
                f"{threshold_m:.0f} m) ONLY because of sharp vertices, which "
                f"are either legal corners or collapsed meanders." + gap)
    return (f"too coarse for a {threshold_m:.0f} m threshold: the 99th "
            f"percentile deviation is {p99:.0f} m with sharp vertices and "
            f"{soft['p99']:.0f} m without them, so the threshold is measuring "
            f"the map before it measures the roads.")


def name_report(name: Any) -> dict[str, Any]:
    """Settles the 28-character NAME question from 2026-10-06.

    A padded name will not equal "Maryland" in a join, and the failure is a
    join that matches nothing rather than one that raises.
    """
    s = "" if name is None else str(name)
    return {"repr": repr(s), "length": len(s), "stripped_length": len(s.strip()),
            "padded": s != s.strip()}


# --------------------------------------------------------------------------
# the network, which is the only impure part
# --------------------------------------------------------------------------

def query_url(state_fips: str) -> str:
    """Full geometry, in WGS84, with nothing asked of the server that could
    simplify it. `outSR=4326` is stated rather than assumed, and
    `maxAllowableOffset` is deliberately absent: passing it would ask the
    server to generalise, which is the property being measured."""
    params = {"where": f"STATE='{state_fips}'", "outFields": "STATE,NAME",
              "returnGeometry": "true", "outSR": "4326", "f": "geojson"}
    return SERVICE + "?" + urllib.parse.urlencode(params)


def fetch(url: str) -> tuple[int, bytes, float]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        body = r.read(CAP_BYTES + 1)
        if len(body) > CAP_BYTES:
            raise SystemExit(f"\n  Response exceeded {CAP_BYTES // 2**20} MiB; "
                             f"refusing to measure a truncated polygon.\n")
        return r.status, body, time.monotonic() - t0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--state", default="24",
                    help="two-digit FIPS code, as a STRING (default 24, Maryland)")
    ap.add_argument("--threshold-m", type=float, default=DEFAULT_THRESHOLD_M,
                    help="the border distance G2b intends to use")
    ap.add_argument("--sharp-deg", type=float, default=SHARP_DEG)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    # FIPS codes are zero-padded strings. "1" is not Alabama; "01" is.
    if not (len(args.state) == 2 and args.state.isdigit()):
        raise SystemExit(f"\n  --state must be two digits, got {args.state!r}. "
                         f"FIPS codes are zero-padded strings.\n")

    url = query_url(args.state)
    print(f"\n  TIGERweb State_County, STATE='{args.state}'")
    try:
        status, body, secs = fetch(url)
    except urllib.error.HTTPError as exc:
        print(f"  HTTP {exc.code}. A status is a fact; its cause is not, so "
              f"no cause is guessed here.\n")
        return 1
    except urllib.error.URLError as exc:
        print(f"  could not reach the service: {exc.reason}\n")
        return 1
    print(f"  {status}, {len(body):,} bytes in {secs:.1f} s")

    obj = json.loads(body.decode("utf-8"))
    feats = obj.get("features") or []
    print(f"  features: {len(feats)}")
    if not feats:
        print(f"  {verdict({}, args.threshold_m)}\n")
        return 1
    if obj.get("exceededTransferLimit"):
        print("  WARNING: the service says it returned a PAGE, not the whole "
              "layer. A partial polygon is not measured as a whole one.\n")
        return 1

    for f in feats:
        nr = name_report((f.get("properties") or {}).get("NAME"))
        state = (f.get("properties") or {}).get("STATE")
        print(f"  NAME {nr['repr']}  length {nr['length']}, stripped "
              f"{nr['stripped_length']}, padded: {nr['padded']}   "
              f"STATE {state!r}")

    m = measure(feats, args.sharp_deg)
    sl, dv, ds = m["segment_length_m"], m["deviation_m_all"], m["deviation_m_without_sharp"]
    print(f"\n  {m['rings']} ring(s), {m['vertices']:,} vertices, "
          f"perimeter {m['perimeter_km']:,.1f} km, "
          f"{m['duplicate_vertices_dropped']} duplicate vertices dropped")
    print(f"  segment length  m   median {sl['median']:8.1f}   p90 {sl['p90']:8.1f}   "
          f"p99 {sl['p99']:8.1f}   max {sl['max']:9.1f}")
    print(f"  deviation, all  m   median {dv['median']:8.2f}   p90 {dv['p90']:8.2f}   "
          f"p99 {dv['p99']:8.2f}   max {dv['max']:9.2f}")
    if ds:
        print(f"  deviation, w/o sharp m median {ds['median']:5.2f}   p90 {ds['p90']:8.2f}   "
              f"p99 {ds['p99']:8.2f}   max {ds['max']:9.2f}")
    print(f"  {m['segments_touching_sharp_vertex']} segment(s) touch a vertex "
          f"sharper than {m['sharp_deg']:.0f} degrees")

    print(f"\n  the {len(m['worst'])} largest estimated deviations -- look at "
          f"these on a map:")
    for w in m["worst"]:
        print(f"    {w['deviation_m']:8.1f} m   segment {w['length_m']:8.1f} m   "
              f"turns {w['turn_a_deg']:5.1f} / {w['turn_b_deg']:5.1f} deg   "
              f"at {w['mid_lat']:.5f}, {w['mid_lon']:.5f}")

    print(f"\n  VERDICT at {args.threshold_m:.0f} m: {verdict(m, args.threshold_m)}\n")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    geo = args.out_dir / f"tigerweb-state-{args.state}-{stamp}.geojson"
    rep = args.out_dir / f"boundary-detail-{args.state}-{stamp}.json"
    geo.write_bytes(body)
    rep.write_text(json.dumps({
        "_what": "discretisation deviation of a TIGERweb state boundary",
        "_url": url, "_fetched_at_utc": stamp, "_bytes": len(body),
        "state": args.state, "threshold_m": args.threshold_m,
        "verdict": verdict(m, args.threshold_m), **m,
    }, indent=1) + "\n", encoding="utf-8")
    print(f"  wrote {geo.name} and {rep.name} under {args.out_dir}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
