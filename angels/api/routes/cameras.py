"""Traffic cameras across the US, for the 3D view.

    GET /cameras/us              every camera the sources below answered for
    GET /cameras/txdot-frame     one TxDOT frame, decoded (TxDOT sends base64 in JSON)
    GET /cameras/probe?id=       whether a camera's live stream plays without a sign-in

The parsing, the sources and what is kept are described in
angels/core/cameras_us.py. Here: the network and the cache.

ON DEMAND, IN THE BACKGROUND. Nothing is fetched until the 3D view asks.
The first ask starts one background refresh of every source (about ninety
requests, sixteen at a time) and answers at once with whatever is ready;
each source then stays for fifteen minutes. The page asks again while
anything is still loading. A source that fails says so by name in
`sources`, and keeps its last good answer if it had one.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from angels import config  # noqa: F401  (loads .env, where the camera keys are)
from angels.core import cameras_us as cu
from angels.core import land

router = APIRouter(prefix="/cameras", tags=["cameras"])

UA = "angels-geoint/0.1 (senior capstone, GIS; viewer only, nothing stored)"
TIMEOUT_S = 25.0
TTL_S = 15 * 60
WORKERS = 16


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return r.read(64 * 1024 * 1024)


def _json(url: str, encoding: str = "utf-8", headers: dict | None = None) -> Any:
    if headers:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **headers})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.loads(r.read(64 * 1024 * 1024).decode(encoding, "replace"))
    return json.loads(_get(url).decode(encoding, "replace"))


def keys() -> dict[str, bool]:
    """Which keyed state sources have a key. Presence only, never a value."""
    k = {st: bool(os.getenv(cu.iteris_env(st), "").strip()) for st in cu.ITERIS}
    k["OH"] = bool(os.getenv(cu.OHGO_ENV, "").strip())
    return k


def sources() -> dict[str, tuple[str, Callable[[], list[list]]]]:
    """key -> (publisher, fetch-and-parse)."""
    s: dict[str, tuple[str, Callable[[], list[list]]]] = {
        "chart": ("Maryland CHART", lambda: cu.from_chart(land.cameras())),
        "wsdot": ("WSDOT", lambda: cu.parse_wsdot(_json(cu.WSDOT_URL, "windows-1252"))),
        "austin": ("City of Austin", lambda: cu.parse_austin(_json(cu.AUSTIN_URL))),
        "deldot": ("DelDOT", lambda: cu.parse_deldot(_json(cu.DELDOT_URL))),
        "king": ("King County", lambda: cu.parse_king(_json(cu.KING_URL))),
    }
    for d in cu.CALTRANS_DISTRICTS:
        s[f"caltrans-{d}"] = ("Caltrans", lambda d=d: cu.parse_caltrans(_json(cu.CALTRANS_URL.format(d=d)), d))
    for d in cu.TXDOT_DISTRICTS:
        s[f"txdot-{d}"] = ("TxDOT", lambda d=d: cu.parse_txdot(_json(cu.TXDOT_URL.format(d=d)), d))
    have = keys()
    for st, (name, base) in cu.ITERIS.items():
        if have[st]:
            # The key travels in the query string, as the platform requires;
            # it is built here, used once, and never returned or logged.
            s[f"511-{st}"] = (name, lambda st=st, name=name, base=base: cu.parse_iteris(
                _json(base + cu.ITERIS_PATH + "?" + urllib.parse.urlencode(
                    {"key": os.getenv(cu.iteris_env(st), "").strip(), "format": "json"})), st, name))
    if have["OH"]:
        s["ohgo"] = ("OHGO (Ohio DOT)", lambda: cu.parse_ohgo(_json(
            cu.OHGO_URL, headers={"Authorization": f"APIKEY {os.getenv(cu.OHGO_ENV, '').strip()}"})))
    for st in cu.LTC_STATES:
        if have.get(st):
            continue                      # read from the state itself
        s[f"ltc-{st}"] = ("LiveTrafficCam", lambda st=st: cu.parse_ltc(_json(cu.LTC_URL.format(s=st)), st))
    return s


_lock = threading.Lock()
_state: dict[str, dict[str, Any]] = {}     # key -> {rows, at, error, loading}
_refreshing = False


def _refresh(keys: list[str], table: dict) -> None:
    global _refreshing

    def one(k: str) -> None:
        try:
            rows = table[k][1]()
            with _lock:
                _state[k] = {"rows": rows, "at": time.time(), "error": None, "loading": False}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
            err = (f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError)
                   else type(exc).__name__)
            with _lock:
                old = _state.get(k) or {}
                _state[k] = {"rows": old.get("rows") or [], "at": old.get("at") or time.time(),
                             "error": err, "loading": False, "failed_at": time.time()}

    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            list(ex.map(one, keys))
    finally:
        with _lock:
            _refreshing = False


def _stale(k: str, now: float) -> bool:
    st = _state.get(k)
    if st is None:
        return True
    if st.get("error"):
        return now - st.get("failed_at", 0) > 120      # retry a failure after two minutes
    return now - st["at"] > TTL_S


@router.get("/us")
def cameras_us(background: bool = True) -> dict:
    global _refreshing
    table = sources()
    now = time.time()
    with _lock:
        due = [k for k in table if _stale(k, now)]
        start = bool(due) and not _refreshing
        if start:
            _refreshing = True
            for k in due:
                _state.setdefault(k, {"rows": [], "at": 0, "error": None})["loading"] = True
    if start:
        if background:
            threading.Thread(target=_refresh, args=(due, table), daemon=True).start()
        else:
            _refresh(due, table)
    with _lock:
        rows: list[list] = []
        by_pub: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        for k, (pub, _) in table.items():
            st = _state.get(k) or {}
            for r in st.get("rows") or []:
                if r[0] not in seen:
                    seen.add(r[0])
                    rows.append(r)
            b = by_pub.setdefault(pub, {"n": 0, "loading": 0, "failed": [], "asked": 0})
            b["asked"] += 1
            b["n"] += len(st.get("rows") or [])
            if st.get("loading"):
                b["loading"] += 1
            if st.get("error"):
                b["failed"].append(f"{k}: {st['error']}")
        loading = any((_state.get(k) or {}).get("loading") for k in table)
    return {"cols": cu.COLS, "rows": rows, "n": len(rows), "loading": loading,
            "sources": by_pub,
            "keys": keys(),
            "evidence": "where the state can see the road: public DOT and 511 feeds",
            "kept": "locations only; pictures are played from each publisher and never stored"}


# -- will this stream play? -----------------------------------------------------
#
# A browser asked for a stream that needs a password shows a sign-in box
# from the publisher's server (DriveNC, 2026-11-02). So before the page plays
# a stream it asks here, and this asks the publisher for the playlist with no
# credentials. Only a camera this API listed can be probed: the URL comes
# from our own rows, never from the request. A host that asked for a sign-in
# is remembered for ten minutes, so its other cameras are not asked again.

PROBE_TIMEOUT_S = 8.0
_auth_hosts: dict[str, tuple[float, str]] = {}


def _row_of(cam_id: str) -> list | None:
    with _lock:
        for st in _state.values():
            for r in st.get("rows") or []:
                if r[0] == cam_id:
                    return r
    return None


def _fetch_head(url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as r:
        return r.status, r.read(4096)


@router.get("/probe")
def probe(id: str = Query(..., max_length=120)) -> dict:
    row = _row_of(id)
    if row is None:
        raise HTTPException(404, "not a camera this API listed")
    col = {c: i for i, c in enumerate(cu.COLS)}
    if row[col["kind"]] != cu.HLS:
        return {"ok": True, "why": "not a stream"}
    url = row[col["media"]]
    host = urllib.parse.urlsplit(url).hostname or ""
    hit = _auth_hosts.get(host)
    if hit and time.time() - hit[0] < 600:
        return {"ok": False, "why": hit[1]}
    try:
        status, body = _fetch_head(url)
        ok = cu.playlist_ok(status, body)
        why = "plays" if ok else "the publisher sent something that is not a playlist"
    except urllib.error.HTTPError as exc:
        ok, why = False, ("the publisher asks for a sign-in" if exc.code in (401, 403)
                          else f"the publisher answered HTTP {exc.code}")
        if exc.code in (401, 403):
            _auth_hosts[host] = (time.time(), why)
    except (urllib.error.URLError, TimeoutError, OSError):
        ok, why = False, "the publisher did not answer"
    return {"ok": ok, "why": why}


_frames: dict[str, tuple[float, bytes]] = {}


@router.get("/txdot-frame")
def txdot_frame(district: str = Query(...), icd: str = Query(...)) -> Response:
    try:
        url = cu.txdot_frame_url(icd, district)
    except ValueError:
        raise HTTPException(404, "not a TxDOT camera") from None
    hit = _frames.get(url)
    if hit and time.time() - hit[0] < 10:
        jpeg = hit[1]
    else:
        try:
            jpeg = cu.txdot_frame_jpeg(_json(url))
        except urllib.error.HTTPError as exc:
            raise HTTPException(502, f"TxDOT answered HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise HTTPException(502, "TxDOT sent no frame") from None
        _frames[url] = (time.time(), jpeg)
        if len(_frames) > 200:
            _frames.pop(next(iter(_frames)))
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
