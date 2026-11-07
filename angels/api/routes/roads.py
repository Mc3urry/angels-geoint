"""Road events from the CARS hubs (IN, KS, MN, NE), for both viewers.

    GET /roads/events?bbox=w,s,e,n     current events, as GeoJSON

The parsing, the access and what is kept are in angels/core/cars_hub.py.
Here: the network, the login and the cache.

ON DEMAND, IN THE BACKGROUND, as /cameras/us: nothing is asked until a
viewer asks; the first ask starts one background refresh of the five hubs
and answers at once with whatever is ready. Each hub's events then stay
five minutes; a failure is retried after two. The hub page's link to the
FEU-g feed is looked up once and kept six hours.

THE LOGIN. One username and password for every hub, from .env. urllib's
Basic-auth handler is given it for one hub's https origin at a time, and a
redirect is followed only within that origin (StayOnHub), so it is sent to
that origin and to nothing else: not to another host, not over plain http. Errors are reported as HTTP status codes and
the hub's own first words, and only when those hold neither the username
nor the password nor a URL.
"""

from __future__ import annotations

import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query

from angels import config  # noqa: F401  (loads .env, where the login is)
from angels.core import cars_hub as ch
from angels.core import land

router = APIRouter(prefix="/roads", tags=["roads"])

UA = "angels-geoint/0.1 (senior capstone, GIS; viewer only, nothing stored)"
TIMEOUT_S = 30.0
TTL_S = 5 * 60
RETRY_S = 2 * 60
LINK_TTL_S = 6 * 3600
MAX_BYTES = 64 * 1024 * 1024


def login() -> tuple[str, str] | None:
    u = os.getenv(ch.USER_ENV, "").strip()
    p = os.getenv(ch.PASSWORD_ENV, "").strip()
    return (u, p) if u and p else None


def origin(url: str) -> str:
    u = urlsplit(url)
    return f"https://{u.hostname}/"


class RedirectRefused(ValueError):
    """A redirect off the hub's https origin. Carries where it pointed, as
    scheme and host only (never a path or query), so the refusal can say."""

    def __init__(self, newurl: str) -> None:
        u = urlsplit(newurl)
        host = u.hostname or ""
        self.target = (f"{u.scheme}://{host}"
                       if re.fullmatch(r"[a-z0-9.-]{1,253}", host) and u.scheme in ("http", "https")
                       else "elsewhere")
        super().__init__("refused: a redirect left the hub")


class StayOnHub(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only within the hub's own https origin.

    The password manager alone is not enough: on Windows' Python it matched
    http://hub/ to a login given for https://hub/ (2026-11-07, the test
    suite on Joshua's machine), so a redirect to plain http would have sent
    the login unencrypted. A redirect anywhere but the same https origin is
    refused before any request is made."""

    def __init__(self, origins: Iterable[str]) -> None:
        super().__init__()
        self.origins = set(origins)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urlsplit(newurl)
        if u.scheme != "https" or f"https://{u.hostname}/" not in self.origins:
            raise RedirectRefused(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def origins_for(index_url: str, also: Iterable[str] = ()) -> list[str]:
    return [origin(index_url)] + [f"https://{h}/" for h in also]


def opener(index_url: str, user: str, password: str,
           also: Iterable[str] = ()) -> urllib.request.OpenerDirector:
    """An opener that sends the login, pre-emptively, to this hub's https
    origin and to the hosts in `also` (cars_hub.TRUSTED_REDIRECTS) only, and
    will not be redirected anywhere else."""
    allowed = origins_for(index_url, also)
    mgr = urllib.request.HTTPPasswordMgrWithPriorAuth()
    for o in allowed:
        mgr.add_password(None, o, user, password, is_authenticated=True)
    return urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(mgr), StayOnHub(allowed))


def _get(op: urllib.request.OpenerDirector, url: str, accept: str, final: list | None = None) -> bytes:
    """The body; and, into `final`, the URL it came from after redirects."""
    if urlsplit(url).scheme != "https":
        raise ValueError("refused: not https")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with op.open(req, timeout=TIMEOUT_S) as r:
        if final is not None:
            final.append(r.geturl())
        return r.read(MAX_BYTES)


def _scrub(msg: str) -> str:
    """Words that may be shown: none that carry a URL or the login."""
    lg = login()
    low = msg.lower()
    if any(t in low for t in ("http", "://", "password", "authorization")):
        return ""
    if lg and any(s and s in msg for s in lg):
        return ""
    return msg


def _http_reason(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read(600).decode("utf-8", "replace")
    except Exception:                       # noqa: BLE001 - a reason is optional
        return ""
    return _scrub(" ".join(re.sub(r"<[^>]+>|[{}\[\]\"]", " ", raw).split())[:160])


def error_text(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 401:
            return "HTTP 401 (the hub refused the login)"
        reason = _http_reason(exc)
        return f"HTTP {exc.code}{f' ({reason})' if reason else ''}"
    if isinstance(exc, RedirectRefused):
        return f"refused: a redirect left the hub, to {exc.target}"
    if isinstance(exc, ValueError):
        return _scrub(str(exc))[:200] or "ValueError"
    return type(exc).__name__


_lock = threading.Lock()
_state: dict[str, dict[str, Any]] = {}      # state -> {feats, counts, at, error, loading, ...}
_links: dict[str, tuple[float, dict]] = {}  # state -> (when looked up, feed_links())
_refreshing = False


def links_for(st: str, op, index_url: str, now: float) -> dict:
    got = _links.get(st)
    if got and now - got[0] < LINK_TTL_S and got[1]["feu_g"]:
        return got[1]
    final: list[str] = []
    html = _get(op, index_url, "text/html", final).decode("utf-8", "replace")
    hosts = {urlsplit(index_url).hostname, *ch.TRUSTED_REDIRECTS.get(st, ())}
    found = ch.feed_links(html, final[0] if final else index_url, hosts)
    if not found["feu_g"]:
        raise ValueError("the hub page answered a sign-in form, not its feed list"
                         if ch.looks_like_sign_in(html)
                         else "no FEU-g link on the hub page")
    _links[st] = (now, found)
    return found


def fetch_state(st: str) -> tuple[list[dict], dict, dict]:
    lg = login()
    if lg is None:
        raise ValueError("no login")
    _pub, index_url = ch.HUBS[st]
    op = opener(index_url, *lg, also=ch.TRUSTED_REDIRECTS.get(st, ()))
    found = links_for(st, op, index_url, time.time())
    raw = _get(op, found["feu_g"][0], "application/xml, text/xml")
    feats, counts = ch.parse_feu(raw, st)
    return feats, counts, found


def _refresh(states: list[str]) -> None:
    global _refreshing

    def one(st: str) -> None:
        try:
            feats, counts, found = fetch_state(st)
            with _lock:
                _state[st] = {"feats": feats, "counts": counts, "at": time.time(), "error": None,
                              "loading": False, "cctv_links": len(found["cctv"])}
        except Exception as exc:            # noqa: BLE001 - one hub's failure is its own
            with _lock:
                old = _state.get(st) or {}
                _state[st] = {**old, "feats": old.get("feats") or [], "at": old.get("at") or 0,
                              "error": error_text(exc), "loading": False, "failed_at": time.time()}

    try:
        with ThreadPoolExecutor(max_workers=len(ch.HUBS)) as ex:
            list(ex.map(one, states))
    finally:
        with _lock:
            _refreshing = False


def _stale(st: str, now: float) -> bool:
    s = _state.get(st)
    if s is None:
        return True
    if s.get("error"):
        return now - s.get("failed_at", 0) > RETRY_S
    return now - s["at"] > TTL_S


@router.get("/events")
def events(bbox: str | None = Query(None, description="west,south,east,north"),
           background: bool = True) -> dict:
    global _refreshing
    if login() is None:
        raise HTTPException(status_code=503, detail=(
            f"no CARS hub login: add {ch.USER_ENV} and {ch.PASSWORD_ENV} to .env "
            "and restart the API"))
    try:
        box = land.parse_bbox(bbox) if bbox else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    now = time.time()
    with _lock:
        due = [st for st in ch.HUBS if _stale(st, now)]
        start = bool(due) and not _refreshing
        if start:
            _refreshing = True
            for st in due:
                _state.setdefault(st, {"feats": [], "at": 0, "error": None})["loading"] = True
    if start:
        if background:
            threading.Thread(target=_refresh, args=(due,), daemon=True).start()
        else:
            _refresh(due)
    with _lock:
        feats: list[dict] = []
        sources: dict[str, dict] = {}
        n_all = 0
        for st, (pub, _url) in ch.HUBS.items():
            s = _state.get(st) or {}
            fs = s.get("feats") or []
            n_all += len(fs)
            feats.extend(f for f in fs if ch.in_bbox(f, box))
            sources[st] = {"publisher": pub, "n": len(fs), "loading": bool(s.get("loading")),
                           "error": s.get("error"),
                           "age_s": round(now - s["at"]) if s.get("at") else None,
                           "messages": (s.get("counts") or {}).get("messages"),
                           "left_out": (s.get("counts") or {}).get("ended_or_expired_or_no_road"),
                           "cctv_links": s.get("cctv_links")}
        loading = any(v["loading"] for v in sources.values())
    by_kind: dict[str, int] = {}
    for f in feats:
        k = f["properties"]["kind"]
        by_kind[k] = by_kind.get(k, 0) + 1
    return {"type": "FeatureCollection", "features": feats,
            "properties": {"n": n_all, "n_in_view": len(feats), "by_kind": by_kind,
                           "loading": loading, "sources": sources, "gaps": ch.GAPS,
                           "evidence": "declared",
                           "note": ("declared by each state's 511 system through the CARS hub; "
                                    "viewer only, never archived, not part of Phase G")}}
