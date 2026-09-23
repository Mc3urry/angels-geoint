"""FastAPI service. Serves the API and the front end from one process.

    uvicorn angels.api.main:app --reload
    -> http://localhost:8000

One server rather than two, deliberately. The front end is a handful of static
files, and mounting them here removes a second process to start, a second port
to remember, and the CORS configuration entirely -- the page and the API are
now the same origin, so there is no cross-origin request to permit.

Routes are added phase by phase as the views appear. /health works with no
data at all, which makes it the first thing to check when something is wrong.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from angels import __version__
from angels.api.routes import coverage, events, live, tracks
from angels.config import AOI_AIR, AOI_SEA, REGION, REGION_NAME, ROOT

app = FastAPI(
    title="ANGELS",
    version=__version__,
    description="Radar returns with no attributable source.",
)

# Same-origin now that this process serves the page too, so CORS is not
# needed for normal use. Kept, narrowly, only so the old two-server setup and
# any local tooling on another port still work.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    """Liveness plus the region this instance is configured for.

    The bbox echo is deliberate: the front end draws its initial view from
    hardcoded bounds, and this is how you catch the two drifting apart.
    """
    return {
        "status": "ok",
        "version": __version__,
        "region": REGION_NAME,
        "bbox": {"region": REGION, "air": AOI_AIR, "sea": AOI_SEA},
    }


app.include_router(live.router)
app.include_router(tracks.router)
app.include_router(coverage.router)
app.include_router(events.router)

# MOUNTED LAST, and it must stay last. StaticFiles at "/" is a catch-all --
# anything registered after it would be shadowed and silently 404.
# html=True serves index.html for the bare path.
app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")

# PHASE 4: from angels.api.routes import analysis;  app.include_router(analysis.router)
# PHASE 5: from angels.api.routes import forensics; app.include_router(forensics.router)
