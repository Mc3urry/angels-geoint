"""Live road speeds for the 3D view: TomTom's traffic-flow tiles, keyed here.

    GET /traffic/status                    {"flow": true|false}, never the key
    GET /traffic/flow/{z}/{x}/{y}.png      one flow tile, colour = speed against free flow

HOW ALL TRAFFIC IS MOVING, WITHOUT FOLLOWING ANYONE. A private car does not
broadcast its position the way an aircraft or a ship does, and following
one would break the project's scope (README, "Scope"). TomTom's flow layer
is the honest substitute: a speed for every road segment, aggregated by
TomTom from many connected vehicles, with no vehicle in it. It is the same
kind of fact the land domain already uses (counts and speeds, never
identities), and it is shown for the viewer only.

The key is TOMTOM_API_KEY in .env (developer.tomtom.com, free tier: 50,000
map tiles a day). TomTom takes it in the query string, so it is added here
and never reaches the browser, a response or an error message (the CARTO
rule, routes/basemap.py). A tile is kept for two minutes: flow is updated
about once a minute upstream, and panning back should not spend the quota.
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from angels import config  # noqa: F401  (loads .env)

router = APIRouter(prefix="/traffic", tags=["traffic"])

ENV = "TOMTOM_API_KEY"
FLOW = "https://api.tomtom.com/traffic/map/4/tile/flow/relative0/{z}/{x}/{y}.png"
MAX_Z = 18
TTL_S = 120
_cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
_CACHE_MAX = 2000


def _key() -> str:
    return os.getenv(ENV, "").strip()


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "angels-geoint/0.1 (senior capstone)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(4 * 1024 * 1024)


@router.get("/status")
def status() -> dict:
    return {"flow": bool(_key()), "how": f"set {ENV} in .env and restart the API",
            "source": "TomTom Traffic Flow, relative to free-flow speed",
            "means": "aggregate speed per road segment; no vehicle is identified"}


@router.get("/flow/{z}/{x}/{y}.png")
def flow(z: int, x: int, y: int) -> Response:
    if not (0 <= z <= MAX_Z and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(404, "no such tile")
    key = _key()
    if not key:
        raise HTTPException(503, f"{ENV} is not set in .env")
    name = f"{z}/{x}/{y}"
    hit = _cache.get(name)
    if hit and time.time() - hit[0] < TTL_S:
        png = hit[1]
    else:
        url = FLOW.format(z=z, x=x, y=y) + "?" + urllib.parse.urlencode({"key": key, "tileSize": 256})
        try:
            png = _get(url)
        except urllib.error.HTTPError as exc:
            raise HTTPException(502, f"TomTom answered HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise HTTPException(502, "TomTom did not answer") from None
        _cache[name] = (time.time(), png)
        _cache.move_to_end(name)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return Response(png, media_type="image/png", headers={"Cache-Control": f"max-age={TTL_S}"})
