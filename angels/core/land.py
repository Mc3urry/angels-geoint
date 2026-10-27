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

from angels.config import RAW, REFERENCE

VEHICLES = RAW / "roads" / "vehicles"
WZDX = RAW / "roads" / "wzdx"
SELECTION = REFERENCE / "roads" / "border-feeds.json"
STATES = REFERENCE / "roads" / "states-generalised"

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


def workzones(root: Path | None = None, *, bbox: Box,
              limit: int = MAX_WORKZONES) -> dict[str, Any]:
    root = Path(root) if root is not None else WZDX
    feats: list[dict[str, Any]] = []
    n_in_view = 0
    feeds_in_view: set[str] = set()
    for side in sorted((Path(root) / "_last").glob("*.json")):
        feed = side.stem
        for b, f in _zones_of(root, feed, side):
            if overlaps(b, bbox):
                n_in_view += 1
                feeds_in_view.add(feed)
                if len(feats) < limit:
                    feats.append(f)
    return {"type": "FeatureCollection", "features": feats,
            "properties": {"evidence": "cooperative (declared by agencies)",
                           "n_in_view": n_in_view, "n_shown": len(feats),
                           "truncated": n_in_view > len(feats),
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


def now() -> float:
    return datetime.now(timezone.utc).timestamp()


def file_age_s(path: Path) -> float | None:
    try:
        return now() - os.path.getmtime(path)
    except OSError:
        return None
