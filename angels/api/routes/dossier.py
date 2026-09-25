"""The dossier: one page per unexplained detection.

    GET  /dossier/list          ranked summaries, filterable
    GET  /dossier/{key}         one full record
    GET  /dossier/chip/{key}    its image, when one was generated
    GET  /dossier/meta          counts, caveats, and the ranking's own bias

WHY THIS IS A ROUTE OVER A PRECOMPUTED FILE

`build_dossiers.py` does the joining -- AIS, persistence, limits, labels,
classifier -- because that work reads a 3.3 GB reference tree and takes
about forty seconds, which is not a thing to do inside a request. The route
serves what the script produced and adds nothing to it. If a number here is
wrong, it is wrong in `dossiers.json` and the script is where to look.

WHAT THE LIST ENDPOINT WILL NOT DO

It will not sort by distance to a limit, and it will not filter by band.

The research question is whether non-cooperative behaviour concentrates at
governance discontinuities. An interface that let a reader pull up "the
strongest candidates within 2 nm" would produce, on demand, a picture that
looks exactly like the finding the study is testing for -- assembled by the
query rather than observed in the world. The band is on every record, and
`/dossier/meta` reports how far from band-neutral the ranking already is and
why. That is the honest version: show the bias, do not offer a handle that
manufactures it.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from angels.config import EVENTS, INTERIM

router = APIRouter(prefix="/dossier", tags=["dossier"])

DOSSIERS = EVENTS / "dossiers.json"
CHIPS = INTERIM / "candidates"


@lru_cache(maxsize=1)
def _doc() -> dict[str, Any]:
    if not DOSSIERS.exists():
        raise HTTPException(404, "No dossiers.json. Run "
                                 "scripts/build_dossiers.py first.")
    return json.loads(DOSSIERS.read_text(encoding="utf-8"))


def _summary(r: dict) -> dict:
    a = r["assessment"]
    return {
        "key": r["key"], "date": r["date"], "lon": r["lon"], "lat": r["lat"],
        "strength": a["strength"], "p_vessel": a["p_vessel"],
        "label": (a["label"] or {}).get("verdict"),
        "reception": r["reception"]["class"],
        "length_m_approx": r["radar"].get("length_m_approx"),
        "band": r["geography"]["band"],
        "nm_to_any_limit": r["geography"]["nm_to_any_limit"],
        "has_chip": bool(r.get("chip")),
        "nearest_ais_m": (r["ais"]["nearest"] or {}).get("distance_m"),
        "persistence": (r["persistence"] or {}).get("verdict"),
        "why": a["why"],
    }


@router.get("/meta")
def meta() -> dict:
    d = _doc()
    return {k: d[k] for k in ("what", "ranking", "n", "with_chip",
                              "with_label", "with_ais", "ranking_bias",
                              "caveats") if k in d}


@router.get("/list")
def listing(limit: int = Query(50, ge=1, le=500),
            offset: int = Query(0, ge=0),
            reception: str | None = None,
            labelled: bool | None = None,
            min_strength: float = Query(0.0, ge=0.0, le=1.0)) -> dict:
    """Ranked summaries. No band or distance filter -- see the module note."""
    rs = _doc()["records"]
    out = [r for r in rs
           if r["assessment"]["strength"] >= min_strength
           and (reception is None or r["reception"]["class"] == reception)
           and (labelled is None
                or bool(r["assessment"]["label"]) == labelled)]
    return {"total": len(out), "offset": offset,
            "items": [_summary(r) for r in out[offset:offset + limit]]}


@router.get("/chip/{key}")
def chip(key: str):
    rec = next((r for r in _doc()["records"] if r["key"] == key), None)
    if rec is None or not rec.get("chip"):
        raise HTTPException(404, "no chip was generated for this candidate")
    p = (CHIPS / rec["chip"]).resolve()
    if CHIPS.resolve() not in p.parents or not p.exists():
        raise HTTPException(404, "chip file missing")
    return FileResponse(p, media_type="image/png")


@router.get("/{key}")
def one(key: str) -> dict:
    rec = next((r for r in _doc()["records"] if r["key"] == key), None)
    if rec is None:
        raise HTTPException(404, "no such candidate")
    return rec
