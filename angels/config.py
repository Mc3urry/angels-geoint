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
LIVE = INTERIM / "live"
"""Latest-poll snapshots the collectors write for the viewer to read, so a
browser tab never spends credits the archive needs. See
opensky.write_snapshot."""
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

# ==========================================================================
# THE NATIONAL BOX
#
# REGION above stays the study area -- it is what the two-domain comparison
# is controlled on, and nothing about it changes. AOI_CONUS is a SECOND,
# coarser collection footprint running alongside it, for one specific reason:
#
#   The thesis claim is about governance discontinuities as a CLASS. One
#   boundary (the DC SFRA) is a case study, and a committee is right to ask
#   whether the effect is a property of boundaries or a property of
#   Washington. CONUS gives ~37 Class B airspaces, the prohibited areas, the
#   MOA network and the ADIZ -- enough to say "boundaries of type X show the
#   effect and type Y does not, controlling for traffic density", which is a
#   statistical claim rather than an anecdote.
#
# OpenSky prices a request by the AREA of the box, in four coarse steps (see
# opensky_credits below), NOT by how many aircraft come back. So the whole
# country costs four credits against Chesapeake-Potomac's one -- 4x, not the
# 600x the area ratio would suggest. That is the fact that makes this
# affordable, and it is worth stating plainly in the methods section.
#
# THE HONEST COST, to be written up rather than discovered in review: over a
# 2-degree box, receiver coverage is near enough uniform to ignore. Over CONUS
# it is emphatically not -- OpenSky is dense in the Northeast corridor and
# thin over the Great Basin, traffic density varies the same way, and both
# correlate with terrain and population. A naive national map of "silences" is
# a map of where nobody is listening. core.coverage stops being a nice-to-have
# and becomes load-bearing, and it has to be spatially varying rather than a
# scalar.
# ==========================================================================

AOI_CONUS = (-125.0, 24.0, -66.5, 49.5)
"""The continental United States. ~1,450 square degrees -> 4 credits/request."""

# The same box for the sea. aisstream has NO quota, so the national AIS view
# costs nothing to request -- but it is not free either, and the cost is
# paid in a different currency: the socket delivers every message in the box,
# so widening it multiplies the message rate and RESETS the table. A vessel
# at anchor reports every three minutes, so after a widen the map is honestly
# incomplete for at least that long, which is why the stream widens ONCE, on
# demand, and never narrows again. See angels/api/routes/live.py.
AOI_SEA_CONUS = AOI_CONUS


# Every collection footprint, in one registry. A collector is defined by a
# box, the archive directory it writes to, and the name its heartbeats and
# lock are filed under -- and those three must stay in step, which is why
# they live together here rather than as three arguments at three call sites.
#
# "air" keeps the legacy dataset and collector names so the existing archive
# and heartbeat history remain valid. Renaming them would orphan both.

AOIS: dict[str, dict] = {
    "air": {
        "box": AOI_AIR,
        "dataset": "aviation",
        "collector": "aviation",
        "label": "DC-Baltimore",
        "interval_s": 30,
        "max_gap_s": 900,
    },
    "conus": {
        "box": AOI_CONUS,
        "dataset": "aviation-conus",
        "collector": "aviation-conus",
        "label": "continental US",
        "interval_s": 600,
        # Three missed polls, matching the air box's thirty. A gap threshold
        # is only meaningful relative to the sample rate: leaving this at 900
        # would split a track on any single dropped poll, and every detector
        # downstream would then see a continent full of short broken tracks
        # and read it as evidence.
        "max_gap_s": 1800,
    },
}

# Why two cadences rather than one.
#
# Orbit, loiter and the surveillance-aircraft inversion all depend on sampling
# a TURN RATE. A Cessna flying a 2 km orbit at 60 m/s closes the circuit in
# about two minutes; at ten-minute polling you get one dot per orbit and the
# pattern is not merely noisy, it is absent. That work needs ~30 s.
#
# Gap detection, identity collisions and distance-to-boundary statistics ask
# where things are and whether they reported at all. Those survive coarse
# sampling intact. Breadth and depth want different rates and no single rate
# serves both, so run two collectors.
#
# Budget, against 4,000 credits/day on a registered account:
#     air   at  30 s   2,880 polls x 1 = 2,880
#     conus at 600 s     144 polls x 4 =   576
#                                       -------
#                                         3,456   (~540 to spare)

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

# Approximate LONGITUDE of each limit off Virginia Beach, for selecting which
# satellite passes are worth downloading.
#
# A STAND-IN, AND LABELLED AS ONE. A maritime limit is a distance from the
# baseline, so it is a curve, not a meridian -- these values are only usable
# because the Virginia coast runs nearly north-south across the latitudes this
# study cares about. They are for deciding "is this pass worth a gigabyte",
# never for computing a distance-to-boundary in the analysis. That needs the
# real coastline, and comes from the NOAA maritime limits layer in
# scripts/fetch_reference.py.
#
# Derived from one coast reference (-75.97 at 37N, 48.0 nm per degree of
# longitude) rather than typed in. The derivation reproduces BOTH independently
# documented values above -- 3 nm at -75.91 and 200 nm at -71.81 -- which is
# why the undocumented 24 nm figure can be trusted to the same tolerance.
_COAST_LON = -75.97
_NM_PER_DEG_LON = 48.0

LIMIT_LONGITUDES = {
    nm: round(_COAST_LON + nm / _NM_PER_DEG_LON, 2)
    for nm in TERRITORIAL_LIMITS_NM
}


def limits_crossed(lon_min: float, lon_max: float) -> tuple[int, ...]:
    """Which jurisdictional limits fall inside this longitude span.

    The number that should decide what to download. "54% of the study box" is
    a measure of area and says nothing about whether the scene contains a
    boundary -- and the boundaries are the independent variable of the entire
    experiment. A pass covering half the box but no limit at all answers no
    question this project is asking.
    """
    return tuple(nm for nm, lon in LIMIT_LONGITUDES.items()
                 if lon_min <= lon <= lon_max)

# -- study period ----------------------------------------------------------
#
# THE WINDOW IS SET BY THE SLOWEST SOURCE, NOT BY RECENCY.
#
# The maritime argument is a comparison: what the radar observed against what
# was reported. Sentinel-1 is available within hours of acquisition, so it is
# tempting to study the last nine months. MarineCadastre AIS is not.
#
# Measured 2026-09-16 by scripts/check_ais_lag.py, bisecting the bulk archive:
#
#     newest AIS day served   2024-12-31
#     lag                     624 days
#
# Not the 145-165 days their FAQ documents. And the frontier landing exactly
# on 31 December is the tell: this is published a YEAR AT A TIME, not on a
# rolling delay. 2025 is not late, it is unreleased, and there is no date at
# which to expect it.
#
# So the observation window is 2024, where both halves exist today. Sentinel-1
# covers it completely and the archive costs nothing extra to reach back into.
# Six scenes already downloaded from August and September 2026 are not wasted
# -- they are the forward half, and they become matchable whenever 2026 is
# published -- but nothing can be concluded from them in the meantime.
#
# RE-MEASURE BEFORE TRUSTING THESE DATES:
#     python scripts/check_ais_lag.py --passes
# A frontier that has moved means a wider window is available.

AIS_FRONTIER = "2024-12-31"        # measured, not assumed. See above.

# Nine months ending at the frontier: long enough for seasonality, and it
# contains roughly 42 Sentinel-1 passes at the 6.4-day effective repeat.
OBSERVE_START = "2024-04-01"
OBSERVE_END = AIS_FRONTIER

# One unremarkable week to build and test against -- no major storm, no
# holiday. A baseline, not an interesting week. Inside the window above, so
# anything developed here runs unchanged over the full period.
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

# Copernicus Data Space. Only needed to DOWNLOAD Sentinel-1; the catalogue
# search is anonymous, which is why coverage can be checked before you have
# an account.
CDSE_USERNAME = os.getenv("CDSE_USERNAME", "")
CDSE_PASSWORD = os.getenv("CDSE_PASSWORD", "")

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


def box_area_sqdeg(box: tuple[float, float, float, float]) -> float:
    """Area in square degrees -- the unit OpenSky actually bills in.

    Note this is NOT box_area_km2 rescaled: OpenSky does not apply a cosine
    correction, so a box near the pole costs the same as one at the equator.
    Billing units and physical units are different things and conflating them
    would silently misprice every northern box.
    """
    lomin, lamin, lomax, lamax = box
    return abs(lomax - lomin) * abs(lamax - lamin)


def opensky_credits(box: tuple[float, float, float, float]) -> int:
    """Credits one /states/all request over this box costs.

    OpenSky's published tiers. Four coarse steps by area, with no relation to
    how many aircraft come back -- which is why widening from a metro to a
    continent costs 4x rather than the several-hundred-x the area ratio
    implies.

            <=  25 sq deg   1
          25 - 100 sq deg   2
         100 - 400 sq deg   3
            >  400 sq deg   4   (also the price of the global endpoint)

    Daily allowance: 400 anonymous, 4,000 registered, 8,000 for active feeders
    (>=30% uptime over a month), 14,400 licensed. Refills hourly. /states,
    /tracks and /flights draw on separate buckets. Exhausting one returns 429.
    """
    a = box_area_sqdeg(box)
    if a <= 25:
        return 1
    if a <= 100:
        return 2
    if a <= 400:
        return 3
    return 4


def daily_credits(box: tuple[float, float, float, float],
                  interval_s: float) -> int:
    """What a continuous collector over this box costs per day.

    Call this BEFORE changing an interval or widening a box. The failure mode
    otherwise is silent: you exhaust the bucket partway through the afternoon,
    every later poll 429s, and the archive simply has a hole in it every day
    at roughly the same time -- which looks like a diurnal pattern and is not.
    """
    return int(86400 / max(interval_s, 1.0)) * opensky_credits(box)
