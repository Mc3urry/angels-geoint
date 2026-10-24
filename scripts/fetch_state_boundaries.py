"""G2b: fetch the state polygons the border selection runs against.

    python scripts/fetch_state_boundaries.py
    python scripts/fetch_state_boundaries.py --only 24 --only 51

Writes one simplified GeoJSON per state to
`data/reference/roads/states-generalised/`, plus `manifest.json` recording what
was fetched, what was skipped and why, and whether the server actually
simplified anything.

WHAT THESE ARE FIT FOR, AND WHAT THEY ARE NOT

Selection, with a 2 km buffer: yes. A box either overlaps two states or it
does not, and simplifying a boundary by ~100 m cannot change that for a box
grown by kilometres -- particularly since `select_border_feeds.py` adds the
simplification tolerance to its buffer rather than merely recording it.

G6's border distances: NO. Those are measured in hundreds of metres against a
boundary shown on 2026-10-23 to be good to about 5 m at full resolution, and
these polygons have been deliberately coarsened by up to ~111 m. The directory
is named `states-generalised` so nobody reaches for it for that. G6 fetches
full resolution, for the states it needs, the way
`measure_boundary_detail.py` does.

WHY SIMPLIFY AT ALL

Speed and size. Maryland alone is 1.06 MB and 26,094 vertices at full
resolution; the coastal states are much larger. Selection would still be
correct at full resolution, only slower -- so this asks the server to
simplify and then CHECKS whether it did, rather than assuming.

THE CHECK: Maryland was measured at full resolution on 2026-10-23 at 26,094
vertices. If the simplified fetch of Maryland comes back near that, the server
ignored `maxAllowableOffset`, and the manifest says `generalised: false`. Then
the selection is still right and nobody is misled about how coarse the
polygons are.

WHICH STATES, AND WHY NOT ALL OF THEM

Every state the service returns is fetched EXCEPT those with no land boundary
with another US state, because they cannot be on either side of an
inter-state discontinuity: Alaska (Canada only), Hawaii, and the territories.
The exclusions are named in code with a reason each. A state the service
returns that is neither fetched nor named is a refusal, not a silent gap --
the selection would otherwise report "one state" for a feed whose neighbour
simply was not downloaded. Mechanism G.

Runs on the user's machine. The Census host refuses the agent's proxy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
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
OUT_DIR = REFERENCE / "roads" / "states-generalised"
TIMEOUT_S = 120.0
CAP_BYTES = 128 * 1024 * 1024
PAUSE_S = 0.5            # a public service; one request at a time, politely

GENERALISE_DEG = 0.001
# One thousandth of a degree is 111.3 m of latitude, and never more than that
# of longitude. The selection adds this to its buffer.
GENERALISE_M_UPPER_BOUND = round(GENERALISE_DEG * 111_320.0, 1)

# Measured 2026-10-23, scripts/measure_boundary_detail.py, full resolution.
MARYLAND_FULL_RES_VERTICES = 26_094
MARYLAND = "24"

NO_LAND_NEIGHBOUR: dict[str, str] = {
    "02": "Alaska: its only land boundary is with Canada",
    "15": "Hawaii: no land boundary",
    "60": "American Samoa: territory, no land boundary with a state",
    "66": "Guam: territory, no land boundary with a state",
    "69": "Northern Mariana Islands: territory, no land boundary with a state",
    "72": "Puerto Rico: territory, no land boundary with a state",
    "78": "U.S. Virgin Islands: territory, no land boundary with a state",
}


class BoundaryRefused(RuntimeError):
    """The boundary set would be incomplete or wrong, so none is written."""


# --------------------------------------------------------------------------
# pure
# --------------------------------------------------------------------------

def extent_url(fips: str) -> str:
    """Only the bounding extent of one state, in WGS84 -- four numbers.

    Asked for every state skipped BY RULE, so the selection can tell a feed in
    Anchorage (inside a state skipped on purpose) from a feed whose box is
    simply wrong. Both overlap no fetched polygon, and without this they would
    share one outcome. See select_border_feeds.R_SKIPPED_STATE.
    """
    q = {"where": f"STATE='{fips}'", "returnExtentOnly": "true",
         "outSR": "4326", "f": "json"}
    return SERVICE + "?" + urllib.parse.urlencode(q)


def parse_extent(doc: dict[str, Any]) -> list[float] | None:
    """[xmin, ymin, xmax, ymax], or None if the service gave nothing usable.

    None is not an error: a feed in a state whose extent is unknown simply
    stays in the flagged outcome, which over-reports rather than hides."""
    e = doc.get("extent") if isinstance(doc, dict) else None
    if not isinstance(e, dict):
        return None
    try:
        box = [float(e["xmin"]), float(e["ymin"]), float(e["xmax"]), float(e["ymax"])]
    except (KeyError, TypeError, ValueError):
        return None
    if any(v != v for v in box) or box[0] > box[2] or box[1] > box[3]:   # NaN, inverted
        return None
    return box


def list_url() -> str:
    q = {"where": "1=1", "outFields": "STATE,NAME", "returnGeometry": "false",
         "f": "json"}
    return SERVICE + "?" + urllib.parse.urlencode(q)


def state_url(fips: str, generalise_deg: float = GENERALISE_DEG) -> str:
    """One state, in WGS84, simplified by `generalise_deg`.

    `outSR` is stated rather than assumed, since `maxAllowableOffset` is in the
    units of the OUTPUT spatial reference -- degrees here. A projected outSR
    would make 0.001 a millimetre.
    """
    q = {"where": f"STATE='{fips}'", "outFields": "STATE,NAME",
         "returnGeometry": "true", "outSR": "4326", "f": "geojson",
         "maxAllowableOffset": repr(generalise_deg)}
    return SERVICE + "?" + urllib.parse.urlencode(q)


def plan(listed: list[dict[str, str]],
         only: tuple[str, ...] = ()) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Which listed states to fetch, and which to skip with the reason.

    Every listed state lands in exactly one of the two. `--only` narrows the
    fetch for testing; anything it leaves out is recorded as skipped BY THE
    COMMAND LINE, so a partial set can never be mistaken for a complete one.
    """
    fetch, skip = [], {}
    for s in listed:
        fips = str(s["STATE"])
        if fips in NO_LAND_NEIGHBOUR:
            skip[fips] = NO_LAND_NEIGHBOUR[fips]
        elif only and fips not in only:
            skip[fips] = "left out by --only on the command line: THIS SET IS PARTIAL"
        else:
            fetch.append({"STATE": fips, "NAME": s.get("NAME")})
    return fetch, skip


def check_plan(listed, fetch, skip) -> None:
    if len(fetch) + len(skip) != len(listed):
        raise BoundaryRefused(
            f"{len(fetch)} to fetch plus {len(skip)} skipped is not the "
            f"{len(listed)} the service listed; a state is lost between them")
    bad = [s["STATE"] for s in listed if not (isinstance(s["STATE"], str)
                                               and len(s["STATE"]) == 2)]
    if bad:
        raise BoundaryRefused(
            f"STATE values that are not two-character strings: {bad}. FIPS "
            f"must stay zero-padded strings or a join matches nothing.")


def vertex_count(gj: dict[str, Any]) -> int:
    n = 0
    for f in gj.get("features", []):
        g = f.get("geometry") or {}
        c = g.get("coordinates") or []
        if g.get("type") == "Polygon":
            n += sum(len(r) for r in c)
        elif g.get("type") == "MultiPolygon":
            n += sum(len(r) for p in c for r in p)
    return n


def validate_state(fips: str, gj: dict[str, Any]) -> None:
    """One feature, with geometry, for the state asked for, and complete."""
    feats = gj.get("features")
    if gj.get("exceededTransferLimit"):
        raise BoundaryRefused(f"{fips}: the service returned a PAGE, not the "
                              f"whole polygon")
    if not isinstance(feats, list) or len(feats) != 1:
        raise BoundaryRefused(f"{fips}: expected 1 feature, got "
                              f"{len(feats) if isinstance(feats, list) else feats!r}")
    props = feats[0].get("properties") or {}
    if str(props.get("STATE")) != fips:
        raise BoundaryRefused(f"{fips}: asked for STATE {fips!r}, got "
                              f"{props.get('STATE')!r}")
    if vertex_count(gj) == 0:
        raise BoundaryRefused(f"{fips}: the feature has no geometry")


def generalisation_verdict(vertices: dict[str, int]) -> tuple[bool | None, str]:
    """Did the server simplify? Answered from Maryland, whose full-resolution
    count is known. None when Maryland was not fetched -- unknown is not yes."""
    md = vertices.get(MARYLAND)
    if md is None:
        return None, ("Maryland was not in this fetch, so whether the server "
                      "simplified is unknown")
    ratio = md / MARYLAND_FULL_RES_VERTICES
    if ratio > 0.75:
        return False, (f"Maryland came back with {md:,} vertices against "
                       f"{MARYLAND_FULL_RES_VERTICES:,} at full resolution: the "
                       f"server did NOT simplify. Selection is still correct, "
                       f"only slower, and these polygons are full resolution")
    return True, (f"Maryland came back with {md:,} vertices against "
                  f"{MARYLAND_FULL_RES_VERTICES:,} at full resolution "
                  f"({ratio:.0%}): simplified as asked")


# --------------------------------------------------------------------------
# the network
# --------------------------------------------------------------------------

def get_json(url: str) -> tuple[dict[str, Any], int, float]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        body = r.read(CAP_BYTES + 1)
    if len(body) > CAP_BYTES:
        raise BoundaryRefused(f"response over {CAP_BYTES // 2**20} MiB; "
                              f"refusing to keep a truncated polygon")
    return json.loads(body.decode("utf-8")), len(body), time.monotonic() - t0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", action="append", default=[],
                    help="two-digit FIPS string; repeatable. Marks the set PARTIAL.")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    for o in args.only:
        if not (len(o) == 2 and o.isdigit()):
            raise SystemExit(f"\n  --only {o!r}: FIPS codes are two-digit "
                             f"zero-padded strings, e.g. 01 not 1\n")

    print("\n  listing states from TIGERweb State_County")
    try:
        listing, _, _ = get_json(list_url())
    except (urllib.error.URLError, BoundaryRefused) as exc:
        print(f"  could not list states: {exc}\n")
        return 1
    listed = [{"STATE": str(f["attributes"]["STATE"]),
               "NAME": f["attributes"].get("NAME")}
              for f in listing.get("features", [])]
    if listing.get("exceededTransferLimit"):
        print("  the state LIST came back as a page; refusing a partial set\n")
        return 1

    fetch, skip = plan(listed, tuple(args.only))
    try:
        check_plan(listed, fetch, skip)
    except BoundaryRefused as exc:
        print(f"  REFUSED: {exc}\n")
        return 1
    print(f"  {len(listed)} listed: {len(fetch)} to fetch, {len(skip)} skipped")
    for fips, why in sorted(skip.items()):
        print(f"    skip {fips}  {why}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    states: dict[str, dict[str, Any]] = {}
    vertices: dict[str, int] = {}
    for i, s in enumerate(fetch, 1):
        fips = s["STATE"]
        try:
            gj, nbytes, secs = get_json(state_url(fips))
            validate_state(fips, gj)
        except (urllib.error.URLError, BoundaryRefused, json.JSONDecodeError) as exc:
            # One state missing makes every feed near it read as "one state".
            # That is a wrong answer with a right shape, so the whole set is
            # refused rather than written with a hole in it.
            print(f"  [{i:>2}/{len(fetch)}] {fips} FAILED: {exc}")
            print("  No manifest written: a boundary set with a missing state "
                  "would make its neighbours' feeds look single-state.\n")
            return 1
        name = f"{fips}.geojson"
        raw = json.dumps(gj, separators=(",", ":")).encode("utf-8")
        (args.out_dir / name).write_bytes(raw)
        v = vertex_count(gj)
        vertices[fips] = v
        states[fips] = {"name": s["NAME"], "file": name, "vertices": v,
                        "bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest()}
        print(f"  [{i:>2}/{len(fetch)}] {fips} {str(s['NAME'])[:22]:<22} "
              f"{v:>8,} vertices  {nbytes / 1024:>8,.0f} KiB  {secs:5.1f} s")
        time.sleep(PAUSE_S)

    # Extents of the states skipped BY RULE -- not those --only left out, which
    # are a partial set and refused downstream anyway.
    skipped_extents: dict[str, list[float] | None] = {}
    for fips in sorted(f for f in skip if f in NO_LAND_NEIGHBOUR):
        try:
            doc, _, _ = get_json(extent_url(fips))
            skipped_extents[fips] = parse_extent(doc)
        except (urllib.error.URLError, BoundaryRefused, json.JSONDecodeError):
            skipped_extents[fips] = None
        shown = skipped_extents[fips]
        print(f"  extent {fips}: "
              f"{'unknown -- feeds there will stay flagged' if shown is None else [round(v, 2) for v in shown]}")
        time.sleep(PAUSE_S)

    generalised, why = generalisation_verdict(vertices)
    manifest = {
        "_what": "state polygons for G2b's border selection, SIMPLIFIED. Fit "
                 "for selection with a buffer; NOT fit for G6 border distances.",
        "_service": SERVICE,
        "_fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generalise_deg": GENERALISE_DEG,
        "generalise_m_upper_bound": GENERALISE_M_UPPER_BOUND,
        "generalised": generalised,
        "generalised_note": why,
        "partial": bool(args.only),
        "states": states,
        "skipped": skip,
        "skipped_extents": skipped_extents,
    }
    (args.out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    total = sum(vertices.values())
    print(f"\n  {len(states)} states, {total:,} vertices")
    print(f"  {why}")
    if args.only:
        print("  PARTIAL SET: --only was used. The selection will treat every "
              "state not fetched as absent.")
    print(f"  wrote {len(states)} files and manifest.json to {args.out_dir}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
