"""Track geometry for the map.

    GET /tracks?start=&end=&domain=&min_points=&limit=

Returns a GeoJSON FeatureCollection of LineStrings, one per track.

Deliberately thin. All the work -- reading the archive, grouping reports,
splitting on silence -- lives in the adapter, because the moment historical
access lands the adapter's body changes and this file does not.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query

from angels.adapters.aviation.opensky import AviationAdapter
from angels.config import AOI_AIR, AOI_SEA
from angels.core.models import Track

router = APIRouter(tags=["tracks"])

_ADAPTERS = {"air": AviationAdapter()}

# A one-second ADS-B feed produces far more vertices than a map can show. The
# renderer cannot tell the difference and the payload triples, so thin long
# tracks before serialising. Endpoints are always kept -- losing where a track
# started or stopped would be losing the interesting part.
MAX_VERTICES = 250


def decimate(track: Track, limit: int = MAX_VERTICES) -> list:
    if len(track) <= limit:
        return track.reports
    step = len(track) / limit
    idx = sorted({int(i * step) for i in range(limit)} | {len(track) - 1})
    return [track.reports[i] for i in idx]


def track_to_feature(track: Track) -> dict[str, Any]:
    reports = decimate(track)
    coords = [[r.position.lon, r.position.lat] for r in reports]
    alts = [r.position.alt_m for r in reports if r.position.alt_m is not None]

    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": {
            "platform_id": track.platform_id,
            "domain": track.domain,
            "t_start": track.t_start.isoformat(),
            "t_end": track.t_end.isoformat(),
            "duration_s": track.duration.total_seconds(),
            "n_reports": len(track),
            "n_vertices": len(coords),
            "path_km": round(track.path_length_m() / 1000, 1),
            "net_km": round(track.net_displacement_m() / 1000, 1),
            "max_alt_m": max(alts) if alts else None,
        },
    }


@router.get("/tracks")
def get_tracks(
    start: datetime | None = Query(None, description="UTC ISO8601; default 2h ago"),
    end: datetime | None = Query(None, description="UTC ISO8601; default now"),
    domain: Literal["air", "sea"] = "air",
    min_points: int = Query(2, ge=2, description="drop tracks shorter than this"),
    limit: int = Query(500, ge=1, le=5000, description="max tracks returned"),
) -> dict[str, Any]:
    adapter = _ADAPTERS.get(domain)
    if adapter is None:
        raise HTTPException(501, f"no adapter for domain '{domain}' yet")

    end = end or datetime.now(timezone.utc)
    start = start or end - timedelta(hours=2)

    # Query params arrive naive if the caller omits a timezone. Assume UTC
    # rather than rejecting -- but assume it explicitly, in one place, here at
    # the edge. Nothing downstream should ever see a naive datetime.
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if end <= start:
        raise HTTPException(400, "end must be after start")

    bbox = AOI_AIR if domain == "air" else AOI_SEA
    tracks = [t for t in adapter.tracks(start, end, bbox) if len(t) >= min_points]
    tracks.sort(key=lambda t: len(t), reverse=True)
    shown = tracks[:limit]

    return {
        "type": "FeatureCollection",
        "features": [track_to_feature(t) for t in shown],
        "properties": {
            "domain": domain,
            "bbox": bbox,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "n_tracks": len(shown),
            "n_tracks_total": len(tracks),
            "truncated": len(tracks) > len(shown),
        },
    }
