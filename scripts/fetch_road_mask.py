"""G3: fetch the two Maryland layers the land mask is built from.

    python scripts/fetch_road_mask.py                  # both layers
    python scripts/fetch_road_mask.py --layer cameras  # one

Writes data/reference/roads/g3/<layer>.geojson and manifest.json.

WHAT THE MASK IS FOR

G6 compares disruptions the vehicles observe with disruptions agencies
declare. A road nobody independent can see is not a road where nothing
happened, which is mechanism J ("missing" meaning "missing where I looked").
So before any land statistic, the project needs to know where independent
observation exists at all, and how much traffic each place carries:

    cameras   CHART traffic cameras: where the state itself can see the road
    aadt      annual average daily traffic stations: exposure, every vehicle
              counted including private cars, as numbers and never as identity

Both are ArcGIS FeatureServers on Maryland's iMAP. Both are point layers. The
AADT layer is STATIONS, not segments, which matters for G4 and is recorded in
the manifest rather than discovered later.

THE THREE THINGS THIS FILE REFUSES

**A page taken for the layer.** Both services cap a response (the camera
layer at 550) and say so with `exceededTransferLimit`. The probe on
2026-10-02 saw exactly that. So the total is asked for first with
`returnCountOnly`, the layer is paged in OBJECTID order, and the write is
refused unless the features fetched equal that count with no OBJECTID twice.
A short layer written as complete would put a hole in the mask that reads as
quiet road.

**Somebody else's links.** The camera layer carries a stream `url` and an
`iframe` per camera. The mask needs where a camera is, not how to watch it,
and a third party's stream links are not this project's to republish
(mechanism X: the payload is the surface). They are dropped by name from the
TRACKED file, and every byte written is scanned with the project's
body-credential patterns first. A match refuses the write and reports the
pattern name, never the match.

**The layer's links are dead (2026-10-28).** All 451 point at
`chart.maryland.gov/video/video.php?feed=...`, which answers 404. CHART moved
its player and the state's GIS layer did not follow. Of the layer's 451
cameras, 358 are in CHART's own live list and 93 are not, and CHART lists 195
the layer does not. So the viewer's links now come from CHART's own feed,
`DataFeeds/GetCamerasJson` (553 cameras, each with a `publicVideoURL`). The
layer stays the mask's record, and every run probes one of the layer's old
links and records the answer. A self-published record that nobody checks is
this project's subject; here it is the state's own map of its own cameras.

**But the viewer can watch them, from CHART (2026-10-27, Joshua).** The Land
tab has a Cameras sub-tab that plays a camera in CHART's own player, from
CHART's servers. So the links are also written to `camera-streams.json`,
which `.gitignore` leaves untracked. It stays on this machine and is never
published. A link is kept only if it points at a Maryland government host
(ALLOWED_STREAM_HOSTS); any other host is counted by name and dropped. The
layer ships an `iframe` field, embed markup made by the publisher, so
embedding is what it offers. Only the `src` is taken from that markup. The
markup itself is never placed in a page.

**A year nobody stated.** The AADT layer has `AADT_2010` to `AADT_2018` and
a bare `AADT`. Which year the bare column is, the layer does not say
(mechanism P: a measurement without the input that defines it). The manifest
lists the year columns present and records the bare one as NOT INFERRED.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

try:
    import _bootstrap  # noqa: F401  (must precede the angels imports)
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import REFERENCE
from scripts.probe_road_feeds import body_credential

OUT_DIR = REFERENCE / "roads" / "g3"
USER_AGENT = ("angels-geoint/0.1 (senior capstone, GIS; contact via "
              "github.com/Mc3urry/angels-geoint)")
TIMEOUT_S = 60.0
CAP_BYTES = 64 * 1024 * 1024
PAGE_SIZE = 500          # under the camera layer's 550; the same for both
PAUSE_S = 0.5            # a public service; one request at a time
IMAP = "https://mdgeodata.md.gov/imap/rest/services/Transportation/"

LAYERS: dict[str, dict[str, Any]] = {
    "cameras": {
        "service": IMAP + "MD_TrafficCameras/FeatureServer/0",
        "drop": {"url": "a third party's stream link; not needed for the mask",
                 "iframe": "a third party's embed markup; not needed"},
        "what": "CHART traffic cameras: where the state can see the road",
    },
    "aadt": {
        "service": IMAP + "MD_AnnualAverageDailyTraffic/FeatureServer/0",
        "drop": {"LINK": "a link out of the layer; not needed for the mask"},
        "what": "AADT count stations: exposure, every vehicle, as numbers",
    },
}


# A stream link is kept only if its host ends in one of these.
ALLOWED_STREAM_HOSTS = (".maryland.gov", ".md.gov")
_SRC = re.compile(r"""\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def stream_link(url: Any) -> tuple[str | None, str | None]:
    """(url, None) if it is an http(s) link to an allowed host, else
    (None, the host or reason it was refused)."""
    if not isinstance(url, str) or not url.strip():
        return None, "empty"
    p = urlparse(url.strip())
    if p.scheme not in ("http", "https"):
        return None, f"scheme:{p.scheme or 'none'}"
    host = (p.hostname or "").lower()
    if not any(host == h.lstrip(".") or host.endswith(h) for h in ALLOWED_STREAM_HOSTS):
        return None, host or "no host"
    return url.strip(), None


def iframe_src(markup: Any) -> str | None:
    """The `src` of the publisher's embed markup, and nothing else from it."""
    if not isinstance(markup, str):
        return None
    m = _SRC.search(markup)
    return m.group(1) if m else None


def camera_streams(features: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """({OBJECTID: {"embed": url|None, "page": url|None}}, a report)."""
    streams: dict[str, Any] = {}
    refused: dict[str, int] = {}
    n_embed = n_page = 0
    for f in features:
        props = f.get("properties") or {}
        oid = props.get("OBJECTID", f.get("id"))
        embed, why_e = stream_link(iframe_src(props.get("iframe")))
        page, why_p = stream_link(props.get("url"))
        for why in (why_e, why_p):
            if why and why != "empty":
                refused[why] = refused.get(why, 0) + 1
        if embed or page:
            streams[str(oid)] = {"embed": embed, "page": page}
            n_embed += bool(embed)
            n_page += bool(page)
    return streams, {"n_cameras": len(features), "n_with_stream": len(streams),
                     "n_embed": n_embed, "n_page": n_page,
                     "refused_by_host": dict(sorted(refused.items())),
                     "allowed_hosts": list(ALLOWED_STREAM_HOSTS)}


CHART_FEED = "https://chart.maryland.gov/DataFeeds/GetCamerasJson"


def parse_chart_feed(doc: Any) -> list[dict[str, Any]]:
    """CHART's camera list, refused unless every row has an id and a point."""
    if not isinstance(doc, list) or not doc:
        raise MaskRefused("the CHART camera feed is not a non-empty list")
    bad = [i for i, c in enumerate(doc) if not (
        isinstance(c, dict) and c.get("id")
        and isinstance(c.get("lat"), (int, float))
        and isinstance(c.get("lon"), (int, float)))]
    if bad:
        raise MaskRefused(f"{len(bad)} CHART camera row(s) lack an id or a point")
    return doc


def chart_streams(items: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """({CHART id: what the viewer needs}, a report). Links are kept only on
    an allowed host, exactly as for the layer."""
    streams: dict[str, Any] = {}
    refused: dict[str, int] = {}
    for c in items:
        url, why = stream_link(c.get("publicVideoURL"))
        if why and why != "empty":
            refused[why] = refused.get(why, 0) + 1
        streams[str(c["id"])] = {
            "embed": url, "page": url,
            "name": str(c.get("name") or c.get("description") or ""),
            "description": str(c.get("description") or ""),
            "category": ", ".join(c.get("cameraCategories") or []),
            "lat": c["lat"], "lon": c["lon"],
            "op_status": c.get("opStatus"), "comm_mode": c.get("commMode"),
        }
    ops: dict[str, int] = {}
    for s in streams.values():
        ops[str(s["op_status"])] = ops.get(str(s["op_status"]), 0) + 1
    return streams, {"n_cameras": len(items),
                     "n_with_link": sum(1 for s in streams.values() if s["embed"]),
                     "op_status": dict(sorted(ops.items())),
                     "refused_by_host": dict(sorted(refused.items()))}


def cross_check(layer_feats: list[dict[str, Any]], items: list[dict[str, Any]]) -> dict[str, int]:
    """The state's GIS layer against CHART's own list, matched on the feed id."""
    layer = {str((f.get("properties") or {}).get("feedID")) for f in layer_feats}
    chart = {str(c["id"]) for c in items}
    return {"layer": len(layer), "chart": len(chart), "both": len(layer & chart),
            "layer_only": len(layer - chart), "chart_only": len(chart - layer)}


def frame_policy(headers: Any) -> dict[str, Any]:
    """Whether CHART's player page may be shown inside another page."""
    get = (lambda k: headers.get(k)) if hasattr(headers, "get") else (lambda k: None)
    xfo = (get("X-Frame-Options") or "").strip()
    csp = get("Content-Security-Policy") or ""
    anc = next((d.strip() for d in csp.split(";")
                if d.strip().lower().startswith("frame-ancestors")), "")
    blocked = xfo.upper() in ("DENY", "SAMEORIGIN") or (
        anc and "*" not in anc and "localhost" not in anc)
    return {"x_frame_options": xfo or None, "frame_ancestors": anc or None,
            "embeddable": not blocked}


def head_status(url: str) -> tuple[int | None, Any]:
    """(status, headers) of one GET, body unread. Errors are a status too."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return r.status, r.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers
    except (urllib.error.URLError, TimeoutError):
        return None, None


class MaskRefused(RuntimeError):
    """The layer as fetched cannot be written as the layer."""


# --------------------------------------------------------------------------
# requests, pure
# --------------------------------------------------------------------------

def count_url(service: str) -> str:
    return service + "/query?" + urlencode(
        {"where": "1=1", "returnCountOnly": "true", "f": "json"})


def page_url(service: str, offset: int, size: int = PAGE_SIZE) -> str:
    """Ordered by OBJECTID, or paging is not stable between requests."""
    return service + "/query?" + urlencode({
        "where": "1=1", "outFields": "*", "outSR": "4326", "f": "geojson",
        "orderByFields": "OBJECTID ASC",
        "resultOffset": str(offset), "resultRecordCount": str(size)})


def parse_count(doc: Any) -> int:
    n = doc.get("count") if isinstance(doc, dict) else None
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise MaskRefused(f"the count request did not return a count: {doc!r:.120}")
    return n


# --------------------------------------------------------------------------
# what was fetched, pure
# --------------------------------------------------------------------------

def object_id(feature: dict[str, Any]) -> Any:
    props = feature.get("properties") or {}
    return props.get("OBJECTID", feature.get("id"))


def merge_pages(pages: list[dict[str, Any]], expected: int) -> list[dict[str, Any]]:
    """All features, refused unless they are exactly the layer."""
    feats: list[dict[str, Any]] = []
    for p in pages:
        got = p.get("features")
        if not isinstance(got, list):
            raise MaskRefused("a page had no feature list")
        feats.extend(got)
    ids = [object_id(f) for f in feats]
    if any(i is None for i in ids):
        raise MaskRefused("a feature has no OBJECTID, so completeness "
                          "cannot be checked")
    if len(set(ids)) != len(ids):
        raise MaskRefused(f"{len(ids) - len(set(ids))} OBJECTID(s) fetched "
                          f"twice: the paging was not stable")
    if len(feats) != expected:
        raise MaskRefused(f"fetched {len(feats)} features but the layer "
                          f"reports {expected}: a short layer would read as "
                          f"quiet road")
    return feats


def without(feature: dict[str, Any], drop: dict[str, str]) -> dict[str, Any]:
    props = {k: v for k, v in (feature.get("properties") or {}).items()
             if k not in drop}
    return {"type": "Feature", "geometry": feature.get("geometry"),
            "properties": props}


def describe(features: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts a reader needs before trusting the layer: geometry types,
    points without usable coordinates, the extent, and nulls per field."""
    types: dict[str, int] = {}
    bad = 0
    xs: list[float] = []
    ys: list[float] = []
    nulls: dict[str, int] = {}
    for f in features:
        g = f.get("geometry") or {}
        t = str(g.get("type"))
        types[t] = types.get(t, 0) + 1
        c = g.get("coordinates")
        if (t == "Point" and isinstance(c, list) and len(c) >= 2
                and all(isinstance(v, (int, float)) and math.isfinite(v)
                        for v in c[:2])):
            xs.append(float(c[0]))
            ys.append(float(c[1]))
        else:
            bad += 1
        for k, v in (f.get("properties") or {}).items():
            if v is None:
                nulls[k] = nulls.get(k, 0) + 1
    return {
        "n": len(features),
        "geometry_types": types,
        "without_usable_point": bad,
        "extent": [min(xs), min(ys), max(xs), max(ys)] if xs else None,
        "nulls_by_field": dict(sorted(nulls.items())),
    }


def year_columns(features: list[dict[str, Any]], prefix: str = "AADT_") -> dict[str, Any]:
    keys = set()
    for f in features[:50]:
        keys.update((f.get("properties") or {}).keys())
    years = sorted(k for k in keys if k.startswith(prefix) and k[len(prefix):].isdigit())
    return {"year_columns": years,
            "bare_column": "AADT" if "AADT" in keys else None,
            "bare_column_year": "NOT INFERRED: the layer does not state it"}


def serialise(features: list[dict[str, Any]]) -> bytes:
    doc = {"type": "FeatureCollection", "features": features}
    return json.dumps(doc, separators=(",", ":"), sort_keys=True).encode("utf-8")


def refuse_credentials(raw: bytes, layer: str) -> None:
    hit = body_credential(raw)
    if hit:
        raise MaskRefused(f"{layer}: the output carries something shaped like "
                          f"a {hit}; nothing written")


# --------------------------------------------------------------------------
# the network
# --------------------------------------------------------------------------

def merge_manifest(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """A one-layer fetch replaces THAT layer's entry and keeps the others.

    Found 2026-10-27, before it bit: `--layer cameras` wrote a manifest with
    only the cameras in it, so refreshing one layer would have erased the
    other's record, sha256 and all, from the tracked file. Each layer keeps
    its own `fetched_at_utc`, so a merged manifest says when each was taken."""
    layers = dict((old or {}).get("layers") or {})
    layers.update(new["layers"])
    out = {**new, "layers": layers}
    out["_partial"] = set(layers) != set(LAYERS)
    return out


def get_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        body = r.read(CAP_BYTES + 1)
    if len(body) > CAP_BYTES:
        raise MaskRefused("a page over the size cap; refusing a truncated page")
    doc = json.loads(body.decode("utf-8"))
    if isinstance(doc, dict) and "error" in doc:
        raise MaskRefused(f"the service answered with an error object: "
                          f"{str(doc['error'])[:160]}")
    return doc


def fetch_layer(name: str, spec: dict[str, Any], size: int) -> list[dict[str, Any]]:
    expected = parse_count(get_json(count_url(spec["service"])))
    print(f"  {name}: the layer reports {expected:,} features")
    pages, got, offset = [], 0, 0
    while got < expected:
        page = get_json(page_url(spec["service"], offset, size))
        n = len(page.get("features") or [])
        if n == 0:
            break                                   # merge_pages will refuse
        pages.append(page)
        got += n
        offset += n
        print(f"    page at {offset - n:>6,}: {n} features")
        time.sleep(PAUSE_S)
    return merge_pages(pages, expected)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--layer", action="append", choices=sorted(LAYERS),
                    default=[], help="repeatable; both if omitted")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--page-size", type=int, default=PAGE_SIZE)
    args = ap.parse_args(argv)

    names = args.layer or sorted(LAYERS)
    manifest: dict[str, Any] = {
        "_what": "G3 mask inputs: Maryland CHART cameras and AADT stations",
        "_fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "_partial": bool(args.layer) and set(args.layer) != set(LAYERS),
        "layers": {},
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        spec = LAYERS[name]
        streams = None
        try:
            feats = fetch_layer(name, spec, args.page_size)
            if name == "cameras":
                streams = camera_streams(feats)
            kept = [without(f, spec["drop"]) for f in feats]
            raw = serialise(kept)
            refuse_credentials(raw, name)
        except (urllib.error.URLError, MaskRefused, json.JSONDecodeError,
                TimeoutError) as exc:
            print(f"  {name} REFUSED: {exc}\n  nothing written for this layer.")
            return 1
        path = args.out_dir / f"{name}.geojson"
        path.write_bytes(raw)
        entry = {"service": spec["service"], "what": spec["what"],
                 "file": path.name, "bytes": len(raw),
                 "sha256": hashlib.sha256(raw).hexdigest(),
                 "dropped_fields": spec["drop"], **describe(kept)}
        if name == "aadt":
            entry["aadt_years"] = year_columns(kept)
        if streams is not None:
            links, srep = streams
            # The layer's own links, checked rather than trusted: one probe.
            first = next((v.get("page") or v.get("embed") for v in links.values()), None)
            status, _ = head_status(first) if first else (None, None)
            entry["layer_links"] = {**srep, "probe_url_path": "/video/video.php",
                                    "probe_status": status,
                                    "used_by_viewer": False}
            print(f"  the layer's own camera links: one probed, HTTP {status}")
            try:
                chart = parse_chart_feed(get_json(CHART_FEED))
            except (urllib.error.URLError, MaskRefused, json.JSONDecodeError,
                    TimeoutError) as exc:
                print(f"  CHART camera feed REFUSED: {exc}; no stream file written")
                chart = None
            if chart is not None:
                cams, crep = chart_streams(chart)
                sample = next((s["embed"] for s in cams.values() if s["embed"]), None)
                vstatus, vheaders = head_status(sample) if sample else (None, None)
                policy = {**frame_policy(vheaders), "probe_status": vstatus}
                raw_s = json.dumps({
                    "_what": "CHART cameras and player links, LOCAL ONLY: "
                             "untracked by .gitignore and never published. "
                             "The viewer plays them from CHART's servers.",
                    "_source": CHART_FEED,
                    "_fetched_at_utc": manifest["_fetched_at_utc"],
                    **crep, "frame_policy": policy,
                    "streams": cams}, indent=1).encode("utf-8")
                hit = body_credential(raw_s)
                if hit:
                    crep = {**crep, "written": False,
                            "why": f"carries something shaped like a {hit}"}
                    print(f"  camera streams NOT written: {crep['why']}")
                else:
                    (args.out_dir / "camera-streams.json").write_bytes(raw_s)
                    crep = {**crep, "written": True, "file": "camera-streams.json"}
                    print(f"  CHART's own camera feed: {crep['n_cameras']} cameras, "
                          f"{crep['n_with_link']} with a player link; status "
                          f"{crep['op_status']}; one player probed, HTTP {vstatus}, "
                          f"embeddable: {policy['embeddable']}")
                entry["chart_feed"] = {**crep, "source": CHART_FEED,
                                       "frame_policy": policy,
                                       "vs_layer": cross_check(feats, chart)}
                print(f"  layer against CHART: {entry['chart_feed']['vs_layer']}")
        entry["fetched_at_utc"] = manifest["_fetched_at_utc"]
        manifest["layers"][name] = entry
        print(f"  {name}: {entry['n']:,} written, {len(raw) / 2**20:.1f} MiB, "
              f"{entry['without_usable_point']} without a usable point")
    mpath = args.out_dir / "manifest.json"
    try:
        old = json.loads(mpath.read_text(encoding="utf-8")) if args.layer else None
    except (OSError, ValueError):
        old = None
    manifest = merge_manifest(old, manifest)
    mpath.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"  wrote {args.out_dir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
