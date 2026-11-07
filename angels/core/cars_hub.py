"""Road events from the CARS program's North American Hub, for the viewers.

ACCESS (2026-11-06). Castle Rock Associates, which runs the CARS 511
systems, gave Joshua one login for the hubs of Iowa, Indiana, Kansas,
Minnesota and Nebraska (and SANDAG, whose feeds are empty "until a new data
source is available"). The login is in .env as CARS_HUB_USER and
CARS_HUB_PASSWORD. It is applied here, on the server, as HTTP Basic
(the Hub ICD, section 2.3), and only ever to the hub host it belongs to.
It never reaches the browser, a response, a log line or an error message.

FORMAT. Each hub's index page lists its XML feeds. The one used is FEU-g,
the "full event update" message of the Hub ICD (FEU 2.2): one
<full-event-update> per event, with a headline, element details,
locations in micro-degrees and local times with their UTC offsets. The
hub rewrites the file every 30 to 60 seconds; the API asks every five
minutes.

WHAT IS KEPT. Each event's public description, where it is and when it
applies. `operator-comments` are "free text remarks, not for dissemination
to the public" (ICD 1) and are never read. An event whose status is
`ended`, or whose valid period is over, is dropped (ICD 1.3, 1.5.8.1).

VIEWER ONLY. These are agency declarations, like the WZDx work zones, but
they are not part of Phase G. The registered test reads each state's regime
from the WZDx registry as committed at registration, and its declared set
from the WZDx feeds the collector archives; nothing here is archived and
nothing here enters either (DECISIONS, 2026-11-07).
"""

from __future__ import annotations

import gzip
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

USER_ENV = "CARS_HUB_USER"
PASSWORD_ENV = "CARS_HUB_PASSWORD"

# state -> (publisher, the hub's index page). HTTPS only: the login is a
# password, and it is not sent over plain HTTP, whatever scheme the email
# gave (it gave http for four of them).
HUBS = {
    "IN": ("INDOT (CARS hub)", "https://inhub.carsprogram.org/index.jsf"),
    "KS": ("KDOT (CARS hub)", "https://ks.carsprogram.org/hub/index.jsf"),
    "MN": ("MnDOT (CARS hub)", "https://mn.carsprogram.org/hub/index.jsf"),
    "NE": ("NDOT (CARS hub)", "https://ne.carsprogram.org/hub/index.jsf"),
}
# HUBS NOT ASKED, said on the map rather than left blank.
GAPS = {
    "IA": ("ia.carsprogram.org redirects to iowadot.gov, the DOT's home page and not "
           "a feed host, so the login is not sent there (2026-11-07). Ask Castle Rock "
           "where Iowa's hub went"),
    "SANDAG": "Castle Rock: feeds unavailable until a new data source is found (2026-11-06)",
}

# HOSTS A HUB REDIRECTS TO THAT MAY RECEIVE THE LOGIN, per state. Anything
# else a redirect points at is refused. Kansas: ks.carsprogram.org redirects
# to kscars.kandrive.gov, on KanDrive, the Kansas 511 domain Castle Rock's
# email lists; Joshua allowed it, for Kansas only (2026-11-07).
TRUSTED_REDIRECTS = {"KS": ("kscars.kandrive.gov",)}

# The index page's link to the FEU-g feed, and to camera feeds (listed in
# /roads/events' status, not used yet: CCTV is offered for IA and IN only).
FEU_G_LINK = re.compile(r"feu[-_ ]?g", re.I)
CCTV_LINK = re.compile(r"cctv|camera", re.I)

COLS = ["id", "state", "kind", "type", "headline", "road", "direction", "text",
        "start", "end", "updated", "planned"]

# The headline's element names (ICD 1.4.1), grouped for one colour each.
KIND_OF_TYPE = {
    "incident": "incident", "obstruction": "incident", "unusual-driving": "incident",
    "mobile-situation": "incident", "disaster": "incident", "disturbance": "incident",
    "closure": "closure",
    "roadwork": "roadwork",
    "restriction": "restriction", "winter-driving-restriction": "restriction",
    "cdot-osow-restrictions": "restriction",
    "weather-condition": "weather", "precipitation": "weather", "wind": "weather",
    "visibility-air-quality": "weather", "temperature": "weather",
    "pavement-condition": "weather", "winter-driving-index": "weather",
    "mdss-conditions": "weather", "mdss-weather-conditions": "weather",
    "nws-warning": "weather", "cdot-road-conditions": "weather", "ma-road-conditions": "weather",
    "traffic-condition": "traffic", "delay": "traffic",
    "special-event": "event", "sporting-event": "event", "activity": "event",
    "parking-information": "event",
}
KINDS = ("incident", "closure", "roadwork", "restriction", "weather", "traffic", "event", "other")
TEXT_MAX = 400


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def kids(el: ET.Element | None, name: str) -> list[ET.Element]:
    return [] if el is None else [c for c in el if local(c.tag) == name]


def kid(el: ET.Element | None, *path: str) -> ET.Element | None:
    for name in path:
        if el is None:
            return None
        el = next((c for c in el if local(c.tag) == name), None)
    return el


def text(el: ET.Element | None, *path: str) -> str:
    e = kid(el, *path)
    return " ".join((e.text or "").split()) if e is not None else ""


# -- the index page ---------------------------------------------------------------

class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def feed_links(html: str, index_url: str, hosts: Iterable[str] | None = None) -> dict[str, list[str]]:
    """The FEU-g and camera links on a hub's index page, as absolute URLs on
    the hub's own hosts (`hosts`; by default the page's own). A link anywhere
    else is dropped, not followed: the login must not travel with it."""
    p = _Links()
    p.feed(html)
    allowed = set(hosts) if hosts else {urlsplit(index_url).hostname}
    out: dict[str, list[str]] = {"feu_g": [], "cctv": []}
    for href, label in p.links:
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        url = urljoin(index_url, href)
        u = urlsplit(url)
        if u.hostname not in allowed:
            continue
        url = u._replace(scheme="https").geturl()
        both = f"{href} {label}"
        if FEU_G_LINK.search(both) and url not in out["feu_g"]:
            out["feu_g"].append(url)
        elif CCTV_LINK.search(both) and url not in out["cctv"]:
            out["cctv"].append(url)
    return out


def looks_like_sign_in(html: str) -> bool:
    low = html.lower()
    return "type=\"password\"" in low or "type='password'" in low or "j_password" in low


# -- times ------------------------------------------------------------------------

def when(el: ET.Element | None) -> datetime | None:
    """A DateTimeZone (ICD 1.1.2): local date YYYYMMDD, time HHMMSS, offset
    +-HHMM. Returned in UTC; None when any part is missing or malformed."""
    if el is None:
        return None
    d, t, off = text(el, "date"), text(el, "time"), text(el, "utc-offset")
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", off or "")
    if not (re.fullmatch(r"\d{8}", d) and re.fullmatch(r"\d{4}(\d{2})?", t) and m):
        return None
    try:
        lt = datetime.strptime(d + t.ljust(6, "0"), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    sign = 1 if m.group(1) == "+" else -1
    tz = timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))
    return lt.replace(tzinfo=tz).astimezone(timezone.utc)


def iso(t: datetime | None) -> str | None:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


# -- locations --------------------------------------------------------------------

def micro(el: ET.Element | None) -> tuple[float, float] | None:
    """A geo-location in micro-degrees (ICD 1.5.7.2.1.1), as (lon, lat)."""
    try:
        lat = int(text(el, "latitude")) / 1e6
        lon = int(text(el, "longitude")) / 1e6
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return lon, lat


def link_geometry(link: ET.Element) -> dict | None:
    a = micro(kid(link, "primary-location", "geo-location"))
    b = micro(kid(link, "secondary-location", "geo-location"))
    if a and b and a != b:
        return {"type": "LineString", "coordinates": [list(a), list(b)]}
    p = a or b
    return {"type": "Point", "coordinates": list(p)} if p else None


# -- one event --------------------------------------------------------------------

def _phrase(desc: ET.Element) -> tuple[str, str, str] | None:
    """(role, type, words) for one element description (ICD 1.5.1)."""
    for c in desc:
        role = local(c.tag)
        if role in ("phrase", "cause", "advice", "qualifier"):
            inner = next(iter(c), None)
            if inner is not None and (inner.text or "").strip():
                return role, local(inner.tag), " ".join(inner.text.split())
            if (c.text or "").strip():
                return role, "", " ".join(c.text.split())
        elif role == "additional-text":
            words = text(c, "description")
            if words:
                return role, "", words
    return None


def parse_event(feu: ET.Element, state: str, now: datetime) -> dict | None:
    """One <full-event-update> as a GeoJSON feature, or None when it has
    ended, has expired, or says nowhere on a road."""
    statuses = [text(ind, "status").lower() for ind in kids(kid(feu, "event-indicators"), "event-indicator")]
    if "ended" in statuses:
        return None
    eid = text(feu, "event-reference", "event-id")
    hl = kid(feu, "headline", "headline")
    h_el = next(iter(hl), None) if hl is not None else None
    htype = local(h_el.tag) if h_el is not None else ""
    hwords = " ".join((h_el.text or "").split()) if h_el is not None else ""

    geom = road = direction = None
    words: list[str] = []
    starts: list[datetime] = []
    ends: list[datetime] = []
    updated: list[datetime] = []
    for det in kids(kid(feu, "details"), "detail"):
        for desc in kids(kid(det, "descriptions"), "description"):
            ph = _phrase(desc)
            if ph and ph[2] not in words:
                words.append(ph[2])
        for loc in kids(kid(det, "locations"), "location"):
            link = kid(loc, "location-on-link")
            if link is not None and geom is None:
                geom = link_geometry(link)
                if geom is not None:
                    road = text(link, "route-designator") or None
                    direction = text(link, "link-direction") or None
        times = kid(det, "times")
        up = when(kid(times, "update-time"))
        st = when(kid(times, "start-time"))
        vp = kid(times, "valid-period")
        end = when(kid(vp, "end-time"))
        if end is None and text(vp, "duration").isdigit():
            base = st if st and (not up or st > up) else up
            if base:
                end = base + timedelta(minutes=int(text(vp, "duration")))
        for t, into in ((up, updated), (st, starts), (end, ends)):
            if t:
                into.append(t)
    if geom is None:
        return None
    end = max(ends) if ends else None
    if end is not None and end < now:
        return None
    start = min(starts) if starts else None
    head = hwords or (words[0] if words else htype.replace("-", " "))
    rest = [w for w in words if w != head]
    body = "; ".join(rest)
    if len(body) > TEXT_MAX:
        body = body[: TEXT_MAX - 1].rstrip() + "…"
    return {
        "type": "Feature",
        "geometry": geom,
        "properties": {
            "id": eid or None, "state": state,
            "kind": KIND_OF_TYPE.get(htype, "other"), "type": htype or None,
            "headline": head or None, "road": road, "direction": direction,
            "text": body or None, "start": iso(start), "end": iso(end),
            "updated": iso(max(updated) if updated else None),
            "planned": bool(start and start > now),
        },
    }


def unpack(raw: bytes) -> bytes:
    """The feed as served: plain XML, gzip, or a zip of one XML file."""
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            return z.read(names[0]) if names else b""
    return raw


def parse_feu(raw: bytes, state: str, now: datetime | None = None) -> tuple[list[dict], dict]:
    """Every current event in one FEU-g document, and counts of what was left
    out and why (so an empty map can be told from an empty feed)."""
    now = now or datetime.now(timezone.utc)
    feats: list[dict] = []
    counts = {"messages": 0, "ended_or_expired_or_no_road": 0}
    seen: set[str] = set()
    for _, el in ET.iterparse(io.BytesIO(unpack(raw)), events=("end",)):
        if local(el.tag) != "full-event-update":
            continue
        counts["messages"] += 1
        f = parse_event(el, state, now)
        el.clear()
        if f is None:
            counts["ended_or_expired_or_no_road"] += 1
            continue
        key = f["properties"]["id"] or f"{state}-{counts['messages']}"
        if key in seen:
            continue
        seen.add(key)
        f["properties"]["id"] = key
        feats.append(f)
    return feats, counts


def in_bbox(f: dict, box: tuple[float, float, float, float] | None) -> bool:
    if box is None:
        return True
    w, s, e, n = box
    g = f["geometry"]
    pts: Iterable = [g["coordinates"]] if g["type"] == "Point" else g["coordinates"]
    return any(w <= x <= e and s <= y <= n for x, y in pts)
