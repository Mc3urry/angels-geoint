"""Traffic cameras across the United States, for the 3D view: parsers only.

WHERE THEY COME FROM. Every source is a public, keyless, real-time feed, read
as its publisher serves it:

    MD   Maryland CHART            the G3 list (land.cameras), CHART's own player
    CA   Caltrans CWWP2            one JSON per district, 12 districts, still frames
    WA   WSDOT Travel Center       one JSON, still frames on images.wsdot.wa.gov
    TX   TxDOT ITS                 one JSON per district, 25 districts, frames via our API
    TX   City of Austin            Socrata open data, frames on cctv.austinmobility.io
    DE   DelDOT                    one JSON, live HLS video on video.deldot.gov
    WA   King County Road Services ArcGIS layer, frames on info.kingcounty.gov
    NY GA LA AZ WI ID UT NV CT NC  the state's 511 developer API, when a free key is
                                   in .env (CAM_KEY_<state>); stills and live HLS
    OH   OHGO                      Ohio DOT's public API, with OHGO_API_KEY
    *    LiveTrafficCam            every other state and DC: an aggregator of
                                   official DOT and 511 feeds that checks each
                                   camera's frame is current; at most 200 per
                                   state, live ones only

The state-DOT endpoints and their parsing follow God's Eye View
(server/providers/cctv, MIT), which exercises them; LiveTrafficCam's API is
the one its own MCP server documents (github.com/bzsasson/livetrafficcam-mcp,
MIT). Florida's FL511 and New York City's TMC are left out: their terms
need a written agreement.

WHAT IS KEPT. A location, a name, who publishes it, and where its live
picture is. No frame is stored, by this file or anything that calls it:
the picture is played from the publisher, in the browser, while the card
is open (README, "Viewer-only exception").

Media URLs are pinned to each publisher's own host. A record whose picture
lives anywhere else is dropped, not followed.
"""

from __future__ import annotations

import base64
import math
import re
from typing import Any
from urllib.parse import urlencode, urlsplit

COLS = ["id", "lon", "lat", "name", "src", "kind", "media", "page", "state", "note", "still"]
# still: a still frame from the same camera, for a video row whose stream
# will not play here (2026-11-02). Null when there is none.

# kind: how the browser shows the picture
IMAGE, HLS, PLAYER = "image", "hls", "player"

CALTRANS_URL = "https://cwwp2.dot.ca.gov/data/d{d}/cctv/cctvStatusD{d:02d}.json"
CALTRANS_DISTRICTS = tuple(range(1, 13))
WSDOT_URL = "https://data.wsdot.wa.gov/travelcenter/Cameras.json"
TXDOT_ORIGIN = "https://its.txdot.gov"
TXDOT_URL = TXDOT_ORIGIN + "/its/DistrictIts/GetCctvStatusListByDistrict?districtCode={d}"
TXDOT_FRAME_URL = TXDOT_ORIGIN + "/its/DistrictIts/GetCctvSnapshotByIcdId"
TXDOT_DISTRICTS = ("ABL", "AMA", "ATL", "AUS", "BMT", "BWD", "BRY", "CHS", "CRP",
                   "DAL", "ELP", "FTW", "HOU", "LRD", "LBB", "LFK", "ODA", "PAR",
                   "PHR", "SJT", "SAT", "TYL", "WAC", "WFS", "YKM")
AUSTIN_URL = "https://data.austintexas.gov/resource/b4k4-adkb.json?$limit=5000"
DELDOT_URL = "https://tmc.deldot.gov/json/videocamera.json"
KING_URL = ("https://services.arcgis.com/Ej0PsM5Aw677QF1W/arcgis/rest/services/"
            "TRAFFICCAMERA_POINT_2029/FeatureServer/0/query?where=1%3D1&outFields="
            "AssetID,Location,ImageURL,Live,CurrentStatus,Owner&f=geojson")
LTC_SITE = "https://livetrafficcam.com"
LTC_URL = LTC_SITE + "/api/cams.json?state={s}"

STATES = ("AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
          "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
          "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
          "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
          "WV", "WI", "WY")
# Read from their own DOT, so the aggregator is not asked for them.
DIRECT = {"CA", "WA", "TX", "DE", "MD"}
LTC_STATES = tuple(s for s in STATES if s not in DIRECT)

# THE 511 PLATFORM WITH A DEVELOPER KEY. These states run the same 511
# software, with the same documented endpoint (`/api/v2/get/cameras?key=
# &format=json`, each site's /help/endpoint/cameras page) and a free key
# from each site's developer page. A state's cameras come from here when
# its key is in .env, and from LiveTrafficCam (200 at most) when it is not.
# Throttled at ten calls a minute per key; this asks once per refresh.
ITERIS = {
    "NY": ("511NY", "https://511ny.org"),
    "GA": ("511GA", "https://511ga.org"),
    "LA": ("511LA", "https://511la.org"),
    "AZ": ("AZ511", "https://www.az511.com"),
    "WI": ("511WI", "https://511wi.gov"),
    "ID": ("Idaho 511", "https://511.idaho.gov"),
    "UT": ("UDOT Traffic", "https://www.udottraffic.utah.gov"),
    "NV": ("NVroads", "https://prod-nv.ibi511.com"),
    "CT": ("CTroads", "https://ctroads.org"),
    "NC": ("DriveNC", "https://www.drivenc.gov"),
}
ITERIS_PATH = "/api/v2/get/cameras"

# 511 sites whose VideoUrl is not public. DriveNC's points at NCDOT's own
# video servers (cfase*.services.ncdot.gov), which ask the browser for a
# username and password (seen 2026-11-02). A sign-in box from a state server
# inside this page is not something to put in front of anyone, so these
# states are shown by their still frames.
NO_PUBLIC_VIDEO = {"NC"}


def iteris_env(state: str) -> str:
    return f"CAM_KEY_{state}"


OHGO_URL = "https://publicapi.ohgo.com/api/v1/cameras?page-all=true"
OHGO_ENV = "OHGO_API_KEY"


def _num(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return math.nan
    return f


def _ok_point(lat: float, lon: float) -> bool:
    # The fifty states and DC, loosely: a zero or a swapped pair is refused.
    return (math.isfinite(lat) and math.isfinite(lon)
            and 17.5 <= lat <= 72.0 and -180.0 <= lon <= -64.0)


def _pinned(url: Any, origin: str) -> str | None:
    """`url` if it is https (or http) on exactly `origin`'s host, else None."""
    if not isinstance(url, str) or not url.strip():
        return None
    u = urlsplit(url.strip())
    o = urlsplit(origin)
    if u.scheme not in ("https", "http") or u.hostname != o.hostname or u.username or u.password:
        return None
    return url.strip()


def _row(id_, lon, lat, name, src, kind, media, page=None, state="", note="", still=None) -> list:
    return [str(id_), round(lon, 6), round(lat, 6), str(name or "")[:160], src, kind,
            media, page, state, str(note or "")[:120], still]


# -- Caltrans ------------------------------------------------------------------

def parse_caltrans(doc: dict, district: int) -> list[list]:
    out = []
    for row in (doc or {}).get("data") or []:
        c = (row or {}).get("cctv") or {}
        if str(c.get("inService", "")).lower() != "true":
            continue
        loc = c.get("location") or {}
        lat, lon = _num(loc.get("latitude")), _num(loc.get("longitude"))
        if not _ok_point(lat, lon):
            continue
        img = _pinned(((c.get("imageData") or {}).get("static") or {}).get("currentImageURL"),
                      "https://cwwp2.dot.ca.gov/")
        if not img:
            continue
        name = str(loc.get("locationName") or "").strip()
        m = re.match(r"^([A-Za-z0-9_-]+)\s*--\s*", name)
        code = (m.group(1) if m else str(len(out))).lower()
        label = name[m.end():] if m else name
        if loc.get("nearbyPlace"):
            label = f"{label} ({loc['nearbyPlace']})"
        out.append(_row(f"ca-d{district}-{code}", lon, lat, label or f"Caltrans D{district} {code}",
                        "Caltrans", IMAGE, img, None, "CA", f"District {district}"))
    return out


# -- WSDOT ---------------------------------------------------------------------

def _from_mercator(x: float, y: float) -> tuple[float, float]:
    lon = x / 6378137.0 * 180.0 / math.pi
    lat = math.degrees(2 * math.atan(math.exp(y / 6378137.0)) - math.pi / 2)
    return lat, lon


def parse_wsdot(doc: dict) -> list[list]:
    out = []
    for row in (doc or {}).get("features") or []:
        a = (row or {}).get("attributes") or {}
        g = (row or {}).get("geometry") or {}
        cid = a.get("CameraID")
        img = _pinned(a.get("ImageURL"), "https://images.wsdot.wa.gov/")
        x, y = _num(g.get("x")), _num(g.get("y"))
        if not isinstance(cid, int) or not img or not (math.isfinite(x) and math.isfinite(y)):
            continue
        lat, lon = _from_mercator(x, y)
        if not _ok_point(lat, lon):
            continue
        out.append(_row(f"wsdot-{cid}", lon, lat, str(a.get("CameraTitle") or f"WSDOT {cid}").strip(),
                        "WSDOT", IMAGE, img, None, "WA"))
    return out


# -- TxDOT ---------------------------------------------------------------------

# TxDOT camera ids are free text ("IH35 @ 6th"); they only ever travel URL-
# encoded, so anything printable up to 80 characters is accepted.
ICD_RE = r"[^\x00-\x1f\x7f]{1,80}"


def txdot_frame_path(icd: str, district: str) -> str:
    """Our API's address for one TxDOT frame. TxDOT answers a frame request
    with JSON carrying a base64 JPEG, which an <img> cannot show."""
    return "/cameras/txdot-frame?" + urlencode({"district": district, "icd": icd})


def parse_txdot(doc: dict, district: str) -> list[list]:
    out, seen = [], set()
    groups = (doc or {}).get("roadwayCctvStatuses") or {}
    if not isinstance(groups, dict):
        return out
    for rows in groups.values():
        for r in rows if isinstance(rows, list) else []:
            if str((r or {}).get("statusDescription", "")) != "Device Online" or r.get("hasSnapshot") is False:
                continue
            icd = str(r.get("icd_Id") or "").strip()
            lat, lon = _num(r.get("latitude")), _num(r.get("longitude"))
            if not icd or icd in seen or not _ok_point(lat, lon) or not re.fullmatch(ICD_RE, icd):
                continue
            seen.add(icd)
            road = ((r.get("equipLoc") or {}).get("roadway")) or ""
            out.append(_row(f"txdot-{district.lower()}-{icd}", lon, lat, str(r.get("name") or icd).strip(),
                            "TxDOT", IMAGE, txdot_frame_path(icd, district), None, "TX",
                            f"{district} district{f', {road}' if road else ''}"))
    return out


def txdot_frame_url(icd: str, district: str) -> str:
    if district not in TXDOT_DISTRICTS or not re.fullmatch(ICD_RE, icd or ""):
        raise ValueError("not a TxDOT camera")
    return TXDOT_FRAME_URL + "?" + urlencode({"icdId": icd, "districtCode": district})


def txdot_frame_jpeg(doc: dict) -> bytes:
    snip = (doc or {}).get("snippet")
    if not isinstance(snip, str) or not snip:
        raise ValueError("TxDOT sent no frame")
    if "," in snip[:64] and snip.startswith("data:"):
        snip = snip.split(",", 1)[1]
    jpeg = base64.b64decode(snip, validate=False)
    if not jpeg.startswith(b"\xff\xd8"):
        raise ValueError("TxDOT's frame is not a JPEG")
    return jpeg


# -- City of Austin --------------------------------------------------------------

def parse_austin(rows: list) -> list[list]:
    out, seen = [], set()
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        cid = str(r.get("camera_id") or r.get("cameraid") or "").strip()
        if not cid.isdigit() or cid in seen:
            continue
        status = str(r.get("camera_status") or "").strip().upper()
        if status and status != "TURNED_ON":
            continue
        loc = r.get("location") or {}
        coords = loc.get("coordinates") if isinstance(loc, dict) else None
        if coords and len(coords) >= 2:
            lon, lat = _num(coords[0]), _num(coords[1])
        else:
            lat = _num(loc.get("latitude") if isinstance(loc, dict) else None)
            lon = _num(loc.get("longitude") if isinstance(loc, dict) else None)
        if not _ok_point(lat, lon):
            continue
        seen.add(cid)
        name = (r.get("location_name") or r.get("camera_name") or f"Austin camera {cid}")
        out.append(_row(f"austin-{cid}", lon, lat, str(name).strip(), "City of Austin", IMAGE,
                        f"https://cctv.austinmobility.io/image/{cid}.jpg", None, "TX"))
    return out


# -- DelDOT ---------------------------------------------------------------------

def parse_deldot(doc: dict) -> list[list]:
    out = []
    for r in (doc or {}).get("videoCameras") or []:
        if str((r or {}).get("status", "")).lower() != "active":
            continue
        lat, lon = _num(r.get("lat")), _num(r.get("lon"))
        cid = str(r.get("id") or "").strip()
        hls = _pinned((r.get("urls") or {}).get("m3u8s"), "https://video.deldot.gov/")
        if not _ok_point(lat, lon) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", cid) or not hls:
            continue
        if not re.fullmatch(r"/live/[A-Za-z0-9_.-]+/playlist\.m3u8", urlsplit(hls).path):
            continue
        out.append(_row(f"deldot-{cid.lower()}", lon, lat, str(r.get("title") or cid).strip(),
                        "DelDOT", HLS, hls, None, "DE",
                        f"{r['county']} County" if r.get("county") else ""))
    return out


# -- King County ------------------------------------------------------------------

def parse_king(doc: dict) -> list[list]:
    out = []
    for f in (doc or {}).get("features") or []:
        p = (f or {}).get("properties") or {}
        if p.get("Live") != 1 or str(p.get("CurrentStatus") or "").strip() != "Active":
            continue
        img = _pinned(p.get("ImageURL"), "https://info.kingcounty.gov/")
        c = ((f.get("geometry") or {}).get("coordinates")) or []
        if not img or len(c) < 2:
            continue
        lon, lat = _num(c[0]), _num(c[1])
        if not _ok_point(lat, lon):
            continue
        out.append(_row(f"king-{p.get('AssetID')}", lon, lat, str(p.get("Location") or "King County camera"),
                        "King County Road Services", IMAGE, img, None, "WA", p.get("Owner") or ""))
    return out


# -- Maryland CHART (from G3, through land.cameras) ---------------------------------

def from_chart(fc: dict) -> list[list]:
    out = []
    for f in (fc or {}).get("features") or []:
        p = f.get("properties") or {}
        lon, lat = (f.get("geometry") or {}).get("coordinates") or (None, None)
        lon, lat = _num(lon), _num(lat)
        if not _ok_point(lat, lon):
            continue
        out.append(_row(f"chart-{p.get('id')}", lon, lat, p.get("location") or "CHART camera",
                        "Maryland CHART", PLAYER if p.get("has_stream") else IMAGE,
                        f"/land/cameras/{p.get('id')}/stream" if p.get("has_stream") else None,
                        None, "MD", p.get("county") or ""))
    return out


# -- the 511 platform (keyed) -------------------------------------------------------

def _https(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    u = urlsplit(url.strip())
    return url.strip() if u.scheme == "https" and u.hostname and not u.username and not u.password else None


def parse_iteris(doc: Any, state: str, name: str) -> list[list]:
    """A 511 v2 camera list. One row per camera: its live stream when it
    has one, its still view otherwise. Views that are not Enabled are
    skipped; a camera with none is dropped."""
    out = []
    for c in doc if isinstance(doc, list) else []:
        if not isinstance(c, dict):
            continue
        lat, lon = _num(c.get("Latitude")), _num(c.get("Longitude"))
        if not _ok_point(lat, lon) or c.get("Id") is None:
            continue
        views = [v for v in (c.get("Views") or []) if isinstance(v, dict)
                 and str(v.get("Status") or "Enabled").lower() == "enabled"]
        video = None if state in NO_PUBLIC_VIDEO else next(
            (_https(v.get("VideoUrl")) for v in views if _https(v.get("VideoUrl"))), None)
        still = next((_https(v.get("Url")) for v in views if _https(v.get("Url"))), None)
        if not (video or still):
            continue
        label = c.get("Name") or c.get("Location") or f"{name} camera {c['Id']}"
        note = " · ".join(str(x) for x in (c.get("Roadway"), c.get("Direction")) if x and x not in ("None", "Unknown"))
        out.append(_row(f"{state.lower()}511-{c['Id']}", lon, lat, str(label).strip(), name,
                        HLS if video else IMAGE, video or still, None, state, note,
                        still if video else None))
    return out


def playlist_ok(status: int, body: bytes) -> bool:
    """An HLS playlist anyone can play: 200 and a playlist, not a login page."""
    return status == 200 and body.lstrip()[:7] == b"#EXTM3U"


def parse_ohgo(doc: Any) -> list[list]:
    out = []
    results = (doc or {}).get("Results") if isinstance(doc, dict) else None
    if results is None and isinstance(doc, dict):
        results = doc.get("results")
    for c in results or []:
        if not isinstance(c, dict):
            continue
        g = lambda k: c.get(k) if k in c else c.get(k[0].lower() + k[1:])  # noqa: E731
        lat, lon = _num(g("Latitude")), _num(g("Longitude"))
        views = g("CameraViews") or []
        img = None
        for v in views if isinstance(views, list) else []:
            if isinstance(v, dict):
                img = _https(v.get("LargeUrl") or v.get("largeUrl") or v.get("SmallUrl") or v.get("smallUrl"))
                if img:
                    break
        if not img or not _ok_point(lat, lon) or g("Id") is None:
            continue
        out.append(_row(f"ohgo-{g('Id')}", lon, lat, str(g("Location") or g("Description") or "OHGO camera"),
                        "OHGO (Ohio DOT)", IMAGE, img, None, "OH", str(g("Description") or "")))
    return out


# -- LiveTrafficCam ----------------------------------------------------------------

def parse_ltc(doc: dict, state: str) -> list[list]:
    out = []
    for c in (doc or {}).get("cams") or []:
        if not isinstance(c, dict) or c.get("live_status") != "live":
            continue
        lat, lon = _num(c.get("lat")), _num(c.get("lng"))
        img = c.get("image")
        if isinstance(img, str) and img.startswith("/img/"):
            img = LTC_SITE + img
        img = _pinned(img, LTC_SITE + "/")
        if not img or not _ok_point(lat, lon) or c.get("id") is None:
            continue
        page = c.get("official_url") if isinstance(c.get("official_url"), str) and \
            c["official_url"].startswith("https://") else None
        out.append(_row(f"ltc-{c['id']}", lon, lat, str(c.get("name") or "").strip(),
                        str(c.get("attribution") or "LiveTrafficCam").replace("Camera: ", ""),
                        IMAGE, img, page, str(c.get("state") or state).upper(),
                        f"{c.get('route') or ''} · via LiveTrafficCam".strip(" ·")))
    return out
