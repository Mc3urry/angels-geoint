"""FastAPI service.

Runs today with no data: GET /health works the moment you install deps.
Routes are added phase by phase as the views appear.

    uvicorn angels.api.main:app --reload
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from angels import __version__
from angels.config import AOI, AOI_NAME

app = FastAPI(title="ANGELS", version=__version__,
              description="Radar returns with no attributable source.")

# The web/ page is served as a static file during development, so it arrives
# from a different origin than this API. Tighten before anything is public.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__,
            "aoi": AOI, "aoi_name": AOI_NAME}


# PHASE 1: from angels.api.routes import tracks;   app.include_router(tracks.router)
# PHASE 2: from angels.api.routes import events;   app.include_router(events.router)
# PHASE 4: from angels.api.routes import analysis; app.include_router(analysis.router)
# PHASE 5: from angels.api.routes import forensics; app.include_router(forensics.router)
