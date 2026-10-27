"""G2b: which transit feeds operate across, or right beside, a state line.

    python scripts/select_border_feeds.py
    python scripts/select_border_feeds.py --buffer-m 3000
    python scripts/select_border_feeds.py --dry-run

Reads the G0 transit registry and the state polygons that
`scripts/fetch_state_boundaries.py` saved, and writes
`data/reference/roads/border-feeds.json`: the feeds G2c will poll, each with
the states it touches, and every feed it will not poll, each with its reason.

WHY THIS NARROWING EXISTS

191 vehicle-position feeds polled every 30 s is 7.0 GiB a day, measured. The
research question is about governance DISCONTINUITIES, and an agency whose
whole service area sits inside one state has no discontinuity to be near. So
the land domain polls the ones that straddle or abut a state line, and the
selection is made from geometry rather than from a catalogue label -- the first
row of the registry is Metro St. Louis, filed under subdivision "Illinois",
whose box runs straight across the Mississippi into Missouri.

THE TEST, AND WHY IT IS NOT "TOUCHES A BOUNDARY LINE"

A feed is a border agency when its bounding box, buffered by BUFFER_M, overlaps
TWO OR MORE state polygons. Testing against boundary LINES would have to tell
a shared border from a coastline, which is a geometry problem of its own;
overlap with two polygons sidesteps it. Four outcomes, never collapsed:

    two or more states   selected, with the states named
    one state            excluded: no discontinuity inside its service area
    a skipped state      excluded: inside the extent of a state left out BY
                         RULE (Alaska, Hawaii, territories) -- Anchorage
    no state at all      excluded AND FLAGGED: a bad box or a gap in the
                         boundary set, which needs looking at

The third exists because the fourth would otherwise have fired on Anchorage
every run, and a flag that always fires is a flag nobody reads.

WHY THE BUFFER IS 2 KM, AND WHY IT IS AN UPPER BOUND

The G5 pre-registration will choose the analysis threshold from the positional
accuracy of the vehicle fixes -- hundreds of metres at most, since the
2026-10-23 measurement showed the boundary itself is good to about 5 m. This
selection must never drop a feed whose vehicles could fall inside THAT
threshold, so it uses a deliberately larger one. Over-including costs storage;
under-including loses observations that cannot be recovered afterwards, because
a moment that was not polled cannot be polled later. The buffer is recorded in
the artefact so a G5 threshold larger than it is detectable rather than silent.

The registry's boxes come from each feed's static stops, so vehicles operate
inside them; a box that stops short of a line by less than the buffer still
reaches the border zone, which is the case the buffer exists for.

THE SEVEN WITH NO BOX

Geometry cannot select a feed the catalogue gave no box. They are named in
NAMED_BY_HAND with a decision and a reason each, in code, because changing
which feeds are polled is a commit and not a configuration change -- mechanism
O. Every reason is a JUDGEMENT FROM THE MUNICIPALITY, not a measurement, and
says so. A no-box feed that is not in that table is a refusal, not a silent
drop, and a table entry whose feed has since gained a box is reported so that
geometry can take the decision back.

NO NETWORK. Everything here is a pure function of two files on disk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Re-runs this script under the interpreter that has ANGELS installed, if the
# one invoking it does not. See scripts/_bootstrap.py. The name check matters:
# swallowing every ModuleNotFoundError would also swallow the one _bootstrap
# raises about 'angels' itself.
try:
    import _bootstrap  # noqa: F401  (must precede the angels imports)
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import REFERENCE
from scripts.fetch_road_registries import RegistryRefused, check_no_credentials

ROADS = REFERENCE / "roads"
REGISTRY = ROADS / "transit-registry.json"
# Named for what it is fit for. These polygons are simplified, which is right
# for selection with a 2 km buffer and WRONG for G6's border distances, which
# need full resolution. The name is there so nobody reaches for them for that.
BOUNDARIES = ROADS / "states-generalised"
OUT = ROADS / "border-feeds.json"

BUFFER_M = 2000.0
M_PER_DEG_LAT = 111_320.0

# A BOX THIS LARGE IS NOT EVIDENCE OF ANYTHING. Measured 2026-10-23 on the 184
# usable boxes: median 46 km, p90 104 km, and the largest legitimate ones are
# Denver's RTD at 333 km and Bustang, Colorado's statewide intercity bus, at
# 321 km. Then a gap -- the next two are 1,029 km (a campus shuttle in
# Rochester NY spanning twelve states) and 1,512 km (San Angelo, Texas,
# reaching Georgia). Those were each stretched by a stray stop coordinate, and
# the first real run selected both and called them "regional aggregators": a
# true observation (four or more states) with a false inference attached.
# Mechanism M.
#
# 500 km sits inside that factor-of-three gap with room on both sides. A box
# over it is treated exactly like a missing one: geometry cannot decide the
# feed, so a person must, in NAMED_BY_HAND, or the run refuses.
MAX_BOX_KM = 500.0

# Decisions for feeds the registry gave no bounding box. Each is a judgement
# from the municipality, stated as one, and reviewable as a diff.
NAMED_BY_HAND: dict[str, tuple[str, str]] = {
    "mdb-1695": (
        "include",
        "Transit Authority of Northern Kentucky, Fort Wright KY, runs across "
        "the Ohio River into downtown Cincinnati: the Kentucky-Ohio line. The "
        "clearest border agency in the registry, and the catalogue gave it no "
        "box, so geometry could never have found it."),
    "tld-7044-vp": (
        "exclude",
        "Lexington Transit Authority, Lexington KY: central Kentucky, far from "
        "any state line. Judged from the municipality."),
    "mdb-1566": (
        "exclude",
        "Beach Cities Transit, Redondo Beach CA: interior Los Angeles County. "
        "Judged from the municipality."),
    "tld-1260-vp": (
        "exclude",
        "Long Beach Transit, Long Beach CA: interior Los Angeles County. "
        "Judged from the municipality."),
    "tld-4364-vp": (
        "exclude",
        "Torrance Transit, Long Beach CA area: interior Los Angeles County. "
        "Judged from the municipality."),
    "tld-3391-vp": (
        "exclude",
        "Sacramento Regional Transit, Sacramento CA: central California. "
        "Judged from the municipality."),
    "tld-6890-vp": (
        "exclude",
        "Green Bay Metro, Green Bay WI: northeast Wisconsin, well short of the "
        "Michigan line. Judged from the municipality."),
    # These two HAVE boxes, and the boxes are impossible. See MAX_BOX_KM.
    "tld-6884-vp": (
        "exclude",
        "Rochester Institute of Technology, Rochester NY: a campus shuttle whose "
        "registry box is 1,029 km tall across twelve states, stretched by a "
        "stray stop. Rochester is not on a state line. Judged from the "
        "municipality."),
    "tld-5865-vp": (
        "exclude",
        "Concho Valley Transit District, San Angelo TX: its registry box is "
        "1,512 km wide and reaches Georgia, stretched by a stray stop. San "
        "Angelo is not on a state line. Judged from the municipality."),
}

# SAME AGENCY, MORE THAN ONE SELECTED FEED. Found by reading the first real
# selection, 2026-10-23: Virginia Railway Express and Pioneer Valley each
# appeared twice, under different ids and DIFFERENT URLS -- so a de-duplication
# keyed on the URL found zero, which is a check that compared the wrong thing.
#
# Two feeds of one agency are either different vehicles (bus and rail) or the
# same vehicles twice (an old host and a new one, or two vendors). Metadata
# cannot always tell, and the two mistakes are not symmetrical: keeping both
# when they duplicate silently double-counts every vehicle and inflates every
# rate; keeping one when they differ loses coverage, which shows up as fewer
# vehicles. So where it is unclear, ONE IS KEPT, and the choice is marked
# PROVISIONAL until vehicle ids from both are compared at the same moment.
#
# A selected same-agency group with no entry here is a refusal.
SAME_AGENCY: dict[tuple[str, ...], tuple[str, str]] = {
    ("mdb-1850", "mdb-1853"): (
        "both",
        "WMATA bus and rail: the URLs name the mode (bus-gtfsrt, rail-gtfsrt), "
        "and buses and trains are different vehicles."),
    ("mdb-3060", "mdb-3063"): (
        "both",
        "SEPTA rail and bus: the URLs name the mode (septarail, septa), and "
        "trains and buses are different vehicles."),
    ("mdb-1734", "tld-1127-vp"): (
        "keep:tld-1127-vp",
        "Virginia Railway Express from two hosts with the same feed name, "
        "VehiclePositionFeed: almost certainly the same trains twice. Kept: "
        "the agency's https host. The http one answered HTTP 403 when tried on "
        "2026-10-23, which is a fact about that request and not yet a reason. "
        "CONFIRMED 2026-10-26: polled at the same moment, both feeds carried "
        "the same 6 vehicle ids, the same labels and positions 0 m apart, in "
        "2 of 3 rounds; in the third the http host returned a body that is not "
        "GTFS-Realtime. data/reference/roads/same-agency-2026-10-26T200344Z.json."),
    ("mdb-2670", "tld-4679-vp"): (
        "keep:tld-4679-vp",
        "Pioneer Valley published twice, by Swiftly and by Avail InfoPoint: "
        "the same buses from two vendors. Kept: the agency's own host over the "
        "third-party one. PROVISIONAL until vehicle ids are compared. Tried "
        "2026-10-26: the Swiftly feed refused with an HTTP error in all 3 rounds "
        "(it is a keyed vendor API), so it cannot be compared, and it cannot be "
        "polled either -- which is also why keeping one cannot double-count."),
}

# A CATALOGUE URL THAT IS WRONG, CORRECTED HERE AND NOT IN THE CATALOGUE.
#
# The registry is a recording of the Mobility Database and stays one: editing
# it would make it a recording of nothing. So a correction is a row in this
# table, applied to the SELECTION, with the catalogue's own URL kept beside the
# polled one in the artefact. Each entry names the exact catalogue URL it
# corrects, and the build REFUSES if the catalogue now says something else --
# a correction reapplied to a URL it was never tested against is a guess.
URL_CORRECTIONS: dict[str, tuple[str, str, str]] = {
    "mdb-1921": (
        "http://gtfs-rt.c-tran.org/gtfsrealtime/VehiclePostions",
        "http://gtfs-rt.c-tran.org/gtfsrealtime/VehiclePositions",
        "C-TRAN. The catalogue spells the path 'VehiclePostions'. That URL "
        "answered HTTP 404 on all 2,516 polls from 2026-10-26 20:05 to "
        "2026-10-27 17:04 UTC. On 2026-10-27 the same URL spelled "
        "'VehiclePositions' answered HTTP 200 with 5,235 bytes, from the "
        "collecting machine. Whether that body parses is the poller's to say."),
}


def apply_url_corrections(rows: list[dict[str, Any]],
                          table: dict[str, tuple[str, str, str]] | None = None
                          ) -> list[dict[str, Any]]:
    """Rows with any correction applied; the catalogue URL is kept, never lost."""
    table = URL_CORRECTIONS if table is None else table
    out = []
    for r in rows:
        fix = table.get(r.get("id"))
        if fix is not None:
            catalogue, polled, why = fix
            if r.get("url") != catalogue:
                raise RegistryRefused(
                    f"URL_CORRECTIONS[{r['id']}] corrects {catalogue!r}, but the "
                    f"catalogue now says {r.get('url')!r}. Re-decide it rather "
                    f"than reapply it.")
            r = dict(r, url=polled, url_in_catalogue=catalogue,
                     url_corrected=why)
        out.append(r)
    return out

R_DUPLICATE = "same agency as another selected feed: one kept, see SAME_AGENCY"

R_ONE_STATE = "one state: no discontinuity inside the service area"
R_NO_STATE = "no state in the boundary set: a bad box or a gap in the boundaries"
R_BY_HAND = "no box, excluded by hand"
# FOUND BEFORE THE FIRST REAL RUN, 2026-10-23.
#
# A smoke run of all 191 real feeds turned up one that overlapped nothing:
# Anchorage People Mover. Alaska is skipped ON PURPOSE -- no land boundary with
# another state -- so with real polygons it would have landed in R_NO_STATE and
# been flagged "a bad box or a gap ... look at them", every run, forever. Its box
# is fine and the gap is deliberate. Two facts sharing one outcome is mechanism
# A, and a flag that always fires teaches the reader to skip the one line meant
# to catch a real hole in the boundary set -- rule 16.
#
# The fix is geometric, not a lookup of the registry's subdivision label: the
# fetch records the EXTENT of every state it skips by rule, and a feed that
# overlaps no fetched polygon but sits inside a skipped extent is explained.
R_SKIPPED_STATE = "in a state the boundary set skips by rule (no land neighbour)"


# --------------------------------------------------------------------------
# geometry, pure, in longitude/latitude
# --------------------------------------------------------------------------

Rect = tuple[float, float, float, float]          # min_lon, min_lat, max_lon, max_lat


def box_rect(box: dict[str, Any]) -> Rect:
    return (float(box["min_lon"]), float(box["min_lat"]),
            float(box["max_lon"]), float(box["max_lat"]))


def buffered(rect: Rect, metres: float) -> Rect:
    """Grow a lon/lat box by `metres` on every side.

    Longitude degrees shrink with latitude, so the widening uses the latitude
    nearer the pole -- the larger of the two in absolute value -- which makes
    the buffer at least `metres` everywhere along the box rather than only at
    its middle. An upper bound is the point.
    """
    x0, y0, x1, y1 = rect
    dlat = metres / M_PER_DEG_LAT
    poleward = max(abs(y0), abs(y1))
    dlon = metres / (M_PER_DEG_LAT * max(math.cos(math.radians(poleward)), 1e-6))
    return (x0 - dlon, y0 - dlat, x1 + dlon, y1 + dlat)


def rect_km(rect: Rect) -> tuple[float, float]:
    """(width, height) in km, at the box's middle latitude. Descriptive only."""
    x0, y0, x1, y1 = rect
    mid = math.radians((y0 + y1) / 2)
    return ((x1 - x0) * M_PER_DEG_LAT * math.cos(mid) / 1000,
            (y1 - y0) * M_PER_DEG_LAT / 1000)


def rings_of(geom: dict[str, Any]) -> list[list[list[float]]]:
    t, c = geom.get("type"), geom.get("coordinates") or []
    if t == "Polygon":
        return list(c)
    if t == "MultiPolygon":
        return [ring for poly in c for ring in poly]
    return []


def bounds(rings: list[list[list[float]]]) -> Rect | None:
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def rects_overlap(a: Rect, b: Rect) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _orient(ax, ay, bx, by, cx, cy) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _on_segment(ax, ay, bx, by, cx, cy) -> bool:
    return (min(ax, bx) <= cx <= max(ax, bx)
            and min(ay, by) <= cy <= max(ay, by))


def segments_meet(p1, p2, q1, q2) -> bool:
    """Do two closed segments share at least one point?"""
    d1 = _orient(*q1, *q2, *p1)
    d2 = _orient(*q1, *q2, *p2)
    d3 = _orient(*p1, *p2, *q1)
    d4 = _orient(*p1, *p2, *q2)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) \
            and d1 != 0 and d2 != 0 and d3 != 0 and d4 != 0:
        return True
    return ((d1 == 0 and _on_segment(*q1, *q2, *p1))
            or (d2 == 0 and _on_segment(*q1, *q2, *p2))
            or (d3 == 0 and _on_segment(*p1, *p2, *q1))
            or (d4 == 0 and _on_segment(*p1, *p2, *q2)))


def segment_meets_rect(x1, y1, x2, y2, rect: Rect) -> bool:
    x0, y0, xa, ya = rect
    if x0 <= x1 <= xa and y0 <= y1 <= ya:
        return True
    if x0 <= x2 <= xa and y0 <= y2 <= ya:
        return True
    corners = ((x0, y0), (xa, y0), (xa, ya), (x0, ya))
    p, q = (x1, y1), (x2, y2)
    return any(segments_meet(p, q, corners[i], corners[(i + 1) % 4])
               for i in range(4))


def point_in_rings(x: float, y: float, rings) -> bool:
    """Even-odd over every ring, so holes are holes."""
    inside = False
    for ring in rings:
        n = len(ring)
        j = n - 1
        for i in range(n):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            if (yi > y) != (yj > y):
                xcross = (xj - xi) * (y - yi) / (yj - yi) + xi
                if x < xcross:
                    inside = not inside
            j = i
    return inside


def rect_overlaps_rings(rect: Rect, rings) -> bool:
    """Does an axis-aligned box share any area with a polygon?

    One pass over the edges. If any edge reaches into the box -- an endpoint
    inside it, or a crossing of one of its sides -- they overlap; that also
    covers a polygon lying wholly inside the box. If no edge reaches in, the box
    is either wholly inside the polygon or wholly outside it, and one corner
    settles which.
    """
    x0, y0, xa, ya = rect
    for ring in rings:
        for i in range(len(ring) - 1):
            x1, y1 = ring[i][0], ring[i][1]
            x2, y2 = ring[i + 1][0], ring[i + 1][1]
            if (max(x1, x2) < x0 or min(x1, x2) > xa
                    or max(y1, y2) < y0 or min(y1, y2) > ya):
                continue
            if segment_meets_rect(x1, y1, x2, y2, rect):
                return True
    return point_in_rings(x0, y0, rings)


def states_touched(rect: Rect, states: dict[str, dict[str, Any]]) -> list[str]:
    """FIPS codes, as strings, of every state polygon the box overlaps."""
    hit = []
    for fips, s in states.items():
        if s["bounds"] is None or not rects_overlap(rect, s["bounds"]):
            continue
        if rect_overlaps_rings(rect, s["rings"]):
            hit.append(fips)
    return sorted(hit)


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def load_states(directory: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """State polygons keyed by FIPS STRING, and the fetch manifest.

    FIPS stays a zero-padded string end to end. "01" is Alabama; 1 is nothing,
    and a join keyed on it matches no row rather than raising -- the trap the
    2026-10-06 probe found in TIGERweb's own STATE column.
    """
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"\n  No boundary set at {directory}\n"
                         f"  Run: python scripts/fetch_state_boundaries.py\n")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("partial"):
        # A state left out of the set makes every feed near it overlap one
        # fewer polygon than it really does, and "one state" is then a wrong
        # answer with exactly the right shape. No selection from a partial set.
        raise RegistryRefused(
            "the boundary set was fetched with --only and is PARTIAL; every "
            "state not in it would read as absent. Fetch the full set.")
    states: dict[str, dict[str, Any]] = {}
    for fips, info in manifest["states"].items():
        gj = json.loads((directory / info["file"]).read_text(encoding="utf-8"))
        rings = [r for f in gj.get("features", [])
                 for r in rings_of(f.get("geometry") or {})]
        states[str(fips)] = {"name": info.get("name"), "rings": rings,
                             "bounds": bounds(rings)}
    return states, manifest


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# the selection
# --------------------------------------------------------------------------

def has_box(row: dict[str, Any]) -> bool:
    """A box geometry can be trusted with: present, valid, and not impossible.

    "Impossible" is larger than MAX_BOX_KM in either dimension. Such a feed is
    treated exactly like one with no box -- geometry cannot decide it."""
    b = row.get("bbox")
    if not (isinstance(b, dict) and b.get("valid")):
        return False
    return max(rect_km(box_rect(b))) <= MAX_BOX_KM


def check_hand_table(rows: list[dict[str, Any]]) -> list[str]:
    """Refuse an unnamed no-box feed; report a named one that now has a box.

    Returns warnings. Raises for the case that would otherwise drop a feed
    silently, which is the only case that cannot be noticed afterwards.
    """
    ids = {r["id"] for r in rows}
    no_box = {r["id"] for r in rows if not has_box(r)}
    unnamed = sorted(no_box - set(NAMED_BY_HAND))
    if unnamed:
        raise RegistryRefused(
            f"{len(unnamed)} feed(s) have no box and no decision in "
            f"NAMED_BY_HAND: {unnamed}. Geometry cannot decide them and a "
            f"silent drop would leave no trace. Add a decision with a reason.")
    warn = []
    for fid in sorted(NAMED_BY_HAND):
        if fid not in ids:
            warn.append(f"{fid} is named by hand but is not in the registry "
                        f"any more; the entry is dead")
        elif fid not in no_box:
            warn.append(f"{fid} is named by hand but now HAS a box; remove "
                        f"the entry so geometry decides")
    return warn


def skipped_containing(rect: Rect,
                       skipped: dict[str, dict[str, Any]] | None) -> str | None:
    """The FIPS of a rule-skipped state whose extent overlaps the box, if any."""
    for fips, info in sorted((skipped or {}).items()):
        ext = info.get("extent")
        if ext and rects_overlap(rect, tuple(ext)):
            return fips
    return None


def select(rows: list[dict[str, Any]], states: dict[str, dict[str, Any]],
           buffer_m: float = BUFFER_M,
           skipped: dict[str, dict[str, Any]] | None = None
           ) -> tuple[list[dict], list[dict]]:
    """Every row lands in exactly one of the two lists. See check_arithmetic."""
    selected: list[dict] = []
    excluded: list[dict] = []
    for r in rows:
        base = {"id": r["id"], "provider": r.get("provider"),
                "subdivision": r.get("subdivision"), "url": r.get("url")}
        if "env_var" in r:
            base["env_var"] = r["env_var"]          # a NAME, never a value
        if not has_box(r):
            decision, why = NAMED_BY_HAND[r["id"]]
            raw = r.get("bbox")
            seen = ({"box_km": [round(v, 1) for v in rect_km(box_rect(raw))]}
                    if isinstance(raw, dict) and raw.get("valid") else {})
            if decision == "include":
                selected.append({**base, "how": "named by hand", "states": None,
                                 "reason": why, **seen})
            else:
                excluded.append({**base, "reason": R_BY_HAND, "detail": why,
                                 **seen})
            continue
        rect = buffered(box_rect(r["bbox"]), buffer_m)
        touched = states_touched(rect, states)
        w, h = rect_km(box_rect(r["bbox"]))
        size = {"box_km": [round(w, 1), round(h, 1)]}
        if len(touched) >= 2:
            selected.append({**base, "how": "geometry", "states": touched,
                             "state_names": [states[f]["name"] for f in touched],
                             **size})
        elif len(touched) == 1:
            excluded.append({**base, "reason": R_ONE_STATE,
                             "states": touched, **size})
        else:
            where = skipped_containing(rect, skipped)
            if where is not None:
                excluded.append({**base, "reason": R_SKIPPED_STATE, "states": [],
                                 "skipped_state": where,
                                 "detail": skipped[where].get("reason"), **size})
            else:
                excluded.append({**base, "reason": R_NO_STATE, "states": [], **size})
    return selected, excluded


def resolve_same_agency(selected: list[dict], excluded: list[dict]
                        ) -> tuple[list[dict], list[dict]]:
    """Apply SAME_AGENCY to any agency with two or more SELECTED feeds.

    Grouped by provider name, which is a weak key -- a duplicate filed under a
    differently spelled name would slip past. The vehicle-id comparison is the
    real guard; this is the one the selection can run on its own.
    """
    groups: dict[str, list[dict]] = {}
    for s in selected:
        groups.setdefault(str(s.get("provider") or "").strip().lower(), []).append(s)
    drop: set[str] = set()
    undecided = []
    for name, members in groups.items():
        if len(members) < 2:
            continue
        key = tuple(sorted(m["id"] for m in members))
        if key not in SAME_AGENCY:
            undecided.append((members[0].get("provider"), list(key)))
            continue
        decision, why = SAME_AGENCY[key]
        if decision.startswith("keep:"):
            keep = decision.split(":", 1)[1]
            drop |= {m["id"] for m in members if m["id"] != keep}
    if undecided:
        raise RegistryRefused(
            f"agencies with more than one selected feed and no decision in "
            f"SAME_AGENCY: {undecided}. They are either different vehicles or "
            f"the same vehicles twice, and polling both of a duplicate pair "
            f"double-counts every vehicle without any sign. Decide each one.")
    kept, moved = [], []
    for s in selected:
        if s["id"] in drop:
            key = next(k for k in SAME_AGENCY if s["id"] in k)
            moved.append({k: v for k, v in s.items() if k not in ("how",)} |
                         {"reason": R_DUPLICATE, "detail": SAME_AGENCY[key][1]})
        else:
            kept.append(s)
    return kept, excluded + moved


def check_arithmetic(art: dict[str, Any]) -> None:
    """Selected plus excluded equals rows read, and the reasons sum."""
    if art["n_selected"] + art["n_excluded"] != art["rows_read"]:
        raise RegistryRefused(
            f"{art['n_selected']} selected plus {art['n_excluded']} excluded "
            f"is not {art['rows_read']} rows read; a feed is lost between the "
            f"lists")
    if sum(art["excluded_by_reason"].values()) != art["n_excluded"]:
        raise RegistryRefused("the per-reason counts do not sum to the "
                              "number excluded")


def summary_sentence(art: dict[str, Any]) -> str:
    """One line, four cases, four sentences (rule 13)."""
    n, sel = art["rows_read"], art["n_selected"]
    stray = art["excluded_by_reason"].get(R_NO_STATE, 0)
    if n == 0:
        return "the registry held no feeds, so nothing was selected"
    if sel == 0:
        return (f"none of {n} feeds overlaps two states; either there are no "
                f"border agencies or the boundary set is wrong, and the second "
                f"is likelier")
    head = (f"{sel} of {n} feeds overlap two or more states within "
            f"{art['_rules']['buffer_m']:.0f} m and will be polled")
    if stray:
        return head + (f"; {stray} overlap NO state, which for a US feed means a "
                       f"bad box or a gap in the boundaries -- look at them")
    return head


def build(registry: Path, boundaries: Path, buffer_m: float) -> dict[str, Any]:
    reg = json.loads(registry.read_text(encoding="utf-8"))
    rows = list(reg["selected"]) + list(reg.get("credentialed", []))
    warnings = check_hand_table(rows)
    states, manifest = load_states(boundaries)
    # THE GENERALISATION IS ADDED TO THE BUFFER, NOT MERELY RECORDED.
    #
    # The polygons are simplified by up to tol_m, so a box 2,000 m from the
    # TRUE line can sit up to 2,000 + tol_m m from the simplified one. Testing
    # at buffer_m alone would let exactly the near-miss feeds the buffer exists
    # for slip through, and the artefact would still say "2,000 m" -- a correct
    # number attached to a guarantee it did not deliver. Mechanism U.
    tol_m = float(manifest.get("generalise_m_upper_bound", 0.0))
    reasons = manifest.get("skipped", {})
    skipped = {f: {"extent": e, "reason": reasons.get(f)}
               for f, e in (manifest.get("skipped_extents") or {}).items()}
    selected, excluded = select(rows, states, buffer_m + tol_m, skipped)
    selected, excluded = resolve_same_agency(selected, excluded)
    selected = apply_url_corrections(selected)

    by_reason: dict[str, int] = {}
    for e in excluded:
        by_reason[e["reason"]] = by_reason.get(e["reason"], 0) + 1
    art = {
        "_what": "G2b: transit feeds whose service area straddles or abuts a "
                 "state line, and every feed that does not, with the reason",
        "_built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "_inputs": {
            "transit_registry": registry.name,
            "transit_registry_sha256": file_sha(registry),
            "boundary_manifest_sha256": file_sha(boundaries / "manifest.json"),
            "boundary_source": manifest.get("_service"),
            "boundary_generalised": manifest.get("generalised"),
        },
        "_rules": {
            "test": "the feed's bounding box, grown by buffer_m on every side, "
                    "overlaps two or more state polygons",
            "buffer_m": buffer_m,
            "generalisation_m_upper_bound": tol_m,
            "buffer_applied_m": buffer_m + tol_m,
            "note": "buffer_m is an UPPER bound on any border threshold G5 may "
                    "choose. A G5 threshold above it requires re-running this "
                    "selection, or feeds whose vehicles reach the border zone "
                    "were never polled.",
            "named_by_hand": {k: {"decision": v[0], "reason": v[1]}
                              for k, v in sorted(NAMED_BY_HAND.items())},
            "max_box_km": MAX_BOX_KM,
            "same_agency": {" + ".join(k): {"decision": v[0], "reason": v[1]}
                            for k, v in sorted(SAME_AGENCY.items())},
            "url_corrections": {k: {"catalogue": v[0], "polled": v[1],
                                    "reason": v[2]}
                                for k, v in sorted(URL_CORRECTIONS.items())},
            "states_in_set": len(states),
        },
        "rows_read": len(rows),
        "n_selected": len(selected),
        "n_excluded": len(excluded),
        "excluded_by_reason": by_reason,
        "warnings": warnings,
        "selected": sorted(selected, key=lambda s: s["id"]),
        "excluded": sorted(excluded, key=lambda s: s["id"]),
    }
    check_arithmetic(art)
    check_no_credentials(art)
    return art


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--registry", type=Path, default=REGISTRY)
    ap.add_argument("--boundaries", type=Path, default=BOUNDARIES)
    ap.add_argument("--buffer-m", type=float, default=BUFFER_M)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        art = build(args.registry, args.boundaries, args.buffer_m)
    except RegistryRefused as exc:
        print(f"\n  REFUSED: {exc}\n  Nothing written.\n")
        return 1

    print(f"\n  {summary_sentence(art)}\n")
    for reason, n in sorted(art["excluded_by_reason"].items(), key=lambda kv: -kv[1]):
        print(f"    {n:>4} excluded  {reason}")
    for w in art["warnings"]:
        print(f"    WARNING  {w}")
    print(f"\n  selected ({art['n_selected']}):")
    for s in art["selected"]:
        where = ", ".join(s.get("state_names") or []) or "by hand"
        size = s.get("box_km")
        dims = f"{size[0]:>6.0f} x {size[1]:<5.0f} km" if size else " " * 18
        print(f"    {s['id']:<12} {str(s['provider'])[:40]:<40} {dims}  {where}")
    big = [s for s in art["selected"] if s.get("states") and len(s["states"]) >= 4]
    if big:
        print(f"\n  {len(big)} selected feed(s) touch four or more states. Those "
              f"are regional aggregators rather than border agencies; a border "
              f"effect inside one is diluted by everything else it covers.")
    if args.dry_run:
        print("\n  --dry-run: nothing written\n")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(art, indent=1) + "\n", encoding="utf-8")
    print(f"\n  wrote {args.out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
