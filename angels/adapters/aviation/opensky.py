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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from angels.adapters.aviation.plausibility import AviationPlausibility
from angels.config import RAW
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

class RateLimited(RuntimeError):
    """OpenSky answered 429: today's credits are spent.

    Its own exception because it is not an outage and must never be reported
    as one. On 2026-09-19 the viewer said "OpenSky is not responding" for
    hours while OpenSky was responding perfectly clearly -- with "you have no
    credits left". The two call for opposite actions: an outage is waited out,
    an exhausted budget is a spending problem on our side.
    """

    def __init__(self, retry_after_s: float | None, message: str) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


# The most recent credit count OpenSky reported. It sends the remainder of the
# day's budget on every successful response; keeping it lets a caller SEE the
# budget draining instead of learning about it from a 429.
LAST_BUDGET: dict[str, float | None] = {"remaining": None, "at": None}


def _header_float(r, name: str) -> float | None:
    try:
        v = r.headers.get(name)
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


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
    if r.status_code == 429:
        wait = _header_float(r, "X-Rate-Limit-Retry-After-Seconds")
        raise RateLimited(
            wait,
            "OpenSky daily credits are spent"
            + (f"; they return in about {wait / 3600:.1f} h" if wait else ""))
    r.raise_for_status()
    remaining = _header_float(r, "X-Rate-Limit-Remaining")
    if remaining is not None:
        LAST_BUDGET.update(remaining=remaining, at=time.time())
    payload = r.json()

    t = datetime.fromtimestamp(payload["time"], tz=UTC)
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
            t=datetime.fromtimestamp(ts, tz=UTC),
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
# reading back the archive
# --------------------------------------------------------------------------

def archive_row_to_report(d: Mapping[str, Any], *,
                          uncertainty_m: float = 10.0) -> Report | None:
    """One Parquet row (named columns) to a Report.

    The twin of row_to_report, which takes the API's bare array. These two MUST
    agree -- tests/test_opensky.py asserts they produce identical Reports for
    equivalent input, because a drift between them would mean live data and
    archived data silently disagree.
    """
    lat, lon = d.get("latitude"), d.get("longitude")
    if lat is None or lon is None:
        return None

    ts = d.get("time_position")
    if ts is None:
        ts = d.get("last_contact")
    if ts is None:
        return None

    alt = d.get("geo_altitude")
    if alt is None:
        alt = d.get("baro_altitude")

    return Report(
        platform_id=d["icao24"],
        position=Position(
            lat=float(lat),
            lon=float(lon),
            t=datetime.fromtimestamp(int(ts), tz=UTC),
            uncertainty_m=uncertainty_m,
            speed_mps=d.get("velocity"),
            heading_deg=d.get("true_track"),
            alt_m=alt,
        ),
        source="adsb",
    )


# The columns archive_row_to_report actually reads, plus position_source for
# provenance. Named explicitly rather than SELECT * for two reasons:
#
#   1. Parquet is columnar, so naming them lets DuckDB skip the rest entirely.
#      On a 400-million-row archive that is the difference between a query and
#      a wait.
#   2. It excludes `fetched_at`, the one TIMESTAMP WITH TIME ZONE column.
#      DuckDB needs pytz to hand a tz-aware timestamp back to Python, and
#      nothing here uses it -- every time in a Report comes from the integer
#      unix seconds in time_position or last_contact. Selecting it would drag
#      in a dependency to carry a value we throw away.
_REPORT_COLUMNS = (
    "icao24", "callsign", "time_position", "last_contact",
    "longitude", "latitude", "baro_altitude", "geo_altitude",
    "velocity", "true_track", "position_source",
)


class ArchiveReadError(RuntimeError):
    """The archive exists but could not be read.

    Distinct from "there is no archive yet", which is a normal state and
    returns an empty list. This one means something is actually wrong --
    a missing dependency, a corrupt Parquet file, a permissions problem --
    and must not be mistaken for an empty sky.
    """


def read_archive(root: Path, t_start: datetime, t_end: datetime,
                 bbox: tuple[float, float, float, float],
                 *, include_on_ground: bool = False,
                 limit: int | None = None,
                 dataset: str = "aviation") -> list[Report]:
    """Reports from the Parquet the ingest script has been writing.

    The live endpoint only ever returns *now*, so any time window has to come
    from stored data. DuckDB reads the hour-partitioned files directly and
    pushes the filters down, so a narrow window never scans the whole archive
    -- which is the entire reason for partitioning by hour.

    `dataset` selects which collector's archive to read: "aviation" for the
    30-second DC-Baltimore box, "aviation-conus" for the ten-minute national
    one. They are kept in separate directories rather than a single archive
    with a region column because their sample rates differ by twentyfold, and
    anything that computed a rate over the union of the two would be wrong
    without ever looking wrong.
    """
    import duckdb  # adapter-local: core must stay stdlib-only

    pattern = (root / dataset / "hour=*" / "*.parquet").as_posix()
    lomin, lamin, lomax, lamax = bbox

    where = [
        f"last_contact >= {int(t_start.timestamp())}",
        f"last_contact <= {int(t_end.timestamp())}",
        f"longitude BETWEEN {lomin} AND {lomax}",
        f"latitude BETWEEN {lamin} AND {lamax}",
        "latitude IS NOT NULL",
    ]
    if not include_on_ground:
        where.append("NOT on_ground")

    cols_sql = ", ".join(_REPORT_COLUMNS)
    sql = (f"SELECT {cols_sql} FROM read_parquet('{pattern}') "
           f"WHERE {' AND '.join(where)} ORDER BY icao24, last_contact")
    if limit:
        sql += f" LIMIT {int(limit)}"

    # An archive that does not exist yet is NOT an error -- the collector may
    # simply not have run. Decide that by LOOKING, before running the query,
    # rather than by catching whatever the query throws.
    #
    # WHY THAT DISTINCTION IS THE WHOLE POINT. This function used to wrap the
    # query in `except Exception: return []`, on the reasoning that an empty
    # archive is normal. It is -- but that handler also swallowed real
    # failures. When pandas turned out to be missing from a fresh environment,
    # every caller was told, calmly, that there were no aircraft. The map went
    # blank, run_detectors.py printed "No tracks in the archive. Is the
    # collector running?", and the collector was running perfectly.
    #
    # An error reported as an absence is the exact failure this project exists
    # to detect, appearing inside the project itself for the fourth time. A
    # real failure now raises.
    if not list(Path(root / dataset).glob("hour=*/*.parquet")):
        log.debug("no parquet under %s/%s yet", root, dataset)
        return []

    try:
        rel = duckdb.sql(sql)
        cols = [d[0] for d in rel.description]
        # fetchall() rather than .df(): DuckDB's DataFrame conversion requires
        # pandas, and nothing here needs a DataFrame -- the rows are turned
        # into dicts and then into Reports either way. Dropping it removes a
        # heavyweight dependency from the install and a whole class of
        # environment breakage with it.
        rows = [dict(zip(cols, r)) for r in rel.fetchall()]
    except Exception as exc:
        raise ArchiveReadError(
            f"could not read the {dataset} archive under {root}: {exc}"
        ) from exc

    out = [archive_row_to_report(r) for r in rows]
    return [r for r in out if r is not None]


def latest_states(root: Path, t_start: datetime, t_end: datetime,
                  bbox: tuple[float, float, float, float],
                  *, include_on_ground: bool = False) -> list[dict[str, Any]]:
    """The most recent fix for each aircraft in the window.

    Separate from read_archive because it answers a different question and is
    polled far more often. The map re-fetches positions every few seconds but
    track geometry only occasionally -- one row per aircraft rather than
    hundreds keeps that cheap.

    Carries callsign, heading and speed, which Track deliberately does not:
    callsign is an aviation concept and core.models stays domain-blind. The
    front end also needs heading and speed to dead-reckon between polls.
    """
    import duckdb

    pattern = (root / "aviation" / "hour=*" / "*.parquet").as_posix()
    lomin, lamin, lomax, lamax = bbox

    where = [
        f"last_contact >= {int(t_start.timestamp())}",
        f"last_contact <= {int(t_end.timestamp())}",
        f"longitude BETWEEN {lomin} AND {lomax}",
        f"latitude BETWEEN {lamin} AND {lamax}",
        "latitude IS NOT NULL",
    ]
    if not include_on_ground:
        where.append("NOT on_ground")

    # One row per aircraft: the newest. DISTINCT ON is a DuckDB nicety that
    # avoids a window function and a subquery.
    sql = f"""
        SELECT DISTINCT ON (icao24)
               icao24, callsign, latitude, longitude, last_contact,
               velocity, true_track, geo_altitude, baro_altitude,
               vertical_rate, squawk, origin_country
        FROM read_parquet('{pattern}')
        WHERE {' AND '.join(where)}
        ORDER BY icao24, last_contact DESC
    """
    try:
        return duckdb.sql(sql).df().to_dict("records")
    except Exception as exc:
        log.warning("latest_states failed (%s); returning nothing", exc)
        return []


# --------------------------------------------------------------------------
# the adapter
# --------------------------------------------------------------------------

@dataclass
class AviationAdapter:
    """Satisfies angels.adapters.base.Adapter."""

    domain: Domain = "air"
    plausibility: AviationPlausibility = field(default_factory=AviationPlausibility)
    tokens: TokenManager | None = None
    archive_root: Path = RAW
    dataset: str = "aviation"
    max_gap_s: float = 900.0
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

        Reads the Parquet archive that scripts/ingest_aviation.py accumulates.
        The live endpoint only ever returns *now*, so a time window has to come
        from stored data.

        PHASE 2 swaps the body for a Trino query against state_vectors_data4
        once historical access is approved. The signature does not change,
        which is the point.
        """
        reports = read_archive(self.archive_root, t_start, t_end, bbox,
                               dataset=self.dataset)
        return split_into_tracks(reports, max_gap_s=self.max_gap_s,
                                 domain=self.domain)

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


# --------------------------------------------------------------------------
# the live snapshot: one poll, shared
# --------------------------------------------------------------------------
#
# THE VIEWER MUST NOT SPEND CREDITS THE ARCHIVE NEEDS.
#
# The collector already asks OpenSky about the DC-Baltimore box every thirty
# seconds. The viewer used to ask again, separately, every eight -- from the
# same 4,000-credit daily budget. On 2026-09-19 a tab left open overnight
# spent roughly 2,600 credits, the budget ran out at 04:20 local time, and the
# collector was refused on every poll until 13:08 -- 8 h 48 min and 979
# refused polls of archive that cannot be backfilled, lost to a map nobody was
# looking at. (Recovery came at 13:08 local, not at midnight UTC, so do not
# assume a fixed daily reset: read X-Rate-Limit-Retry-After-Seconds.)
#
# So the collector writes what it just received to a small file, and the
# viewer reads that file. One poll, two consumers, zero extra credits.

def write_snapshot(path: Path, t: datetime, rows: list,
                   bbox: tuple[float, float, float, float]) -> bool:
    """Write the latest poll atomically. False if it could not be written.

    Written to a temporary name and swapped in, so a reader never sees half a
    file. On Windows the swap can fail if a reader has the file open at that
    instant; that costs one snapshot out of thousands and the next poll
    replaces it thirty seconds later, so the failure is swallowed rather than
    allowed to interrupt the collector.
    """
    import json
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "time": int(t.timestamp()),
            "written": time.time(),
            "bbox": list(bbox),
            "credits_remaining": LAST_BUDGET["remaining"],
            "states": rows,
        }), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def read_snapshot(path: Path, *, max_age_s: float,
                  bbox: tuple[float, float, float, float]) -> dict | None:
    """The latest collector poll, or None if there is no usable one.

    None covers every reason not to trust it -- missing, unreadable, too old,
    or for a different box -- because in every one of those cases the right
    move is the same: do not serve it. A snapshot older than max_age_s means
    the collector has stopped or is being refused, and serving it would show
    a sky frozen at the moment the archive broke.
    """
    import json
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # Valid JSON of the wrong shape is as untrustworthy as a torn write, and
    # must not reach the route as an exception -- that would turn a bad file
    # on disk into a 500 on the live map.
    if not isinstance(doc, dict):
        return None
    try:
        if [round(float(v), 4) for v in doc.get("bbox", [])] != \
                [round(v, 4) for v in bbox]:
            return None
        age = time.time() - float(doc.get("written", 0))
    except (TypeError, ValueError):
        return None
    if age > max_age_s:
        return None
    doc["age_s"] = age
    return doc
