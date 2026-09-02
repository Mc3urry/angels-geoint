"""Live aircraft positions.

    GET /live?domain=air

Goes straight to OpenSky rather than reading the archive, so this is current
to within a few seconds. The archive lags by however long the poller's flush
interval is, which is right for analysis and wrong for a map you are watching.

QUOTA. An authenticated account gets roughly 4,000 credits a day. A browser
polling every 10 s is 8,640 requests -- over budget on its own, and the ingest
poller is spending from the same pot. So responses are cached server-side:
every browser tab, and every refresh inside the cache window, shares one
upstream call. Tune CACHE_S up if you leave a tab open all day.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException

from angels.adapters.aviation import opensky
from angels.adapters.aviation.opensky import TokenManager
from angels.config import AOI_AIR

router = APIRouter(tags=["live"])

CACHE_S = 6.0

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_tokens: TokenManager | None = None


def _token_manager() -> TokenManager:
    global _tokens
    if _tokens is None:
        _tokens = TokenManager()      # raises if credentials are missing
    return _tokens


def _feature(row: list[Any]) -> dict[str, Any] | None:
    lat, lon = row[opensky.LATITUDE], row[opensky.LONGITUDE]
    if lat is None or lon is None:
        return None

    alt = row[opensky.GEO_ALTITUDE]
    if alt is None:
        alt = row[opensky.BARO_ALTITUDE]

    callsign = (row[opensky.CALLSIGN] or "").strip()

    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
        "properties": {
            "icao24": row[opensky.ICAO24],
            "callsign": callsign or row[opensky.ICAO24].upper(),
            "country": row[opensky.ORIGIN_COUNTRY],
            # heading and speed are what let the browser dead-reckon between
            # polls, which is the difference between icons that jump and
            # icons that glide
            "heading": row[opensky.TRUE_TRACK],
            "speed_mps": row[opensky.VELOCITY],
            "alt_m": alt,
            "vertical_rate": row[opensky.VERTICAL_RATE],
            "on_ground": bool(row[opensky.ON_GROUND]),
            "squawk": row[opensky.SQUAWK],
            "t": row[opensky.LAST_CONTACT],
        },
    }


@router.get("/live")
def get_live(domain: Literal["air"] = "air",
             include_on_ground: bool = False) -> dict[str, Any]:
    if domain != "air":
        raise HTTPException(501, f"no live feed for domain '{domain}' yet")

    key = f"{domain}:{include_on_ground}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_S:
        cached = dict(hit[1])
        cached["properties"] = {**cached["properties"], "cached": True}
        return cached

    try:
        t, rows = opensky.fetch_states(AOI_AIR, _token_manager())
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"opensky unreachable: {exc}") from exc

    feats = []
    for row in rows:
        if not include_on_ground and row[opensky.ON_GROUND]:
            continue
        f = _feature(row)
        if f is not None:
            feats.append(f)

    payload = {
        "type": "FeatureCollection",
        "features": feats,
        "properties": {
            "domain": domain,
            "bbox": AOI_AIR,
            "server_time": t.isoformat(),
            "server_time_unix": int(t.timestamp()),
            "n": len(feats),
            "cache_s": CACHE_S,
            "cached": False,
        },
    }
    _cache[key] = (time.time(), payload)
    return payload
