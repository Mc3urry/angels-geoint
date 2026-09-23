"""The observed layer: candidates, sites, and what they are not.

    GET /events/candidates?bbox=&date=&reception=heard
    GET /events/sites
    GET /events/summary

EVERYTHING HERE IS RETROSPECTIVE, AND THE PAYLOAD SAYS SO

/live serves cooperative reporting, current to seconds. This route serves
the other half of the thesis -- radar detections no AIS report explained --
and they are hours to days old by construction, from passes twelve days
apart. Drawn on the same map without a label, the two become one picture of
"what is out there", which is the exact confusion this project exists to
refuse. So every payload carries `evidence: "observed"`, its own
`as_of`, and the caveats that survived the analysis.

WHAT A CANDIDATE IS, IN ONE LINE

A gated radar detection, in water the radar actually searched, that no AIS
track explains, that is not at a fixed structure, and that sits in water
where AIS is demonstrably heard. Everything before the last clause is
subtraction; the last clause is what stops "nobody was listening" from
looking like "somebody was hiding".
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from angels.config import EVENTS

router = APIRouter(prefix="/events", tags=["events"])

CANDIDATES = "candidates-scored.geojson"
FALLBACK = "candidates.geojson"
SITES = "persistent-sites.geojson"


def _read(name: str) -> dict[str, Any] | None:
    path = EVENTS / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def pooled_calibration() -> dict[str, Any] | None:
    """The detection rate this layer was measured at, pooled over passes.

    THE SAMPLING RATE OF THE OBSERVED LAYER.

    A candidate count on its own reads as a census. It is a sample, and the
    sampling fraction is known, different for every vessel size, and very far
    from one: about 4% at 0-15 m against about 93% above 100 m. Serving the
    count without the curve invites the reader to do the one thing the whole
    method forbids -- treat "not detected" as "not there" for the sizes this
    sensor cannot see.

    Counts add across passes; density and the scorable radius are per-pass
    quantities and have no pooled value, so they are dropped rather than
    averaged into something that means nothing.
    """
    strata: dict[str, list[int]] = {}
    curve: dict[str, list[int]] = {}
    chance = 0.0
    unscorable = 0
    n_passes = 0
    edges: list[float] | None = None
    length_m: float | None = None

    for p in sorted(EVENTS.glob("dark-*.geojson")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cal = doc.get("properties", {}).get("calibration")
        if not cal:
            continue
        n_passes += 1
        edges = cal.get("curve_edges_m", edges)
        length_m = cal.get("detectable_length_m", length_m)
        chance += cal.get("expected_chance_matches", 0.0) or 0.0
        unscorable += cal.get("n_unscorable", 0) or 0
        for key, dest in (("strata", strata), ("curve", curve)):
            for name, r in (cal.get(key) or {}).items():
                got = dest.setdefault(name, [0, 0])
                got[0] += r.get("found", 0) or 0
                got[1] += r.get("scored", 0) or 0

    if not n_passes:
        return None

    def rate(found: int, scored: int) -> dict[str, Any]:
        from angels.core.detectors.calibration import wilson
        lo, hi = wilson(found, scored)
        return {
            "found": found, "scored": scored,
            "rate": None if scored == 0 else round(found / scored, 4),
            "ci95": [None if lo != lo else round(lo, 4),
                     None if hi != hi else round(hi, 4)],
        }

    headline = strata.get(">=25 m", [0, 0])
    return {
        "n_passes": n_passes,
        "detectable_length_m": length_m,
        "curve_edges_m": edges,
        "strata": {k: rate(*v) for k, v in strata.items()},
        "curve": {k: rate(*v) for k, v in curve.items()},
        "headline": rate(*headline),
        "n_unscorable": unscorable,
        "expected_chance_matches": round(chance, 2),
        "what": (
            "The share of AIS-reporting vessels this detector actually found, "
            "measured on the same passes, in the same searched water. It is "
            "the sampling rate of the observed layer: below about 25 m the "
            "sensor misses most of what is there, so absence at those sizes "
            "is not evidence of absence."),
    }


def _in_bbox(lon: float, lat: float, bbox: str | None) -> bool:
    if not bbox:
        return True
    try:
        lomin, lamin, lomax, lamax = (float(v) for v in bbox.split(","))
    except ValueError:
        return True
    return lomin <= lon <= lomax and lamin <= lat <= lamax


@router.get("/candidates")
async def candidates(bbox: str | None = Query(None, description="lon,lat,lon,lat"),
                     date: str | None = None,
                     reception: str = "heard",
                     limit: int = 5000) -> dict[str, Any]:
    doc = _read(CANDIDATES) or _read(FALLBACK)
    if doc is None:
        raise HTTPException(404, (
            "No candidate file yet. Run scripts/persistent_sites.py and "
            "scripts/ais_coverage.py. This is a missing product, not an "
            "empty sea."))
    keep = {r.strip() for r in reception.split(",") if r.strip()} \
        if reception and reception != "all" else None

    feats = []
    for f in doc.get("features", []):
        lon, lat = f["geometry"]["coordinates"]
        p = f["properties"]
        if not _in_bbox(lon, lat, bbox):
            continue
        if date and p.get("date") != date:
            continue
        if keep and p.get("reception", "heard") not in keep:
            continue
        feats.append(f)
        if len(feats) >= limit:
            break

    props = dict(doc.get("properties", {}))
    dates = sorted({f["properties"].get("date") for f in doc.get("features", [])
                    if f["properties"].get("date")})
    return {
        "type": "FeatureCollection",
        "features": feats,
        "properties": {
            **props,
            "evidence": "observed",
            "sensor": "sentinel1-vv-cfar",
            "n": len(feats),
            "n_total": len(doc.get("features", [])),
            "dates": dates,
            "as_of": dates[-1] if dates else None,
            "reception_filter": sorted(keep) if keep else "all",
            # Travels WITH the features. A count without its sampling rate is
            # the misreading this project is about.
            "detection_rate": pooled_calibration(),
            # The three sentences that must travel with this layer.
            "retrospective": True,
            "revisit_days": 12,
            "caveat": (
                "Candidates are unexplained radar detections, not confirmed "
                "dark vessels. They are hours to days old, from passes about "
                "twelve days apart, and they carry the detector's residual "
                "false-alarm rate. Read them beside the detection rate for "
                "the same pass."),
        },
    }


@router.get("/sites")
async def sites(fixed_only: bool = True) -> dict[str, Any]:
    """Places detections keep returning to -- the furniture of the sea."""
    doc = _read(SITES)
    if doc is None:
        raise HTTPException(404, "No persistent-sites.geojson yet. Run "
                                 "scripts/persistent_sites.py.")
    feats = [f for f in doc.get("features", [])
             if not fixed_only or f["properties"].get("fixed")]
    return {
        "type": "FeatureCollection",
        "features": feats,
        "properties": {
            **doc.get("properties", {}),
            "evidence": "observed",
            "n": len(feats),
            "what": ("Sites detected on several passes and never explained "
                     "by AIS: wind turbines, platforms, lighthouses, bridge "
                     "islands. Removed from the candidate list, shown here "
                     "so the removal is visible rather than silent."),
        },
    }


@router.get("/summary")
async def summary() -> dict[str, Any]:
    """One line per layer, for the panel."""
    cand = _read(CANDIDATES) or _read(FALLBACK)
    site = _read(SITES)
    return {
        "candidates": None if cand is None else {
            "n": len(cand.get("features", [])),
            "gate": {k: cand.get("properties", {}).get(k)
                     for k in ("gate_min_snr", "gate_min_pixels")},
            "reception_counts": cand.get("properties", {}).get(
                "reception_counts"),
            "dates": sorted({f["properties"].get("date")
                             for f in cand.get("features", [])
                             if f["properties"].get("date")}),
        },
        # Served WITH the count, never on a separate request the front end
        # could forget to make.
        "detection_rate": pooled_calibration(),
        "sites": None if site is None else {
            "n": len(site.get("features", [])),
            "n_fixed": site.get("properties", {}).get("n_fixed"),
        },
        "evidence": "observed",
        "note": ("The observed layer is what SAR saw minus what AIS "
                 "explained minus what is always there. The live layer is "
                 "what chose to broadcast. They are never the same map."),
    }
