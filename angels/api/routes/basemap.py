"""The 3D view's light basemap: CARTO Positron tiles, with the key added here.

    GET /basemap/status                          {"carto": true|false}, never the key
    GET /basemap/carto/{style}/{z}/{x}/{y}.png   one tile, fetched with the key

CARTO's raster tiles started to need a key in 2026, and an unkeyed request
answers with a tile that says "API KEY REQUIRED" across it (Joshua,
2026-10-28). CARTO takes the key only as a `?key=` query parameter, and this
project keeps credentials out of URLs a browser can see (config.py, the
WMATA note): a key in the page's tile URLs would be in every devtools
network list, every proxy log and any screenshot of either. So the browser
asks this API for a tile, and the API asks CARTO with the key. The key is
read from `.env` (CARTO_BASEMAPS_KEY), lives only in this process, and is
never in a response, a log line or an error message.

Without a key the status says so, and the 3D view falls back to Esri's
keyless light-grey basemap rather than drawing a page of watermarks.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from angels import config

router = APIRouter(prefix="/basemap", tags=["basemap"])

STYLES = {"light_all", "light_nolabels", "dark_all"}
MAX_Z = 20
TIMEOUT_S = 20.0
UA = "angels-geoint/0.1 (senior capstone, GIS)"
# The key may be restricted to referers; these requests come from the same
# machine the key was registered for, and say so.
REFERER = "http://localhost:8000/"
CARTO = "https://{s}.basemaps.cartocdn.com/{style}/{z}/{x}/{y}.png"

# Recently served tiles, so panning back does not ask CARTO again.
_CACHE_MAX = 3000
_cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
_TTL_S = 24 * 3600


def _key() -> str:
    return (getattr(config, "CARTO_BASEMAPS_KEY", "") or "").strip()


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": REFERER})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return r.read(8 * 1024 * 1024)


@router.get("/status")
def status() -> dict:
    return {"carto": bool(_key()),
            "fallback": "Esri World Light Gray (keyless)",
            "how": "set CARTO_BASEMAPS_KEY in .env and restart the API"}


@router.get("/carto/{style}/{z}/{x}/{y}.png")
def carto(style: str, z: int, x: int, y: int) -> Response:
    if style not in STYLES or not (0 <= z <= MAX_Z and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(404, "no such tile")
    key = _key()
    if not key:
        raise HTTPException(503, "CARTO_BASEMAPS_KEY is not set in .env")
    name = f"{style}/{z}/{x}/{y}"
    hit = _cache.get(name)
    if hit and time.time() - hit[0] < _TTL_S:
        _cache.move_to_end(name)
        png = hit[1]
    else:
        url = (CARTO.format(s="abcd"[(x + y) % 4], style=style, z=z, x=x, y=y)
               + "?" + urllib.parse.urlencode({"key": key}))
        try:
            png = _get(url)
        except urllib.error.HTTPError as exc:
            # The status only: the exception's own text can carry the URL,
            # and the URL carries the key.
            raise HTTPException(502, f"CARTO answered HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise HTTPException(502, "CARTO did not answer") from None
        _cache[name] = (time.time(), png)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "max-age=86400"})
