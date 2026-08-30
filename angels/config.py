"""One place for every constant that defines a run.

Freeze your area of interest here on day one and do not quietly change it
mid-project. If you widen the box in March, every result computed before then
silently means something different.
"""

from __future__ import annotations

import os
from datetime import timezone
from pathlib import Path

# -- paths -----------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RAW = DATA / "raw"
INTERIM = DATA / "interim"
EVENTS = DATA / "events"
REFERENCE = DATA / "reference"

# -- area of interest ------------------------------------------------------
# (min_lon, min_lat, max_lon, max_lat)
#
# Baltimore. Chosen because federal surveillance aircraft activity here is
# publicly documented, which gives the Phase 2 inversion a sanity check that
# does not depend on your own classifier being right.

AOI = (-77.2, 38.7, -76.0, 39.8)
AOI_NAME = "baltimore"

# -- units and time --------------------------------------------------------
# UTC and SI everywhere internally. Convert at the edges only. Knots, nautical
# miles, feet and local time all try to leak in from source data -- stop them
# in the adapter, never downstream.

TZ = timezone.utc

KNOTS_TO_MPS = 0.514444
FEET_TO_M = 0.3048
NMI_TO_M = 1852.0

# -- credentials -----------------------------------------------------------
# Real values live in .env, which is gitignored. See .env.example.

OPENSKY_CLIENT_ID = os.getenv("OPENSKY_CLIENT_ID", "")
OPENSKY_CLIENT_SECRET = os.getenv("OPENSKY_CLIENT_SECRET", "")
GFW_API_TOKEN = os.getenv("GFW_API_TOKEN", "")

OPENSKY_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/"
    "protocol/openid-connect/token"
)
OPENSKY_API_BASE = "https://opensky-network.org/api"

# Tokens expire after 30 minutes. Refresh with a margin.
OPENSKY_TOKEN_TTL_S = 1800
OPENSKY_TOKEN_MARGIN_S = 120
