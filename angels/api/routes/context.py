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
import threading
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
# The main Overpass server first, then two public mirrors: the whole country
# is a heavy query, and one busy server should not mean no layer.
OVERPASS_SERVERS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
ALPR_RETRY_S = 300
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


def _fetch_alpr() -> dict:
    body = urllib.parse.urlencode({"data": ctx.overpass_query_us()}).encode()
    last: Exception = TimeoutError("no Overpass server")
    for url in OVERPASS_SERVERS:
        try:
            doc = ctx.slim_alpr(json.loads(_get(url, data=body, timeout=OVERPASS_TIMEOUT_S)))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            last = exc
            continue
        doc["fetched_at"] = time.time()
        doc["server"] = urllib.parse.urlsplit(url).hostname
        _write_disk(ALPR_CACHE, doc)
        return doc
    raise last


# IN THE BACKGROUND, 2026-11-02. The whole country takes Overpass minutes,
# longer than a browser or a script will wait, so the first ask starts one
# fetch and answers at once with `loading`; the pages ask again until the
# rows are there. Only one fetch runs however many tabs ask. A failure is a
# 502 naming the source (after which it is tried again in five minutes),
# and a stale answer beats none.
_alpr_lock = threading.Lock()
_alpr_job: dict[str, Any] = {"running": False, "error": None, "failed_at": 0.0}


def _alpr_background() -> None:
    try:
        doc = _fetch_alpr()
        _cache["alpr"] = (time.time(), doc)
        _alpr_job["error"] = None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        _alpr_job["error"] = type(exc).__name__
        _alpr_job["failed_at"] = time.time()
    finally:
        _alpr_job["running"] = False


@router.get("/alpr")
def alpr(background: bool = True) -> dict:
    if "alpr" not in _cache:
        disk = _read_disk(ALPR_CACHE)
        if disk:
            _cache["alpr"] = disk
    if not background:
        return _cached("alpr", ALPR_TTL_S, _fetch_alpr)
    now = time.time()
    hit = _cache.get("alpr")
    if hit and now - hit[0] < ALPR_TTL_S:
        return hit[1]
    with _alpr_lock:
        may_retry = not _alpr_job["error"] or now - _alpr_job["failed_at"] > ALPR_RETRY_S
        if not _alpr_job["running"] and may_retry:
            _alpr_job["running"] = True
            threading.Thread(target=_alpr_background, daemon=True).start()
    if hit:
        return {**hit[1], "stale_s": int(now - hit[0])}
    if _alpr_job["error"] and not _alpr_job["running"]:
        raise HTTPException(502, f"alpr: the source did not answer ({_alpr_job['error']})")
    return {"rows": [], "n": 0, "loading": True, "evidence": ctx.CONTEXT,
            "note": "asking OpenStreetMap for the whole country; the first answer can take a few minutes"}
