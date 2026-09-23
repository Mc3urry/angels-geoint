"""When did the radar last look HERE? The question a live map cannot answer.

    GET /coverage?lon=-75.4&lat=36.9
    GET /coverage/summary

THE POINT OF THIS ROUTE

The live map shows vessels that are broadcasting right now. The moment a
viewer asks "so what is NOT on this map", the honest answer depends entirely
on when independent observation last covered that particular water -- and
for Sentinel-1 that is somewhere between a few hours and twelve days ago,
different for every point, because the swaths do not tile the study area
evenly.

Without this, the front end can only say "SAR is retrospective", which is
true and useless. With it, clicking a patch of sea gives: last searched
2024-12-30 22:59 UTC, 18 days ago, by slice 3 of that pass, which examined
this cell fully. That is the sentence that turns a cooperative-reporting
display into an analysis tool -- it tells the viewer what evidence could
exist about this spot, and therefore what their own eyes on the live map are
allowed to conclude.

IT ANSWERS FOR A POINT, NOT FOR THE BOX

searched_km2 is a total. A vessel is at a place. So this reads the same
per-cell searched grids the detection rate uses (0.01 deg, the fraction of
each cell the detector actually examined), not the scene totals.

The grids are read from data/events/sar-*.geojson once and cached, because
they are a few hundred kilobytes each and the front end asks per click.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query

from angels.adapters.maritime.searched import SEARCHED, SearchedArea
from angels.config import EVENTS

router = APIRouter(tags=["coverage"])

_passes: list[dict[str, Any]] | None = None


def load_passes(force: bool = False) -> list[dict[str, Any]]:
    """Every scene with a searched grid, newest first.

    A scene without a grid is skipped rather than counted as covering
    nothing -- it predates the grid and its coverage is unknown, which is a
    third answer and must not be folded into "not searched".
    """
    global _passes
    if _passes is not None and not force:
        return _passes

    out: list[dict[str, Any]] = []
    for p in sorted(EVENTS.glob("sar-*.geojson")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        meta = doc.get("properties", {})
        area = SearchedArea.from_json(meta.get("searched_grid"))
        if area is None or not meta.get("acquired"):
            continue
        out.append({
            "scene": meta.get("scene", p.stem),
            "acquired": meta["acquired"],
            "searched_km2": meta.get("searched_km2"),
            "mask_source": meta.get("mask_source"),
            "n_detections": len(doc.get("features", [])),
            "area": area,
        })
    out.sort(key=lambda d: d["acquired"], reverse=True)
    _passes = out
    return out


def _age_days(iso: str) -> float:
    t = datetime.fromisoformat(iso)
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return (datetime.now(UTC) - t).total_seconds() / 86400.0


@router.get("/coverage")
async def coverage_at(lon: float = Query(...), lat: float = Query(...),
                      history: int = 5) -> dict[str, Any]:
    """Independent observation of one point: when, by what, how completely."""
    hits = []
    for p in load_passes():
        frac = p["area"].fraction(lon, lat)
        if frac <= 0:
            continue
        hits.append({
            "scene": p["scene"],
            "acquired": p["acquired"],
            "age_days": round(_age_days(p["acquired"]), 2),
            "searched_fraction": round(frac, 2),
            # Below SEARCHED the cell is part-searched shoreline: reported,
            # never counted. The same three-way split the rate uses.
            "status": ("searched" if frac >= SEARCHED else "part-searched"),
            "mask_source": p["mask_source"],
        })
        if len(hits) >= max(1, history):
            break

    last = hits[0] if hits else None
    return {
        "lon": lon, "lat": lat,
        "sensor": "sentinel1-vv-cfar",
        "evidence": "observed",
        "last": last,
        "history": hits,
        "n_passes_on_disk": len(load_passes()),
        # The sentence the front end should print when there is nothing.
        "note": (
            "No Sentinel-1 pass on disk searched this point. Absence of a "
            "vessel here has never been observed by this project -- only "
            "unreported, which is not the same claim."
            if last is None else
            f"Last independently observed {last['age_days']:.1f} days ago; "
            f"anything that arrived since is unobserved by construction."),
    }


@router.get("/coverage/summary")
async def coverage_summary() -> dict[str, Any]:
    """What observation exists at all, for the panel's standing line."""
    passes = load_passes()
    if not passes:
        return {"n_passes": 0, "note": "No searched grids on disk."}
    newest, oldest = passes[0], passes[-1]
    return {
        "n_passes": len(passes),
        "newest": newest["acquired"],
        "oldest": oldest["acquired"],
        "newest_age_days": round(_age_days(newest["acquired"]), 2),
        "oldest_age_days": round(_age_days(oldest["acquired"]), 2),
        "searched_km2_total": round(
            sum(p["searched_km2"] or 0 for p in passes), 1),
        "revisit_days": 12,
        "note": ("Sentinel-1 revisits this area about every 12 days. A live "
                 "map is cooperative reporting only; the observed layer is "
                 "always retrospective."),
    }
