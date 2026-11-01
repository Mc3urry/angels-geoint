"""Keyless context layers for the 3D view, fetched server-side and cached.

    GET /context/satellites   TLEs: stations, visual, GPS (CelesTrak)
    GET /context/alpr         OSM ALPR camera locations, the whole US (viewer-only scope exception)

Fetched here rather than in the browser so that one cached upstream call
serves every open tab. Each source is asked at most once per TTL. A failure
is answered as a 502 naming the source, never as an empty layer: an empty
layer would read as "none here".

The ALPR answer is also kept in one file, `data/interim/context/alpr-us.json`,
overwritten each day and never tracked, so restarting the API does not ask
Overpass for the country again. It is a cache, not an archive: there is
only ever one, and no analysis reads it.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from angels.config import INTERIM
from angels.core import context as ctx

router = APIRouter(prefix="/context", tags=["context"])

UA = ("angels-geoint/0.1 (senior capstone, GIS; contact via "
      "github.com/Mc3urry/angels-geoint)")
TIMEOUT_S = 30.0
OVERPASS_TIMEOUT_S = 260.0

CELESTRAK = "https://celestrak.org/NORAD/elements/gp.php?GROUP={g}&FORMAT=tle"
SAT_GROUPS = ("stations", "visual", "gps-ops")
OVERPASS = "https://overpass-api.de/api/interpreter"
ALPR_TTL_S = 24 * 3600
ALPR_CACHE = INTERIM / "context" / "alpr-us.json"

_cache: dict[str, tuple[float, Any]] = {}


def _get(url: str, *, data: bytes | None = None, timeout: float = TIMEOUT_S) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(256 * 1024 * 1024)


def _cached(key: str, ttl_s: float, make):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl_s:
        return hit[1]
    try:
        value = make()
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        if hit:                                   # stale beats nothing, and says so
            v = hit[1]
            return {**v, "stale_s": int(time.time() - hit[0])} if isinstance(v, dict) else v
        raise HTTPException(502, f"{key}: the source did not answer ({type(exc).__name__})") from None
    _cache[key] = (time.time(), value)
    return value


@router.get("/satellites")
def satellites() -> dict:
    def make():
        sats: list[dict] = []
        seen: set[str] = set()
        for g in SAT_GROUPS:
            for s in ctx.parse_tle(_get(CELESTRAK.format(g=g)).decode("utf-8", "replace"), g):
                if s["line1"][2:7] not in seen:
                    seen.add(s["line1"][2:7])
                    sats.append(s)
        return {"satellites": sats, "n": len(sats), "groups": list(SAT_GROUPS),
                "source": "CelesTrak", "evidence": ctx.CONTEXT}
    return _cached("satellites", 6 * 3600, make)


def _read_disk(path: Path) -> tuple[float, dict] | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return float(doc["fetched_at"]), doc
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_disk(path: Path, doc: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass                                      # a cache that cannot be written is only slower


@router.get("/alpr")
def alpr() -> dict:
    path = ALPR_CACHE
    if "alpr" not in _cache:
        disk = _read_disk(path)
        if disk:
            _cache["alpr"] = disk

    def make():
        body = urllib.parse.urlencode({"data": ctx.overpass_query_us()}).encode()
        doc = ctx.slim_alpr(json.loads(_get(OVERPASS, data=body, timeout=OVERPASS_TIMEOUT_S)))
        doc["fetched_at"] = time.time()
        _write_disk(path, doc)
        return doc
    return _cached("alpr", ALPR_TTL_S, make)
