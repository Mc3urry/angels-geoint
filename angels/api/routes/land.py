"""The Land tab: what the road collectors have already written.

    GET /land/vehicles?minutes=15        newest fix per transit vehicle
    GET /land/status                     every feed's last outcome, both layers
    GET /land/workzones?bbox=w,s,e,n     declared work zones in a box
    GET /land/states?bbox=w,s,e,n        the state lines G2b selected on

Reads files, never the agencies. See `angels/core/land.py` for what each
payload means and, as important, what an absence on it does not mean.
Everything here is cooperative: vehicles reporting themselves, and agencies
declaring their own work zones. The comparison of the two is G6 and does not
exist yet, and the payloads say `evidence: cooperative` so no client can
present them as an observation.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from angels.core import land

router = APIRouter(prefix="/land", tags=["land"])


def _bbox(text: str | None):
    try:
        return land.parse_bbox(text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/vehicles")
def vehicles(minutes: float = Query(15, ge=1, le=120)) -> dict:
    if not land.VEHICLES.exists():
        raise HTTPException(status_code=503, detail=(
            "the roads-vehicles collector has written nothing yet: "
            ".\\collector.ps1 status"))
    return land.recent_vehicles(now_s=land.now(), window_s=minutes * 60)


@router.get("/status")
def status() -> dict:
    t = land.now()
    rows = land.vehicle_status(now_s=t)
    answered = [r for r in rows if r["outcome"] == "ok"]
    return {
        "evidence": "cooperative",
        "vehicles": rows,
        "n_feeds": len(rows),
        "n_answered": len(answered),
        "workzones": land.wzdx_status(now_s=t),
        "note": ("outcomes are the collectors' own words; no cause is "
                 "inferred from them"),
    }


@router.get("/workzones")
def workzones(bbox: str = Query(..., description="west,south,east,north")) -> dict:
    return land.workzones(bbox=_bbox(bbox))


@router.get("/states")
def states(bbox: str | None = None) -> dict:
    return land.states_in(bbox=_bbox(bbox))
