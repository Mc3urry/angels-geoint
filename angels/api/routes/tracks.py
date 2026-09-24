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
from angels.config import AOI_SEA, AOI_SEA_CONUS, AOIS, RAW
from angels.core.models import Track

router = APIRouter(tags=["tracks"])

# One adapter per aviation collection footprint, because they read DIFFERENT
# archives: the DC box polls every 30 s into "aviation", the country every
# 10 min into "aviation-conus". The gap that splits one track from the next
# has to follow the cadence -- 900 s is three missed polls on one and a tenth
# of a poll on the other, and leaving it at 900 for the national archive would
# split a transcontinental flight on every dropped poll and hand every
# detector downstream a continent full of short broken tracks.
_AIR_ADAPTERS = {
    name: AviationAdapter(dataset=AOIS[name]["dataset"],
                          max_gap_s=float(AOIS[name]["max_gap_s"]))
    for name in ("air", "conus")
}
class MaritimeArchive:
    """Tracks from the live-AIS archive the maritime collector writes.

    Nothing here is new machinery: the collector writes ais.COLUMNS, one row
    per report, so `ais.load` and `ais.to_tracks` -- the same two functions
    the retrospective analysis runs on the MarineCadastre bulk files -- read
    it unchanged. That was the whole point of choosing that schema.
    """

    domain = "sea"

    def __init__(self, dataset: str) -> None:
        self.dataset = dataset

    def tracks(self, t_start, t_end, bbox):
        from angels.adapters.maritime import ais

        root = RAW / self.dataset
        files = sorted(root.rglob("*.parquet"))
        if not files:
            # An empty archive is not an empty sea, and the difference has to
            # survive as far as the caller. Raising here lets the route say
            # which collector is not running instead of serving a cheerful
            # zero tracks.
            raise ais.AISReadError(
                f"No live-AIS archive under {root}. The maritime collector "
                f"writes it: python scripts/ingest_maritime.py --aoi "
                f"{'conus' if 'conus' in self.dataset else 'sea'}")
        return ais.to_tracks(ais.load(files, t0=t_start, t1=t_end, bbox=bbox))


_SEA_ADAPTERS = {
    "air": MaritimeArchive("maritime-live"),
    "conus": MaritimeArchive("maritime-live-conus"),
}
_SEA_BOXES = {"air": AOI_SEA, "conus": AOI_SEA_CONUS}

_ADAPTERS = {"air": _AIR_ADAPTERS["air"]}

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
    aoi: Literal["air", "conus"] = "air",
    min_points: int = Query(2, ge=2, description="drop tracks shorter than this"),
    limit: int = Query(500, ge=1, le=5000, description="max tracks returned"),
) -> dict[str, Any]:
    adapter = _AIR_ADAPTERS[aoi] if domain == "air" else _SEA_ADAPTERS[aoi]
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

    bbox = tuple(AOIS[aoi]["box"]) if domain == "air" else _SEA_BOXES[aoi]
    try:
        found = adapter.tracks(start, end, bbox)
    except Exception as exc:                                   # noqa: BLE001
        # 404, not 200-with-nothing. "The collector for this water is not
        # running" and "this water is empty" are opposite claims, and only
        # one of them is ever true here.
        raise HTTPException(404, str(exc)) from exc
    tracks = [t for t in found if len(t) >= min_points]
    tracks.sort(key=lambda t: len(t), reverse=True)
    shown = tracks[:limit]

    return {
        "type": "FeatureCollection",
        "features": [track_to_feature(t) for t in shown],
        "properties": {
            "domain": domain,
            "aoi": aoi if domain == "air" else None,
            "label": (AOIS[aoi]["label"] if domain == "air"
                      else ("US waters" if aoi == "conus"
                            else "Chesapeake-Delaware")),
            # Live-collected, not the bulk archive. Both are the same schema
            # and the same readers; they are months apart in latency, and a
            # reader must not mistake one for the other.
            "archive": ("opensky-collector" if domain == "air"
                        else "aisstream-collector"),
            "bbox": list(bbox),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "n_tracks": len(shown),
            "n_tracks_total": len(tracks),
            "truncated": len(tracks) > len(shown),
        },
    }
