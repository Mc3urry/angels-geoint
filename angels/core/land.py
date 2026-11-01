"""The land domain, read for the viewer: no web framework in this file.

Everything the Land tab shows comes from what the two road collectors have
already written, never from the agencies directly. The viewer must not become
a third poller with its own quota, cadence and failure modes. (On
2026-09-19 a browser tab polling OpenSky cut the air collectors off for nine
hours; the air route has read the collector ever since.)

    vehicles     roads/vehicles/hour=*/positions_*.parquet   (G2c)
    feed status  roads/vehicles/attempts-*.jsonl              (G2c ledger)
    work zones   roads/wzdx/_last/<feed>.json -> the body it names (G1)
    WZDx status  roads/wzdx/attempts-*.jsonl                  (G1 ledger)
    states       reference/roads/states-generalised/           (G2b's lines)

WHAT A VEHICLE ON THIS MAP MEANS

The archive keeps a fix only when a vehicle's (timestamp, position) changed
since the poll before (G2c stores new fixes, not polls). So "vehicles in the
last 15 minutes" means **vehicles whose reported position changed in the
last 15 minutes**. A bus standing at a terminus for twenty minutes drops off
the map while its agency still reports it. The payload says so in words. It
is the archive's definition and not the agency's, and the two are different.

The parquet is written every ten polls, about five minutes, so the newest
fix on the map can be that much older than the collector's last poll. The
payload carries `archive_lag_s`, and the panel prints it.

WHAT AN ABSENCE ON THIS MAP MEANS

Usually nothing about the road. A state with no work zones on the map is
most often a state whose agency publishes none, or publishes behind a key.
That is the publication regime this project studies, and it is not an empty
road. The status payload names every feed that did not answer, and why, in
the collector's own outcome words. No cause is added (rule 9).
"""

from __future__ import annotations

import gzip
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from angels.config import LIVE, RAW, REFERENCE

VEHICLES = RAW / "roads" / "vehicles"
WZDX = RAW / "roads" / "wzdx"
SELECTION = REFERENCE / "roads" / "border-feeds.json"
STATES = REFERENCE / "roads" / "states-generalised"
CAMERAS = REFERENCE / "roads" / "g3" / "cameras.geojson"
STREAMS = REFERENCE / "roads" / "g3" / "camera-streams.json"   # local, untracked
SNAPSHOT = LIVE / "roads-vehicles.json"
SNAPSHOT_MAX_AGE_S = 120        # four missed polls; older means the collector stopped
POINTS_OVER_DEG2 = 16.0         # a view wider than ~4 x 4 degrees gets points

DEFAULT_WINDOW_S = 900
MAX_WORKZONES = 4000
TAIL_BYTES = 600_000          # ~1,500 ledger rows: several cycles of 38 feeds

Box = tuple[float, float, float, float]

# Every function takes its root as an argument and falls back to the module
# path AT CALL TIME, so a test can point the routes at a temporary archive.


# --------------------------------------------------------------------------
# small pure helpers
# --------------------------------------------------------------------------

def parse_bbox(text: str | None) -> Box | None:
    if not text:
        return None
    try:
        w, s, e, n = (float(v) for v in text.split(","))
    except ValueError:
        raise ValueError("bbox is west,south,east,north in degrees") from None
    if not (all(math.isfinite(v) for v in (w, s, e, n)) and w < e and s < n
            and -180 <= w and e <= 180 and -90 <= s and n <= 90):
        raise ValueError("bbox is west,south,east,north with west < east "
                         "and south < north")
    return (w, s, e, n)


def coords_bounds(coords: Any) -> Box | None:
    """Bounds of any nested GeoJSON coordinate array."""
    xs: list[float] = []
    ys: list[float] = []

    def walk(c: Any) -> None:
        if (isinstance(c, (list, tuple)) and len(c) >= 2
                and all(isinstance(v, (int, float)) for v in c[:2])):
            xs.append(float(c[0]))
            ys.append(float(c[1]))
        elif isinstance(c, (list, tuple)):
            for x in c:
                walk(x)
    walk(coords)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def overlaps(a: Box, b: Box) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def selection_index(path: Path | None = None) -> dict[str, dict[str, Any]]:
    try:
        doc = json.loads(Path(path or SELECTION).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {r["id"]: r for r in doc.get("selected", [])}


# --------------------------------------------------------------------------
# vehicles
# --------------------------------------------------------------------------

def hour_dirs(root: Path, now_s: float, window_s: float) -> list[Path]:
    """Partitions that can hold a fix fetched inside the window. Partitions
    are named by FLUSH time, so the hour after the window's end is read too."""
    start = datetime.fromtimestamp(now_s - window_s, timezone.utc).replace(
        minute=0, second=0, microsecond=0)
    end = datetime.fromtimestamp(now_s, timezone.utc) + timedelta(hours=1)
    out = []
    t = start
    while t <= end:
        p = Path(root) / f"hour={t:%Y%m%d%H}"
        if p.is_dir():
            out.append(p)
        t += timedelta(hours=1)
    return out


def recent_vehicles(root: Path | None = None, *, now_s: float,
                    window_s: float = DEFAULT_WINDOW_S,
                    index: dict[str, dict[str, Any]] | None = None
                    ) -> dict[str, Any]:
    """The newest stored fix per vehicle inside the window, as GeoJSON."""
    import pyarrow.parquet as pq           # noqa: PLC0415 - core dependency

    root = Path(root) if root is not None else VEHICLES
    index = index if index is not None else selection_index()
    cols = ["feed_id", "vehicle_key", "label", "route_id", "lat", "lon",
            "bearing", "speed", "vehicle_ts", "fetched_at"]
    files = sorted(f for d in hour_dirs(root, now_s, window_s)
                   for f in d.glob("positions_*.parquet"))
    newest: dict[tuple[str, str], dict[str, Any]] = {}
    newest_fetch = None
    for f in files:
        for r in pq.read_table(f, columns=cols).to_pylist():
            if r["fetched_at"] is None or r["fetched_at"] < now_s - window_s:
                continue
            newest_fetch = max(newest_fetch or 0, r["fetched_at"])
            key = (r["feed_id"], r["vehicle_key"])
            when = (r["vehicle_ts"] or r["fetched_at"], r["fetched_at"])
            old = newest.get(key)
            if old is None or when > (old["vehicle_ts"] or old["fetched_at"],
                                      old["fetched_at"]):
                newest[key] = r
    feats = []
    for (feed, _), r in sorted(newest.items()):
        fix_t = r["vehicle_ts"] or r["fetched_at"]
        meta = index.get(feed, {})
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
            "properties": {
                "feed_id": feed,
                "provider": meta.get("provider", ""),
                "label": r["label"] or "",
                "route_id": r["route_id"] or "",
                "bearing": r["bearing"],
                "speed_mps": r["speed"],
                "age_s": max(0, int(now_s - fix_t)),
                # Which clock the age is from. A vehicle with no timestamp of
                # its own is aged from our poll, which flatters it.
                "age_from": "vehicle" if r["vehicle_ts"] else "poll",
            }})
    return {
        "type": "FeatureCollection", "features": feats,
        "properties": {
            "evidence": "cooperative",
            "n": len(feats),
            "n_feeds": len({f["properties"]["feed_id"] for f in feats}),
            "window_s": window_s,
            "files_read": len(files),
            "archive_lag_s": (int(now_s - newest_fetch)
                              if newest_fetch is not None else None),
            "means": ("vehicles whose reported position CHANGED inside the "
                      "window; a vehicle standing still longer drops off "
                      "while its agency still reports it"),
        }}


def live_vehicles(path: Path | None = None, *, now_s: float,
                  max_age_s: float = SNAPSHOT_MAX_AGE_S,
                  index: dict[str, dict[str, Any]] | None = None
                  ) -> dict[str, Any] | None:
    """The collector's latest poll, every vehicle it reported, or None.

    Added 2026-10-27 because the archive cannot be watched: it keeps only
    changed fixes and is written every five minutes, so vehicles appeared in
    jumps and a bus at a stop vanished. The air view has read the collector's
    snapshot since the 2026-09-19 credit loss, and so does this one now.
    None when the snapshot is missing, unreadable or older than `max_age_s`:
    a fleet frozen at the moment the collector stopped is not a live map."""
    path = Path(path) if path is not None else SNAPSHOT
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    t = doc.get("time")
    if not isinstance(t, (int, float)) or now_s - t > max_age_s:
        return None
    index = index if index is not None else selection_index()
    cols = doc.get("cols") or []
    feats = []
    for v in doc.get("vehicles") or []:
        r = dict(zip(cols, v))
        fix_t = r.get("vehicle_ts") or t
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
            "properties": {
                "feed_id": r["feed_id"],
                # The agency's own vehicle key, so a view can follow one bus
                # from poll to poll. A fleet identifier, never a person.
                "vehicle_key": str(r.get("vehicle_key") or ""),
                "provider": index.get(r["feed_id"], {}).get("provider", ""),
                "label": r.get("label") or "", "route_id": r.get("route_id") or "",
                "bearing": r.get("bearing"), "speed_mps": r.get("speed"),
                "fix_t": fix_t,
                "age_s": max(0, int(now_s - fix_t)),
                "age_from": "vehicle" if r.get("vehicle_ts") else "poll",
                "in_service": bool(r.get("in_service")),
                # Where the speed and heading came from: the vehicle's own
                # report, or the collector's previous poll (`derived`).
                "motion": r.get("motion") or "feed",
            }})
    return {"type": "FeatureCollection", "features": feats,
            "properties": {
                "evidence": "cooperative", "source": "collector-live",
                "n": len(feats),
                "n_in_service": sum(1 for f in feats if f["properties"]["in_service"]),
                "n_feeds": len({f["properties"]["feed_id"] for f in feats}),
                "poll_age_s": int(now_s - t),
                "feeds": doc.get("feeds") or {},
                "means": ("every vehicle the agencies reported at the "
                          "collector's last poll, moving or not; a vehicle "
                          "with no trip is drawn faint, usually parked")}}


# --------------------------------------------------------------------------
# ledgers
# --------------------------------------------------------------------------

def ledger_tail(root: Path, *, tail_bytes: int = TAIL_BYTES) -> list[dict[str, Any]]:
    """The last rows of the newest ledger file, without reading all of it."""
    files = sorted(Path(root).glob("attempts-*.jsonl"))
    if not files:
        return []
    path = files[-1]
    size = path.stat().st_size
    with path.open("rb") as fh:
        fh.seek(max(0, size - tail_bytes))
        chunk = fh.read()
    lines = chunk.split(b"\n")
    if size > tail_bytes:
        lines = lines[1:]                     # the first is probably partial
    rows = []
    for ln in lines:
        if ln.strip():
            try:
                rows.append(json.loads(ln))
            except ValueError:
                continue                       # a line still being written
    return rows


def last_by_feed(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if "feed" in r:
            out[r["feed"]] = r
    return out


def vehicle_status(root: Path | None = None, *, now_s: float,
                   index: dict[str, dict[str, Any]] | None = None
                   ) -> list[dict[str, Any]]:
    """Every selected feed, its last outcome in the collector's own words,
    and a row for a feed with NO ledger row at all: the third footprint."""
    root = Path(root) if root is not None else VEHICLES
    index = index if index is not None else selection_index()
    last = last_by_feed(ledger_tail(root))
    out = []
    for fid, meta in sorted(index.items()):
        r = last.get(fid)
        out.append({
            "feed": fid, "provider": meta.get("provider", ""),
            "states": meta.get("state_names") or [],
            "outcome": r.get("outcome") if r else "no ledger row",
            "status": r.get("status") if r else None,
            "error": (r.get("error") or "")[:120] if r else "",
            "n_vehicles": r.get("n_vehicles") if r else None,
            "age_s": int(now_s - r["t"]) if r and "t" in r else None,
        })
    return out


def wzdx_status(root: Path | None = None, *, now_s: float) -> list[dict[str, Any]]:
    root = Path(root) if root is not None else WZDX
    last = last_by_feed(ledger_tail(root))
    return [{"feed": f, "outcome": r.get("outcome"),
             "returned": r.get("returned"), "n_events": r.get("n_events"),
             "state": r.get("state"), "status": r.get("status"),
             "age_s": int(now_s - r["t"]) if "t" in r else None}
            for f, r in sorted(last.items())]


# --------------------------------------------------------------------------
# work zones
# --------------------------------------------------------------------------

def _first(*vals: Any) -> Any:
    for v in vals:
        if v not in (None, "", []):
            return v
    return None


def zone_props(feed: str, f: dict[str, Any]) -> dict[str, Any]:
    """The few fields the map needs, from WZDx 4.x or 3.x."""
    p = f.get("properties") or {}
    core = p.get("core_details") or {}
    roads = _first(core.get("road_names"), p.get("road_names"),
                   p.get("road_name"))
    if isinstance(roads, list):
        roads = ", ".join(str(r) for r in roads[:3])
    return {
        "feed": feed,
        "road": str(roads or "")[:120],
        "direction": str(_first(core.get("direction"), p.get("direction")) or ""),
        "impact": str(p.get("vehicle_impact") or ""),
        "start": str(p.get("start_date") or ""),
        "end": str(p.get("end_date") or ""),
    }


_ZONE_CACHE: dict[str, tuple[float, list[tuple[Box, dict[str, Any]]]]] = {}


def _zones_of(root: Path, feed: str, side: Path) -> list[tuple[Box, dict[str, Any]]]:
    """A feed's current events, re-read only when its sidecar changes."""
    mtime = side.stat().st_mtime
    hit = _ZONE_CACHE.get(feed)
    if hit and hit[0] == mtime:
        return hit[1]
    zones: list[tuple[Box, dict[str, Any]]] = []
    try:
        name = json.loads(side.read_text(encoding="utf-8")).get("path", "")
        body = next(iter(Path(root).glob(f"hour=*/{name}")), None) if name else None
        doc = json.loads(gzip.decompress(body.read_bytes())) if body else {}
    except (OSError, ValueError):
        doc = {}
    for f in doc.get("features") or []:
        g = f.get("geometry") if isinstance(f, dict) else None
        b = coords_bounds((g or {}).get("coordinates"))
        if b is not None:
            zones.append((b, {"type": "Feature", "geometry": g,
                              "properties": zone_props(feed, f)}))
    _ZONE_CACHE[feed] = (mtime, zones)
    return zones


def first_point(geom: dict[str, Any] | None) -> list[float] | None:
    c = (geom or {}).get("coordinates")
    while isinstance(c, list) and c and isinstance(c[0], list):
        c = c[0]
    return c[:2] if isinstance(c, list) and len(c) >= 2 else None


def workzones(root: Path | None = None, *, bbox: Box,
              limit: int = MAX_WORKZONES) -> dict[str, Any]:
    """Declared work zones in a box.

    A WIDE VIEW GETS EVERY ZONE AS A POINT, NOT SOME ZONES AS LINES. The first
    version cut the country at 4,000 lines in feed-name order, so the first
    few feeds filled the map and later states showed nothing at all. A cut by
    alphabet drawn as a map reads as geography. That is a page taken for the
    layer, the defect G3 refuses, made by this file. Now a view wider than
    POINTS_OVER_DEG2 square degrees draws every zone as one point, with no
    cut. A narrow view keeps lines, and a cut there is stated."""
    root = Path(root) if root is not None else WZDX
    points = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) > POINTS_OVER_DEG2
    feats: list[dict[str, Any]] = []
    n_in_view = 0
    feeds_in_view: set[str] = set()
    for side in sorted((Path(root) / "_last").glob("*.json")):
        feed = side.stem
        for b, f in _zones_of(root, feed, side):
            if overlaps(b, bbox):
                n_in_view += 1
                feeds_in_view.add(feed)
                if points:
                    p = first_point(f["geometry"])
                    if p is not None:
                        feats.append({"type": "Feature",
                                      "geometry": {"type": "Point", "coordinates": p},
                                      "properties": f["properties"]})
                elif len(feats) < limit:
                    feats.append(f)
    return {"type": "FeatureCollection", "features": feats,
            "properties": {"evidence": "cooperative (declared by agencies)",
                           "n_in_view": n_in_view, "n_shown": len(feats),
                           "truncated": n_in_view > len(feats),
                           "as_points": points,
                           "feeds_in_view": sorted(feeds_in_view),
                           "means": ("work zones agencies DECLARE; a place "
                                     "with none is often a place whose "
                                     "agency publishes none")}}


# --------------------------------------------------------------------------
# state lines
# --------------------------------------------------------------------------

def states_in(directory: Path | None = None, *, bbox: Box | None) -> dict[str, Any]:
    directory = Path(directory) if directory is not None else STATES
    try:
        manifest = json.loads((Path(directory) / "manifest.json").read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        return {"type": "FeatureCollection", "features": [],
                "properties": {"error": "no boundary manifest"}}
    feats = []
    for fips, s in sorted((manifest.get("states") or {}).items()):
        path = Path(directory) / s.get("file", f"{fips}.geojson")
        if not path.exists():
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        for f in doc.get("features") or []:
            b = coords_bounds((f.get("geometry") or {}).get("coordinates"))
            if b is None or (bbox is not None and not overlaps(b, bbox)):
                continue
            feats.append({"type": "Feature", "geometry": f["geometry"],
                          "properties": {"state": fips,
                                         "name": str(s.get("name") or "").strip()}})
    return {"type": "FeatureCollection", "features": feats,
            "properties": {"source": manifest.get("_service"),
                           "generalised_m": manifest.get(
                               "generalise_m_upper_bound")}}


def _stream_doc(path: Path | None = None) -> dict[str, Any]:
    try:
        return json.loads(Path(path or STREAMS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _streams(path: Path | None = None) -> dict[str, Any]:
    return _stream_doc(path).get("streams") or {}


def camera_stream(object_id: str, path: Path | None = None) -> dict[str, Any] | None:
    """CHART's own player link for one camera, or None. Read from the local,
    untracked file G3 writes; validated to a Maryland government host there."""
    doc = _stream_doc(path)
    s = (doc.get("streams") or {}).get(str(object_id))
    if not s or not (s.get("embed") or s.get("page")):
        return None
    policy = doc.get("frame_policy") or {}
    return {"id": str(object_id), "embed": s.get("embed"), "page": s.get("page"),
            # Read from CHART's own headers at fetch time, not assumed.
            "embeddable": policy.get("embeddable", True),
            "op_status": s.get("op_status"),
            "source": "Maryland CHART, played from CHART's servers",
            "note": "not recorded, stored or republished by this project"}


def cameras(path: Path | None = None, streams: Path | None = None) -> dict[str, Any]:
    """CHART cameras from G3: where the state itself can see the road.

    Locations in the tracked file; whether CHART offers a stream for each
    comes from the local stream file (see camera_stream)."""
    path = Path(path) if path is not None else CAMERAS
    links = _streams(streams)
    # CHART's own list when G3 has fetched it: 553 cameras with live links,
    # where the GIS layer has 451 with dead ones (FINDINGS 2026-10-28).
    if links and all("lat" in s for s in links.values()):
        feats = [{"type": "Feature",
                  "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
                  # CHART's `name` is often a bare code ("710002"); its
                  # description is the place ("I-70 East of I-270").
                  "properties": {"id": cid,
                                 "location": s.get("description") or s.get("name", ""),
                                 "code": s.get("name", ""),
                                 "county": s.get("category", ""),
                                 "status": s.get("op_status") or "",
                                 "has_stream": bool(s.get("embed") or s.get("page"))}}
                 for cid, s in links.items()]
        return {"type": "FeatureCollection", "features": feats,
                "properties": {"n": len(feats), "source": "CHART's own camera feed",
                               "evidence": "the state's own observation points",
                               "n_with_stream": sum(1 for f in feats
                                                    if f["properties"]["has_stream"]),
                               "streams": "played from CHART's servers; links local only"}}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"type": "FeatureCollection", "features": [],
                "properties": {"error": "no G3 camera layer: "
                               "python scripts/fetch_road_mask.py"}}
    feats = []
    for f in doc.get("features") or []:
        if not f.get("geometry"):
            continue
        p = f.get("properties") or {}
        oid = str(p.get("OBJECTID", ""))
        feats.append({"type": "Feature", "geometry": f["geometry"],
                      "properties": {"id": oid,
                                     "location": p.get("location", ""),
                                     "county": p.get("county", ""),
                                     "has_stream": oid in links}})
    return {"type": "FeatureCollection", "features": feats,
            "properties": {"n": len(feats), "source": "CHART via iMAP (G3)",
                           "evidence": "the state's own observation points",
                           "n_with_stream": sum(1 for f in feats
                                                if f["properties"]["has_stream"]),
                           "streams": ("played from CHART's servers; links "
                                       "local only" if links else
                                       "no local stream file: python "
                                       "scripts/fetch_road_mask.py --layer cameras")}}


def now() -> float:
    return datetime.now(timezone.utc).timestamp()


def file_age_s(path: Path) -> float | None:
    try:
        return now() - os.path.getmtime(path)
    except OSError:
        return None
