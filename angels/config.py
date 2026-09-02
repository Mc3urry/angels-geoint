"""One place for every constant that defines a run.

THE ORGANISING PRINCIPLE: one region, every domain.

Everything this project ingests, analyses and reports is clipped to REGION
below. That is not just tidiness -- it is what makes the central experiment
work. The thesis asks whether non-cooperative behaviour concentrates at
governance discontinuities, and testing that across two domains IN THE SAME
PLACE means region is controlled and domain is the variable. Compare Baltimore
airspace against the South Atlantic and any difference you find could be
weather, economy, season, or enforcement regime rather than anything about
the domains themselves.

It also collapses the data problem. Every download gets clipped to one box:
days of AIS instead of 116 GB, one metro of flight data instead of the planet.

Freeze these on day one. If you widen a box in March, every result computed
before then silently means something different, and you will not remember
which figures were affected.
"""

from __future__ import annotations

import math
import os
from datetime import timezone
from pathlib import Path

from dotenv import load_dotenv

# -- paths -----------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RAW = DATA / "raw"
INTERIM = DATA / "interim"
EVENTS = DATA / "events"
REFERENCE = DATA / "reference"

# ==========================================================================
# THE REGION
#
# The Chesapeake-Potomac corridor: Washington and Baltimore inland, the bay,
# and the continental shelf out past the EEZ limit.
#
# All boxes are (min_lon, min_lat, max_lon, max_lat) -- GeoJSON axis order.
# Adapters convert to whatever their API wants; nothing else should.
# ==========================================================================

REGION = (-78.0, 36.0, -71.0, 40.0)
REGION_NAME = "chesapeake-potomac"

# Per-domain operating boxes. Both sit inside REGION. They differ because the
# domains occupy different parts of it -- polling flight data over 200 nm of
# open ocean wastes quota, and clipping AIS to Dulles returns nothing.

AOI_AIR = (-77.7, 38.3, -76.0, 39.6)
"""DC-Baltimore. Contains DCA, BWI, IAD, the Special Flight Rules Area, the
Flight Restricted Zone, and P-56 over the Capitol."""

AOI_SEA = (-77.2, 36.0, -71.0, 39.6)
"""Chesapeake Bay and the shelf. Contains the Port of Baltimore, Hampton
Roads, Naval Station Norfolk, and the 3, 12, 24 and 200 nm limits."""

# Backwards compatibility: earlier code used a single AOI.
AOI = AOI_AIR

# -- why this region -------------------------------------------------------
#
# AVIATION boundaries, inland:
#   DC Special Flight Rules Area  -- the most heavily enforced airspace in the
#                                    country, with a hard edge and real
#                                    consequences for crossing it
#   Flight Restricted Zone        -- tighter still, inside the SFRA
#   P-56A/B                       -- prohibited over the Capitol and Naval
#                                    Observatory
#   Class B shelves               -- DCA, BWI, IAD
#
# MARITIME boundaries, offshore. Four NESTED limits, which is the real prize:
# you can test whether the effect scales with the STRENGTH of the
# jurisdictional change rather than merely its presence.
#
#   3 nm   state / federal line   ~75.91 W off Virginia Beach
#  12 nm   territorial sea        ~75.72 W
#  24 nm   contiguous zone
# 200 nm   EEZ outer limit        ~71.81 W
#
# KNOWN CONFOUNDER, plan for it: Naval Station Norfolk is the largest naval
# base in the world and sits inside AOI_SEA. Warships legitimately do not
# broadcast AIS, so a meaningful share of "dark vessels" there will be the US
# Navy going about entirely normal business. Navy operating areas are
# published -- mask them, and say in the write-up that you did. This is the
# maritime twin of the parked-aircraft-at-DCA problem: an obvious confounder
# that a careless analysis reports as a finding.

TERRITORIAL_LIMITS_NM = (3, 12, 24, 200)

# -- study period ----------------------------------------------------------
# One week is enough to build against. Pick something unremarkable -- no major
# storm, no holiday. You want a baseline, not an interesting week.

STUDY_START = "2024-09-09"
STUDY_END = "2024-09-15"

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
#
# Loaded HERE rather than in each entry point. Every script and the API import
# this module, so doing it once means nothing can forget -- which is exactly
# what happened when only the ingest script called load_dotenv() and the API
# returned 503 for want of credentials that were sitting on disk the whole
# time.
#
# override=False so a real environment variable still wins over the file,
# which is what you want in CI or a container.

load_dotenv(ROOT / ".env", override=False)

OPENSKY_CLIENT_ID = os.getenv("OPENSKY_CLIENT_ID", "")
OPENSKY_CLIENT_SECRET = os.getenv("OPENSKY_CLIENT_SECRET", "")
GFW_API_TOKEN = os.getenv("GFW_API_TOKEN", "")

OPENSKY_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/"
    "protocol/openid-connect/token"
)
OPENSKY_API_BASE = "https://opensky-network.org/api"

OPENSKY_TOKEN_TTL_S = 1800      # tokens last 30 minutes
OPENSKY_TOKEN_MARGIN_S = 120    # refresh with this much to spare


# -- helpers ---------------------------------------------------------------

def contains(box: tuple[float, float, float, float],
             lat: float, lon: float) -> bool:
    """Is this point inside the box? Every ingest path should call this."""
    return box[0] <= lon <= box[2] and box[1] <= lat <= box[3]


def clip_expr(box: tuple[float, float, float, float],
              lat_col: str = "LAT", lon_col: str = "LON") -> str:
    """SQL WHERE clause for clipping to a box. For DuckDB over Parquet or CSV.

        duckdb.sql(f"SELECT * FROM 'ais/*.csv' WHERE {clip_expr(AOI_SEA)}")
    """
    lomin, lamin, lomax, lamax = box
    return (f"{lon_col} BETWEEN {lomin} AND {lomax} "
            f"AND {lat_col} BETWEEN {lamin} AND {lamax}")


def box_area_km2(box: tuple[float, float, float, float]) -> float:
    """Rough area, for sanity-checking that a box is the size you think."""
    lomin, lamin, lomax, lamax = box
    mid = math.radians((lamin + lamax) / 2)
    return ((lomax - lomin) * 111.32 * math.cos(mid)) * ((lamax - lamin) * 110.57)
