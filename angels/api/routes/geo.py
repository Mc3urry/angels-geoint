"""Place search for both views.

    GET /geo/search?q=…[&lon=…&lat=…]   places matching q, nearest the view first

WHERE, NOT WHO. The search bar in each view finds contacts on the map in the
browser (a call sign, an MMSI, a route) and asks this route only for PLACES:
a town, an address, a road, an airport, or a typed "lat, lon". Nothing about
the person searching is sent anywhere but the query text and, to rank
nearby places first, the centre of the map. "Find my location" never comes
here: the browser's own position stays in the browser.

TWO SOURCES. TomTom's search, with the TOMTOM_API_KEY the road-speed layer
already uses (free tier: 2,500 searches a day), added here so the key never
reaches the browser, a response or an error message. Without a key,
OpenStreetMap's Nominatim, under its usage policy: an identifying User-Agent,
at most one request a second, and answers cached. Answers from either are
kept for a day, so the same search does not spend the quota twice.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from angels import config  # noqa: F401  (loads .env)

router = APIRouter(prefix="/geo", tags=["geo"])

TOMTOM_ENV = "TOMTOM_API_KEY"
TOMTOM = "https://api.tomtom.com/search/2/search/{q}.json"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
UA = "angels-geoint/0.1 (senior GIS capstone; github.com/Mc3urry/angels-geoint)"
LIMIT = 6
TTL_S = 24 * 3600
_CACHE_MAX = 1000
NOMINATIM_GAP_S = 1.1

_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
_nominatim_lock = threading.Lock()
_nominatim_last = [0.0]

COORD_RE = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


def _key() -> str:
    return os.getenv(TOMTOM_ENV, "").strip()


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return r.read(2 * 1024 * 1024)


def clean_query(q: str) -> str:
    """Printable text only, single-spaced, at most 120 characters."""
    return " ".join("".join(ch for ch in str(q) if ch.isprintable()).split())[:120]


def as_coordinates(q: str) -> dict | None:
    """'38.9, -77.03' (lat, lon) is a place already; nothing to ask."""
    m = COORD_RE.match(q)
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return {"name": f"{lat:.5f}, {lon:.5f}", "label": "coordinates as typed (lat, lon)",
            "lon": lon, "lat": lat, "bbox": None, "kind": "coordinates"}


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def parse_tomtom(doc: Any) -> list[dict]:
    out = []
    for r in (doc or {}).get("results") or [] if isinstance(doc, dict) else []:
        if not isinstance(r, dict):
            continue
        pos = r.get("position") or {}
        lat, lon = _f(pos.get("lat")), _f(pos.get("lon"))
        if lat is None or lon is None:
            continue
        addr = r.get("address") or {}
        poi = (r.get("poi") or {}).get("name")
        name = poi or addr.get("freeformAddress") or addr.get("municipality") or "place"
        label = addr.get("freeformAddress") if poi else ", ".join(
            x for x in (addr.get("countrySubdivision"), addr.get("country")) if x)
        vp = r.get("viewport") or {}
        tl, br = vp.get("topLeftPoint") or {}, vp.get("btmRightPoint") or {}
        box = [_f(tl.get("lon")), _f(br.get("lat")), _f(br.get("lon")), _f(tl.get("lat"))]
        out.append({"name": str(name)[:120], "label": str(label or "")[:160], "lon": lon, "lat": lat,
                    "bbox": box if all(b is not None for b in box) else None,
                    "kind": str(r.get("entityType") or r.get("type") or "place")[:40]})
    return out[:LIMIT]


def parse_nominatim(doc: Any) -> list[dict]:
    out = []
    for r in doc if isinstance(doc, list) else []:
        if not isinstance(r, dict):
            continue
        lat, lon = _f(r.get("lat")), _f(r.get("lon"))
        if lat is None or lon is None:
            continue
        bb = r.get("boundingbox") or []
        box = None
        if isinstance(bb, list) and len(bb) == 4:
            s, n, w, e = (_f(x) for x in bb)
            box = [w, s, e, n] if None not in (s, n, w, e) else None
        full = str(r.get("display_name") or "")
        name = r.get("name") or full.split(",")[0] or "place"
        out.append({"name": str(name)[:120], "label": full[:160], "lon": lon, "lat": lat, "bbox": box,
                    "kind": str(r.get("type") or r.get("category") or "place")[:40]})
    return out[:LIMIT]


def _tomtom(q: str, lon: float | None, lat: float | None) -> list[dict]:
    params = {"key": _key(), "limit": LIMIT, "typeahead": "true", "language": "en-US"}
    if lon is not None and lat is not None:
        params.update(lon=round(lon, 3), lat=round(lat, 3))
    url = TOMTOM.format(q=urllib.parse.quote(q, safe="")) + "?" + urllib.parse.urlencode(params)
    return parse_tomtom(json.loads(_get(url)))


def _nominatim(q: str, lon: float | None, lat: float | None) -> list[dict]:
    params = {"q": q, "format": "jsonv2", "limit": LIMIT}
    if lon is not None and lat is not None:
        # A soft preference for the area in view: about 4 degrees around it.
        params["viewbox"] = f"{lon - 4:.3f},{lat + 4:.3f},{lon + 4:.3f},{lat - 4:.3f}"
    with _nominatim_lock:
        gap = NOMINATIM_GAP_S - (time.time() - _nominatim_last[0])
        if gap > 0:
            time.sleep(gap)
        try:
            return parse_nominatim(json.loads(_get(NOMINATIM + "?" + urllib.parse.urlencode(params))))
        finally:
            _nominatim_last[0] = time.time()


@router.get("/search")
def search(q: str = Query(..., max_length=200), lon: float | None = Query(None, ge=-180, le=180),
           lat: float | None = Query(None, ge=-90, le=90)) -> dict:
    q = clean_query(q)
    if len(q) < 2:
        raise HTTPException(422, "type at least two characters")
    coord = as_coordinates(q)
    if coord:
        return {"results": [coord], "source": "typed coordinates", "q": q}
    keyed = bool(_key())
    near = f"{round(lon, 1)},{round(lat, 1)}" if lon is not None and lat is not None else ""
    ck = f"{'t' if keyed else 'n'}|{q.lower()}|{near}"
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < TTL_S:
        return hit[1]
    source = "TomTom" if keyed else "OpenStreetMap Nominatim"
    try:
        results = _tomtom(q, lon, lat) if keyed else _nominatim(q, lon, lat)
    except urllib.error.HTTPError as exc:
        raise HTTPException(502, f"{source} answered HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise HTTPException(502, f"{source} did not answer") from None
    doc = {"results": results, "source": source, "q": q}
    _cache[ck] = (time.time(), doc)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)
    return doc
