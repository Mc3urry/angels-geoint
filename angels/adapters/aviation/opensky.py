"""OpenSky ingest: ADS-B reports and (later) MLAT observations.

Two OpenSky things, easy to conflate:

  * API CLIENT -- self-serve, instant. OAuth2 client credentials, tokens expire
    after 30 minutes. Basic auth with username and password is no longer
    accepted. Gives live state vectors. This module uses it today.
  * HISTORICAL / TRINO -- an application with a human review step. State
    vectors back to 2013, plus the MLAT tables. Needed from Phase 2 on, and
    what `observations()` below will use once approved.

Filter on partition columns in every Trino query or they will suspend your
account. That is not a soft limit.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

import httpx

from angels.adapters.aviation.plausibility import AviationPlausibility
from angels.core.models import Domain, Observation, Position, Report, Track

log = logging.getLogger(__name__)

TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/"
    "protocol/openid-connect/token"
)
API_BASE = "https://opensky-network.org/api"

# --------------------------------------------------------------------------
# The state vector layout.
#
# /states/all returns a BARE ARRAY per aircraft, not an object. Get an index
# wrong and you produce plausible-looking garbage rather than an error -- swap
# 5 and 6 and every aircraft quietly appears in the wrong hemisphere. Index by
# these names, never by a literal.
# --------------------------------------------------------------------------
ICAO24         = 0   # str   -- lowercase hex transponder address
CALLSIGN       = 1   # str   -- 8 chars, space padded, may be blank
ORIGIN_COUNTRY = 2   # str
TIME_POSITION  = 3   # int   -- unix s, when the POSITION was determined
LAST_CONTACT   = 4   # int   -- unix s, last message of any kind
LONGITUDE      = 5   # float -- WGS84 degrees. NOTE: lon before lat.
LATITUDE       = 6   # float
BARO_ALTITUDE  = 7   # float -- metres
ON_GROUND      = 8   # bool
VELOCITY       = 9   # float -- m/s over ground
TRUE_TRACK     = 10  # float -- degrees clockwise from north
VERTICAL_RATE  = 11  # float -- m/s
SENSORS        = 12  # list[int] | None
GEO_ALTITUDE   = 13  # float -- metres
SQUAWK         = 14  # str | None
SPI            = 15  # bool
POSITION_SRC   = 16  # int   -- 0 ADS-B, 1 ASTERIX, 2 MLAT, 3 FLARM
CATEGORY       = 17  # int   -- only present with extended=1

# position_source values, from the API docs
SRC_ADSB, SRC_ASTERIX, SRC_MLAT, SRC_FLARM = 0, 1, 2, 3


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------

class TokenManager:
    """Holds an OAuth2 token and refreshes it before it expires.

    Tokens last 30 minutes. A long-running poller that fetches once and caches
    forever starts returning 401 half an hour in, which is an irritating thing
    to debug at 2am -- so refresh on a margin rather than on failure.
    """

    def __init__(self, client_id: str | None = None,
                 client_secret: str | None = None,
                 *, margin_s: float = 120.0,
                 client: httpx.Client | None = None) -> None:
        self.client_id = client_id or os.getenv("OPENSKY_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("OPENSKY_CLIENT_SECRET", "")
        if not self.client_id or not self.client_secret:
            raise RuntimeError(
                "OpenSky credentials missing. Put OPENSKY_CLIENT_ID and "
                "OPENSKY_CLIENT_SECRET in .env -- see .env.example."
            )
        self._margin_s = margin_s
        self._client = client or httpx.Client(timeout=30.0)
        self._token: str | None = None
        self._expires_at: float = 0.0

    def token(self) -> str:
        if self._token and time.time() < self._expires_at - self._margin_s:
            return self._token

        r = self._client.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        r.raise_for_status()
        payload = r.json()

        self._token = payload["access_token"]
        # expires_in is seconds. Default to the documented 30 min if absent.
        self._expires_at = time.time() + float(payload.get("expires_in", 1800))
        log.debug("opensky token refreshed, valid %.0fs",
                  self._expires_at - time.time())
        return self._token

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}


# --------------------------------------------------------------------------
# fetch and translate
# --------------------------------------------------------------------------

def fetch_states(bbox: tuple[float, float, float, float],
                 tokens: TokenManager,
                 *, client: httpx.Client | None = None,
                 extended: bool = False) -> tuple[datetime, list[list[Any]]]:
    """Raw state vectors over a bounding box.

    bbox is (min_lon, min_lat, max_lon, max_lat) -- GeoJSON order, matching
    angels.config.AOI. OpenSky wants them named separately, hence the unpack.

    Returns (server_time, rows). Kept raw on purpose: the ingest script writes
    these straight to Parquet so the archive holds exactly what the API said,
    and any bug in the translation below can be re-run against stored data
    instead of re-fetched.
    """
    lomin, lamin, lomax, lamax = bbox
    params: dict[str, Any] = {
        "lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax,
    }
    if extended:
        params["extended"] = 1

    c = client or httpx.Client(timeout=30.0)
    r = c.get(f"{API_BASE}/states/all", params=params, headers=tokens.headers())
    r.raise_for_status()
    payload = r.json()

    t = datetime.fromtimestamp(payload["time"], tz=timezone.utc)
    return t, (payload.get("states") or [])


def row_to_report(row: Sequence[Any], *,
                  uncertainty_m: float = 10.0) -> Report | None:
    """One state vector to a Report. None if it cannot be positioned.

    Rows are dropped when they have no lat/lon at all -- an aircraft in contact
    but without a position fix. Those are real and worth counting for coverage
    modelling later, but they are not a position, so they are not a Report.
    """
    lat, lon = row[LATITUDE], row[LONGITUDE]
    if lat is None or lon is None:
        return None

    # Prefer when the POSITION was determined over last radio contact. They
    # differ for an aircraft that is transmitting but whose GPS has gone stale,
    # and using last_contact there would place it where it no longer is.
    ts = row[TIME_POSITION] if row[TIME_POSITION] is not None else row[LAST_CONTACT]
    if ts is None:
        return None

    alt = row[GEO_ALTITUDE] if row[GEO_ALTITUDE] is not None else row[BARO_ALTITUDE]

    return Report(
        platform_id=row[ICAO24],
        position=Position(
            lat=float(lat),
            lon=float(lon),
            t=datetime.fromtimestamp(ts, tz=timezone.utc),
            uncertainty_m=uncertainty_m,
            speed_mps=row[VELOCITY],
            heading_deg=row[TRUE_TRACK],
            alt_m=alt,
        ),
        source="adsb",
    )


def rows_to_reports(rows: Iterable[Sequence[Any]], *,
                    include_on_ground: bool = False,
                    uncertainty_m: float = 10.0) -> list[Report]:
    """Translate a batch, dropping unpositioned rows.

    include_on_ground defaults to FALSE, and that default matters more than it
    looks. A parked airliner reports the same coordinates for hours. Feed those
    to loiter or orbit detection in Phase 2 and every gate at DCA becomes a
    high-confidence detection -- which is exactly the kind of false positive
    that makes a queue useless.

    Set it True when you specifically want ground movement.
    """
    out: list[Report] = []
    for row in rows:
        if not include_on_ground and row[ON_GROUND]:
            continue
        rep = row_to_report(row, uncertainty_m=uncertainty_m)
        if rep is not None:
            out.append(rep)
    return out


def split_into_tracks(reports: Iterable[Report], *,
                      max_gap_s: float = 900.0,
                      domain: Domain = "air") -> list[Track]:
    """Group reports by platform, splitting where the silence is too long.

    A single ICAO24 seen on Monday and again on Thursday is two flights, not
    one track with a three-day straight line through it. Splitting matters
    because Track.position_at will happily interpolate across any gap you leave
    in, inventing a path that never happened.

    max_gap_s is deliberately generous (15 min). The gap detector in Phase 2 is
    what decides whether a silence is *interesting*; this only decides whether
    two reports belong to the same flight. Do not conflate the two jobs.
    """
    by_id: dict[str, list[Report]] = {}
    for r in reports:
        by_id.setdefault(r.platform_id, []).append(r)

    tracks: list[Track] = []
    for pid, group in by_id.items():
        group.sort(key=lambda r: r.position.t)
        run: list[Report] = [group[0]]
        for prev, cur in zip(group, group[1:]):
            delta = (cur.position.t - prev.position.t).total_seconds()
            if delta > max_gap_s:
                tracks.append(Track(pid, domain, run))
                run = []
            run.append(cur)
        if run:
            tracks.append(Track(pid, domain, run))
    return tracks


# --------------------------------------------------------------------------
# the adapter
# --------------------------------------------------------------------------

@dataclass
class AviationAdapter:
    """Satisfies angels.adapters.base.Adapter."""

    domain: Domain = "air"
    plausibility: AviationPlausibility = field(default_factory=AviationPlausibility)
    tokens: TokenManager | None = None
    _client: httpx.Client | None = None

    def _tok(self) -> TokenManager:
        if self.tokens is None:
            self.tokens = TokenManager(client=self._client)
        return self.tokens

    def snapshot(self, bbox: tuple[float, float, float, float]) -> list[Report]:
        """Current reports over the box. One call, one instant."""
        _, rows = fetch_states(bbox, self._tok(), client=self._client)
        return rows_to_reports(rows)

    def tracks(self, t_start: datetime, t_end: datetime,
               bbox: tuple[float, float, float, float]) -> list[Track]:
        """Reports over a window, grouped into tracks.

        PHASE 1 reads the Parquet archive that scripts/ingest_aviation.py is
        accumulating -- the live endpoint only ever returns *now*, so a time
        window has to come from stored data.

        PHASE 2 swaps this for a Trino query against state_vectors_data4 once
        historical access is approved. The signature does not change, which is
        the point.
        """
        raise NotImplementedError(
            "Phase 1: read from data/raw parquet. "
            "Phase 2: Trino query on state_vectors_data4."
        )

    def observations(self, t_start: datetime, t_end: datetime,
                     bbox: tuple[float, float, float, float]
                     ) -> list[Observation]:
        """MLAT fixes -- positions derived from receiver geometry.

        This is the aviation half's independent sensing channel, and the reason
        aviation is so much cheaper than maritime: no detector to build, the
        observations arrive as positions already.

        PHASE 2, requires historical access. Note that live /states/all rows
        with position_source == SRC_MLAT are also multilaterated and could seed
        a crude version of this today.
        """
        raise NotImplementedError("Phase 2: requires OpenSky historical access.")
