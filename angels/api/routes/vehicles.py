"""Transit and intercity rail across the US, for the 3D view only.

    GET /vehicles/us       every transit vehicle the keyless US feeds report
    GET /vehicles/amtrak   Amtrak, VIA Rail and Brightline trains, via Amtraker
    GET /vehicles/plows    state DOT snowplows that publish their positions

VIEWER ONLY. The research collector (scripts/fetch_vehicles.py, footprint
`roads-vehicles`) polls the 38 border feeds G2b selected, and archives them;
that is the land question's evidence and nothing here changes it. This
route shows the rest of the country, and keeps nothing:

  * the 38 border feeds come from the collector's own live snapshot, so
    they are never polled twice;
  * the other keyless US feeds in the transit registry (G2, about 150) are
    polled by this process, every 30 s, only while the 3D view is asking,
    and stop two minutes after it stops;
  * nothing is written to disk. The last poll lives in memory.

The parsing is the collector's own (rows_from, derive_motion, _snap_row), so
a vehicle is read the same way in both places.

KEYED FEEDS. Some of the biggest systems publish positions only behind a
free key: the Bay Area (511.org, every agency in one feed), New York City
buses (MTA Bus Time), Chicago's L (CTA Train Tracker), Portland (TriMet),
the Puget Sound agencies (OneBusAway), Metra, and the ~55 agencies hosted
by Swiftly (LA Metro, SEPTA rail, Maryland MTA, Miami-Dade, VTA, Las Vegas
RTC, Kansas City...). Each joins the national poll when its key is in .env
(see .env.example). A key that travels in a query string is put there only
inside this process; responses carry feed ids and outcomes, never URLs.

Amtrak positions come from Amtraker (api.amtraker.com), a free community API
over Amtrak's own train map; it carries VIA Rail and Brightline too.
"""

from __future__ import annotations

import importlib
import json
import os
import urllib.parse
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from angels import config  # noqa: F401  (loads .env, where the keys are)
from angels.config import INTERIM, LIVE, REFERENCE
from angels.core import land

router = APIRouter(prefix="/vehicles", tags=["vehicles"])

REGISTRY = REFERENCE / "roads" / "transit-registry.json"
BORDER_SNAPSHOT = LIVE / "roads-vehicles.json"
BORDER_MAX_AGE_S = 120
INTERVAL_S = 30.0
IDLE_STOP_S = 120.0
WORKERS = 16
# A cycle waits this long for its feeds, then publishes what has answered.
# A slow feed keeps its last answer and is not asked again until it is done
# (2026-11-02: one cycle used to wait for the slowest of ~150 feeds, and any
# feed that raised sank the whole cycle, so the view said "warming up" for
# minutes, or for ever).
CYCLE_WAIT_S = 25.0

# SWIFTLY'S BUDGET (2026-11-03). One Swiftly key serves about sixty
# agencies and allows 180 requests per 15 minutes across all of them; at
# INTERVAL_S that budget would be gone in a minute. So Swiftly feeds share
# SWIFTLY_BUDGET requests per window (headroom kept below 180), spread
# evenly: with sixty agencies each is asked about every six minutes. A 429
# pauses all of them for five minutes. The request times, and nothing else,
# are kept in one small file so that restarting the API does not spend the
# window twice.
SWIFTLY_HOST = "api.goswift.ly"
SWIFTLY_WINDOW_S = 900.0
SWIFTLY_BUDGET = 150
SWIFTLY_PAUSE_S = 300.0
SWIFTLY_LOG = INTERIM / "transit" / "swiftly-requests.json"
# A Swiftly key opens only the agencies it was granted for. An agency that
# refuses it (401 or 403) is asked again after six hours, not every turn,
# so refusals do not spend the budget the granted agencies need.
SWIFTLY_DENIED_S = 6 * 3600.0
UA = "angels-geoint/0.1 (senior capstone, GIS; viewer only, nothing stored)"
AMTRAKER = "https://api.amtraker.com/v3/trains"
COMPASS = {"N": 0, "NE": 45, "E": 90, "SE": 135, "S": 180, "SW": 225, "W": 270, "NW": 315}


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _with(url: str, **params: str) -> str:
    return url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)


def keyed_feeds(doc: dict) -> list[dict]:
    """GTFS-Realtime feeds that need a key, for the keys present in .env."""
    out: list[dict] = []
    excluded = [e for e in doc.get("excluded") or [] if e.get("rule") == "needs a key"]
    host = lambda e: urllib.parse.urlsplit(e.get("url", "")).hostname or ""  # noqa: E731
    if _env("API_511_ORG_KEY"):
        # One feed for every Bay Area agency ("RG" is 511.org's regional code).
        out.append({"id": "511org-RG", "provider": "511 SF Bay Area (all agencies)",
                    "url": _with("https://api.511.org/transit/vehiclepositions",
                                 agency="RG", api_key=_env("API_511_ORG_KEY"))})
    if _env("MTA_BUSTIME_KEY"):
        out.append({"id": "mta-bustime", "provider": "MTA New York City buses",
                    "url": _with("https://gtfsrt.prod.obanyc.com/vehiclePositions", key=_env("MTA_BUSTIME_KEY"))})
    for e in excluded:
        h = host(e)
        if h == "api.goswift.ly" and _env("SWIFTLY_API_KEY"):
            out.append({**e, "header": ("Authorization", _env("SWIFTLY_API_KEY"))})
        elif h == "api.pugetsound.onebusaway.org" and _env("OBA_PUGET_SOUND_KEY"):
            out.append({**e, "url": _with(e["url"], key=_env("OBA_PUGET_SOUND_KEY"))})
        elif h == "developer.trimet.org" and _env("TRIMET_APP_ID"):
            out.append({**e, "url": _with(e["url"], appID=_env("TRIMET_APP_ID"))})
        elif h == "gtfspublic.metrarr.com" and _env("METRA_API_TOKEN"):
            out.append({**e, "url": _with(e["url"], api_token=_env("METRA_API_TOKEN"))})
    return out


def national_feeds(registry: Path = REGISTRY, border: dict | None = None) -> list[dict]:
    """Every US vehicle-position feed the research collector does not
    already poll: the keyless registry feeds, plus keyed ones with a key."""
    try:
        doc = json.loads(Path(registry).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    border = border if border is not None else land.selection_index()
    feeds = [f for f in doc.get("selected") or []
             if f.get("id") not in border and str(f.get("url", "")).startswith(("http://", "https://"))]
    out, urls = [], set()
    for f in feeds + [f for f in keyed_feeds(doc) if f.get("id") not in border]:
        if f["url"] in urls:              # one feed listed twice is asked once
            continue
        urls.add(f["url"])
        out.append(f)
    return out


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url or "").hostname or ""


# CTA Train Tracker is not GTFS-Realtime: its own JSON, one call for every
# L line. Rows come out in the snapshot's columns.
CTA_URL = "https://lapi.transitchicago.com/api/1.0/ttpositions.aspx"
CTA_LINES = "red,blue,brn,g,org,p,pink,y"


def parse_cta(doc: dict, now: int) -> list[list]:
    routes = ((doc or {}).get("ctatt") or {}).get("route") or []
    out = []
    for r in routes if isinstance(routes, list) else [routes]:
        trains = r.get("train") or []
        for t in trains if isinstance(trains, list) else [trains]:
            try:
                lat, lon = float(t["lat"]), float(t["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            try:
                hdg = float(t.get("heading"))
            except (TypeError, ValueError):
                hdg = None
            out.append(["cta-train", f"rn-{t.get('rn')}", str(t.get("rn") or ""), str(r.get("@name") or ""),
                        lat, lon, hdg, None, now, True, "none"])
    return out


class NationalPoller:
    def __init__(self, feeds_fn=national_feeds, fv=None) -> None:
        self.feeds_fn = feeds_fn
        self._fv = fv
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.wanted_at = 0.0
        self.doc: dict[str, Any] | None = None
        self.previous: dict[str, dict] = {}
        self.error: str | None = None
        self.ex: ThreadPoolExecutor | None = None
        self.inflight: dict[str, Any] = {}           # feed id -> future
        self.last: dict[str, tuple[str, list]] = {}  # feed id -> (outcome, rows)
        self.polled_at: dict[str, float] = {}
        self.swiftly_log: list[float] | None = None  # request times in the window
        self.swiftly_pause_until = 0.0
        self.swiftly_every_s = 0.0
        self.denied_until: dict[str, float] = {}
        self.swiftly_log_path = SWIFTLY_LOG

    def _swiftly_times(self, now: float) -> list[float]:
        if self.swiftly_log is None:
            # Request times AND refusals survive a restart: on 2026-11-03 each
            # restart asked the sixty refusing agencies again, a third of the
            # window's budget every time.
            try:
                doc = json.loads(self.swiftly_log_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                doc = []
            if isinstance(doc, list):               # the first format: times only
                doc = {"times": doc}
            try:
                self.swiftly_log = [float(t) for t in doc.get("times") or []]
                for fid, until in (doc.get("denied") or {}).items():
                    self.denied_until[str(fid)] = max(float(until), self.denied_until.get(str(fid), 0.0))
            except (TypeError, ValueError, AttributeError):
                self.swiftly_log = []
        self.swiftly_log = [t for t in self.swiftly_log if now - t < SWIFTLY_WINDOW_S]
        return self.swiftly_log

    def _save_swiftly(self) -> None:
        try:
            self.swiftly_log_path.parent.mkdir(parents=True, exist_ok=True)
            now = time.time()
            self.swiftly_log_path.write_text(json.dumps({
                "times": [round(t, 1) for t in self.swiftly_log or []],
                "denied": {k: round(t, 1) for k, t in self.denied_until.items() if t > now}}),
                encoding="utf-8")
        except OSError:
            pass                                      # the in-memory budget still holds

    def due(self, feed: dict, n_swiftly: int, now: float) -> bool:
        """Every feed each cycle, except Swiftly's, which share a budget."""
        if _host(feed.get("url", "")) != SWIFTLY_HOST:
            return True
        self._swiftly_times(now)                  # loads the saved refusals on first use
        if now < self.swiftly_pause_until or now < self.denied_until.get(feed["id"], 0.0):
            return False
        # The budget is spread over the agencies that accept the key.
        n_open = max(1, n_swiftly - sum(1 for t in self.denied_until.values() if t > now))
        self.swiftly_every_s = max(INTERVAL_S, SWIFTLY_WINDOW_S * n_open / SWIFTLY_BUDGET)
        if now - self.polled_at.get(feed["id"], 0.0) < self.swiftly_every_s:
            return False
        times = self._swiftly_times(now)
        if len(times) >= SWIFTLY_BUDGET:
            return False
        times.append(now)
        return True

    def swiftly_status(self, now: float) -> dict:
        return {"used": len(self._swiftly_times(now)), "budget": SWIFTLY_BUDGET,
                "refused": sum(1 for t in self.denied_until.values() if t > now),
                "window_s": int(SWIFTLY_WINDOW_S), "every_s": int(self.swiftly_every_s),
                "paused_s": max(0, int(self.swiftly_pause_until - now))}

    def fv(self):
        if self._fv is None:
            # The collector's module: the API runs from the repository root,
            # where tests import it the same way.
            self._fv = importlib.import_module("scripts.fetch_vehicles")
        return self._fv

    def want(self) -> None:
        self.wanted_at = time.time()
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()

    def _run(self) -> None:
        while time.time() - self.wanted_at < IDLE_STOP_S:
            t0 = time.time()
            try:
                self.cycle()
                self.error = None
            except Exception as exc:                # noqa: BLE001 - shown, not raised
                self.error = f"{type(exc).__name__}: {exc}"[:200]
            time.sleep(max(1.0, INTERVAL_S - (time.time() - t0)))

    def safe_one(self, feed: dict) -> tuple[str, str, list[list]]:
        """One feed, and whatever goes wrong stays with that feed."""
        try:
            return self.one(feed)
        except RuntimeError as exc:                 # the bindings are missing: everyone's problem
            self.error = str(exc)[:200]
            return feed["id"], "error", []
        except Exception as exc:                    # noqa: BLE001 - recorded per feed
            return feed["id"], f"error: {type(exc).__name__}", []

    def one(self, feed: dict) -> tuple[str, str, list[list]]:
        fv = self.fv()
        now = int(time.time())
        status, body, _, _, truncated = fv.fetch(feed["url"], feed.get("header") or fv.key_for(feed["url"]))
        if status == 429 and _host(feed["url"]) == SWIFTLY_HOST:
            self.swiftly_pause_until = time.time() + SWIFTLY_PAUSE_S
        if status in (401, 403) and _host(feed["url"]) == SWIFTLY_HOST:
            self.denied_until[feed["id"]] = time.time() + SWIFTLY_DENIED_S
        msg = fv.parse(body) if status and status < 400 and body and not truncated else None
        rows, unpositioned = fv.rows_from(msg, feed["id"], now) if msg is not None else ([], 0)
        outcome = fv.classify(status, body, truncated, msg, len(rows), len(rows) + unpositioned)
        if outcome == "http_error" and status:
            outcome = f"http_error {status}"          # which error says what to do about it
        prev = self.previous.get(feed["id"])
        derived = fv.derive_motion(rows, prev)
        self.previous[feed["id"]] = {r["vehicle_key"]: fv.fix_of(r) for r in rows}
        return feed["id"], outcome, [fv._snap_row(r, derived.get(r["vehicle_key"])) for r in rows]

    def _harvest(self) -> None:
        for fid, fut in list(self.inflight.items()):
            if fut.done():
                del self.inflight[fid]
                _, outcome, rows = fut.result()
                self.last[fid] = (outcome, rows)

    def cycle(self, wait_s: float = CYCLE_WAIT_S) -> None:
        feeds = self.feeds_fn()
        if self.ex is None:
            self.ex = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="transit")
        self._harvest()
        now = time.time()
        n_swiftly = sum(1 for f in feeds if _host(f.get("url", "")) == SWIFTLY_HOST)
        asked_swiftly = False
        for f in feeds:
            if f["id"] not in self.inflight and self.due(f, n_swiftly, now):
                self.polled_at[f["id"]] = now
                asked_swiftly |= _host(f.get("url", "")) == SWIFTLY_HOST
                self.inflight[f["id"]] = self.ex.submit(self.safe_one, f)
        if asked_swiftly:
            self._save_swiftly()
        wait(list(self.inflight.values()), timeout=wait_s)
        self._harvest()
        if self.denied_until:
            self._save_swiftly()                  # refusals learnt this cycle
        ids = [f["id"] for f in feeds]
        results = [(fid, *self.last[fid]) if fid in self.last
                   else (fid, "slow" if fid in self.inflight else "queued", []) for fid in ids]
        if _env("CTA_TRAIN_KEY"):
            try:
                doc = json.loads(_get(_with(CTA_URL, key=_env("CTA_TRAIN_KEY"), rt=CTA_LINES,
                                            outputType="JSON")))
                rows = parse_cta(doc, int(time.time()))
                results.append(("cta-train", "ok" if rows else "not_positions", rows))
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                results.append(("cta-train", "fetch_error", []))
            feeds = feeds + [{"id": "cta-train", "provider": "CTA 'L' trains (Chicago)"}]
        self.doc = {
            "time": int(time.time()),
            "vehicles": [row for _, _, rows in results for row in rows],
            "feeds": {fid: outcome for fid, outcome, _ in results},
            "providers": {f["id"]: f.get("provider", "") for f in feeds},
        }


def transit_keys() -> dict[str, bool]:
    return {k: bool(_env(k)) for k in ("API_511_ORG_KEY", "MTA_BUSTIME_KEY", "SWIFTLY_API_KEY",
                                        "OBA_PUGET_SOUND_KEY", "TRIMET_APP_ID", "METRA_API_TOKEN",
                                        "CTA_TRAIN_KEY")}


poller = NationalPoller()


def _border(now: float) -> dict | None:
    try:
        doc = json.loads(BORDER_SNAPSHOT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    t = doc.get("time")
    if not isinstance(t, (int, float)) or now - t > BORDER_MAX_AGE_S:
        return None
    return doc


@router.get("/us")
def vehicles_us() -> dict:
    poller.want()
    now = time.time()
    fv_cols = ["feed_id", "vehicle_key", "label", "route_id", "lat", "lon",
               "bearing", "speed", "vehicle_ts", "in_service", "motion"]
    border = _border(now)
    rows: list[list] = []
    providers: dict[str, str] = {}
    feeds: dict[str, str] = {}
    if border:
        cols = border.get("cols") or []
        idx = [cols.index(c) if c in cols else None for c in fv_cols]
        for v in border.get("vehicles") or []:
            rows.append([v[i] if i is not None else None for i in idx])
        feeds.update(border.get("feeds") or {})
        providers.update({k: r.get("provider", "") for k, r in land.selection_index().items()})
    nat = poller.doc
    if nat:
        rows.extend(nat["vehicles"])
        feeds.update(nat["feeds"])
        providers.update(nat["providers"])
    return {
        "time": int(now), "cols": fv_cols, "vehicles": rows, "n": len(rows),
        "feeds": feeds, "providers": providers,
        "n_feeds": len(feeds), "n_answered": sum(1 for o in feeds.values() if o in ("ok", "not_positions")),
        # Warming only while the first cycle runs; a poller that failed says
        # so in `error` rather than warming for ever.
        "warming": nat is None and not poller.error,
        "national_age_s": int(now - nat["time"]) if nat else None,
        "border": ("the research collector's live snapshot" if border
                   else "the research collector is not running; border feeds missing"),
        # Which feeds are the research's (G2b), so a viewer can say which
        # vehicles the land question reads and which it only shows.
        "border_feeds": sorted(land.selection_index()),
        "error": poller.error,
        "keys": transit_keys(),
        "swiftly": poller.swiftly_status(now) if _env("SWIFTLY_API_KEY") else None,
        "evidence": "cooperative: self-reported by agency vehicles",
        "kept": "nothing: the last poll is held in memory while the 3D view asks",
    }


_amtrak: tuple[float, dict] | None = None


def _ts(s: Any) -> int | None:
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def parse_amtraker(doc: Any, now: float) -> list[list]:
    """{trainNum: [train, ...]} to rows [id, lon, lat, heading, speed_mps,
    ts, number, route, provider, origin, destination]. Active trains only."""
    out = []
    for trains in (doc.values() if isinstance(doc, dict) else []):
        for t in trains if isinstance(trains, list) else []:
            if not isinstance(t, dict) or t.get("trainState") != "Active":
                continue
            try:
                lat, lon = float(t["lat"]), float(t["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            mph = t.get("velocity")
            out.append([str(t.get("trainID") or t.get("trainNum")), round(lon, 6), round(lat, 6),
                        COMPASS.get(str(t.get("heading") or "").upper()),
                        round(float(mph) * 0.44704, 2) if isinstance(mph, (int, float)) else None,
                        _ts(t.get("updatedAt") or t.get("lastValTS")) or int(now),
                        str(t.get("trainNum") or ""), str(t.get("routeName") or ""),
                        str(t.get("provider") or "Amtrak"),
                        str(t.get("origName") or ""), str(t.get("destName") or "")])
    return out


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read(32 * 1024 * 1024)


@router.get("/amtrak")
def amtrak() -> dict:
    global _amtrak
    now = time.time()
    if _amtrak and now - _amtrak[0] < 60:
        return _amtrak[1]
    try:
        rows = parse_amtraker(json.loads(_get(AMTRAKER)), now)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        if _amtrak:
            return {**_amtrak[1], "stale_s": int(now - _amtrak[0])}
        raise HTTPException(502, f"Amtraker did not answer ({type(exc).__name__})") from None
    doc = {"cols": ["id", "lon", "lat", "heading", "speed_mps", "ts", "number", "route",
                    "provider", "origin", "destination"],
           "rows": rows, "n": len(rows), "time": int(now),
           "source": "Amtraker (api.amtraker.com), over Amtrak's own train map",
           "evidence": "cooperative: positions the railways report for their own trains"}
    _amtrak = (now, doc)
    return doc


# -- DOT snowplows -------------------------------------------------------------------
#
# Fleets a state publishes as a public ArcGIS layer. The item id is the
# state's own catalogue entry; its service address is looked up from ArcGIS
# Online each day, so a republished service is followed rather than broken.
# Iowa DOT: "Snow Plow Truck Location AVL", every plow moving above 3 mph,
# updated every two minutes. Seasonal: in summer it is honestly empty.

PLOW_ITEMS = {"IA": ("Iowa DOT", "20a0c10c06a54240b5f2893e0187e22c")}
ARCGIS_ITEM = "https://www.arcgis.com/sharing/rest/content/items/{id}?f=json"
_plow_service: dict[str, tuple[float, str]] = {}
_plows: tuple[float, dict] | None = None


def _service_url(item_id: str) -> str:
    hit = _plow_service.get(item_id)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    url = (json.loads(_get(ARCGIS_ITEM.format(id=item_id))) or {}).get("url") or ""
    if not url.startswith("https://"):
        raise ValueError("no service url")
    url = url.rstrip("/")
    if url.endswith(("FeatureServer", "MapServer")):
        url += "/0"
    _plow_service[item_id] = (time.time(), url)
    return url


def _pick(p: dict, *needles: str):
    for k, v in p.items():
        kl = k.lower()
        if any(n in kl for n in needles) and v not in (None, ""):
            return v
    return None


def parse_plows(fc: dict, state: str, agency: str, now: float) -> list[list]:
    """GeoJSON to rows [id, lon, lat, heading, speed_mps, ts, label, agency, state]."""
    out = []
    for f in (fc or {}).get("features") or []:
        g = (f or {}).get("geometry") or {}
        p = (f or {}).get("properties") or {}
        c = g.get("coordinates") or []
        if g.get("type") != "Point" or len(c) < 2:
            continue
        label = _pick(p, "label", "truck", "vehicle", "name", "unit") or p.get("OBJECTID")
        hdg = _pick(p, "heading", "bearing", "direction")
        spd = _pick(p, "speed")
        ts = _pick(p, "time", "date", "logdt")
        try:
            hdg = float(hdg) if hdg is not None else None
        except (TypeError, ValueError):
            hdg = None
        try:
            spd = round(float(spd) * 0.44704, 2) if spd is not None else None   # mph
        except (TypeError, ValueError):
            spd = None
        if isinstance(ts, (int, float)):
            ts = int(ts / 1000) if ts > 1e11 else int(ts)
        else:
            ts = int(now)
        out.append([f"{state}-{label}", round(float(c[0]), 6), round(float(c[1]), 6), hdg, spd, ts,
                    str(label), agency, state])
    return out


@router.get("/plows")
def plows() -> dict:
    global _plows
    now = time.time()
    if _plows and now - _plows[0] < 60:
        return _plows[1]
    rows: list[list] = []
    status: dict[str, str] = {}
    for st, (agency, item) in PLOW_ITEMS.items():
        try:
            fc = json.loads(_get(_service_url(item) + "/query?" + urllib.parse.urlencode(
                {"where": "1=1", "outFields": "*", "outSR": "4326", "f": "geojson"})))
            got = parse_plows(fc, st, agency, now)
            rows.extend(got)
            status[st] = f"{len(got)} out"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            status[st] = f"did not answer ({type(exc).__name__})"
    doc = {"cols": ["id", "lon", "lat", "heading", "speed_mps", "ts", "label", "agency", "state"],
           "rows": rows, "n": len(rows), "status": status, "time": int(now),
           "evidence": "cooperative: positions the state reports for its own fleet"}
    _plows = (now, doc)
    return doc
