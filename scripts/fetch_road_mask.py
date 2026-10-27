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
(mechanism X: the payload is the surface). They are dropped by name, and
every byte that is written is scanned with the project's body-credential
patterns before it is written. A match refuses the write and reports the
pattern name, never the match.

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
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

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
        try:
            feats = fetch_layer(name, spec, args.page_size)
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
        manifest["layers"][name] = entry
        print(f"  {name}: {entry['n']:,} written, {len(raw) / 2**20:.1f} MiB, "
              f"{entry['without_usable_point']} without a usable point")
    (args.out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"  wrote {args.out_dir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
