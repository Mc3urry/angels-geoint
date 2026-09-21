"""Live positions, per domain.

    GET /live?domain=air         aircraft, ADS-B via OpenSky
    GET /live?domain=sea         vessels, AIS via aisstream.io
    GET /live?domain=sea&subtypes=cargo,tanker

Both go to the live source rather than the archive, so both are current to
within seconds. The archive lags by the poller's flush interval, which is
right for analysis and wrong for a map you are watching.

EVERYTHING HERE IS COOPERATIVE REPORTING, AND THE ENDPOINT SAYS SO

Every aircraft and every vessel on this route is one that CHOSE to broadcast.
Nothing served here can establish that anything was absent. The dark-vessel
product comes from SAR, which is retrospective by construction -- hours to
days behind, twelve days between revisits -- and it does not and cannot appear
on a live map. So every payload carries `evidence: "cooperative"`, and the
front end keeps the two apart rather than blending them into one "radar".

THE TWO FEEDS ARE OPPOSITE SHAPES AND THE PAYLOAD DOES NOT HIDE IT

    air   PULL, hard quota. An authenticated OpenSky account gets roughly
          4,000 credits a day and a browser polling every 10 s is 8,640
          requests -- over budget on its own, with the ingest poller spending
          from the same pot. So responses are cached server-side and every
          tab shares one upstream call. Nothing is lost by not asking: a
          request returns the current state of everything.

    sea   PUSH, no quota, but a COLD START. aisstream.io delivers each
          message once and anything broadcast while the socket was shut is
          gone. The server holds the socket open and maintains a table; this
          route reads the table. A table four seconds old holds almost
          nothing, and a map drawn from it looks like an empty sea -- so the
          sea payload carries `warming` and `listening_s`, and a client that
          renders a count without checking them is reporting a cold start as
          an absence.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query

from angels.adapters.aviation import opensky
from angels.adapters.aviation.opensky import TokenManager
from angels.adapters.maritime import aisstream
from angels.config import AOI_AIR, AOI_SEA, LIVE

router = APIRouter(tags=["live"])

# THE AIR VIEW READS THE COLLECTOR, AND ASKS OPENSKY ONLY AS A LAST RESORT.
#
# It used to ask OpenSky itself every 8 s from the same 4,000-credit daily
# budget the collectors live on. On 2026-09-19 a tab left open overnight
# spent ~2,600 credits, the budget ran out at 04:20 local, and both
# collectors were refused on every poll until 13:08 -- 8 h 48 min, 979
# refused polls on the DC box alone, of archive that cannot be backfilled.
# The viewer is the one consumer that can always wait; the archive is the one
# that cannot.
#
#   1. The collector's latest poll, if it is under SNAPSHOT_MAX_AGE_S old.
#      Zero credits. This is the normal path whenever the collector runs.
#   2. Otherwise OpenSky directly, cached CACHE_S so that any number of tabs
#      costs one request every thirty seconds -- the collector's own rate.
#   3. After a 429, nothing at all until OpenSky's stated retry time has
#      passed. Asking again only confirms the budget is gone.
SNAPSHOT_MAX_AGE_S = 90.0      # three missed collector polls
CACHE_S = 30.0
_blocked_until = 0.0

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_tokens: TokenManager | None = None


def _token_manager() -> TokenManager:
    global _tokens
    if _tokens is None:
        _tokens = TokenManager()      # raises if credentials are missing
    return _tokens


def _feature(row: list[Any]) -> dict[str, Any] | None:
    lat, lon = row[opensky.LATITUDE], row[opensky.LONGITUDE]
    if lat is None or lon is None:
        return None

    alt = row[opensky.GEO_ALTITUDE]
    if alt is None:
        alt = row[opensky.BARO_ALTITUDE]

    callsign = (row[opensky.CALLSIGN] or "").strip()

    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
        "properties": {
            "icao24": row[opensky.ICAO24],
            "callsign": callsign or row[opensky.ICAO24].upper(),
            "country": row[opensky.ORIGIN_COUNTRY],
            # heading and speed are what let the browser dead-reckon between
            # polls, which is the difference between icons that jump and
            # icons that glide
            "heading": row[opensky.TRUE_TRACK],
            "speed_mps": row[opensky.VELOCITY],
            "alt_m": alt,
            "vertical_rate": row[opensky.VERTICAL_RATE],
            "on_ground": bool(row[opensky.ON_GROUND]),
            "squawk": row[opensky.SQUAWK],
            "t": row[opensky.LAST_CONTACT],
        },
    }


_sea: aisstream.Stream | None = None


def sea_stream() -> aisstream.Stream:
    """The one long-lived socket, created on first use.

    Not started at import. A module-level socket would make importing the app
    -- which the test suite does -- open a network connection, and the suite
    is forbidden from reaching the network at all.
    """
    global _sea
    if _sea is None:
        _sea = aisstream.Stream(AOI_SEA)
    return _sea


@router.get("/live")
async def get_live(
    domain: Literal["air", "sea"] = "air",
    include_on_ground: bool = False,
    subtypes: str | None = Query(
        None, description="comma-separated sea subtypes, e.g. cargo,tanker"),
) -> dict[str, Any]:
    if domain == "sea":
        return await _live_sea(subtypes)

    global _blocked_until
    source = "collector"
    snap = opensky.read_snapshot(LIVE / "aviation.json",
                                 max_age_s=SNAPSHOT_MAX_AGE_S, bbox=AOI_AIR)
    if snap is not None:
        t = datetime.fromtimestamp(snap["time"], tz=UTC)
        rows = snap.get("states") or []
        snapshot_age = round(snap["age_s"], 1)
        remaining = snap.get("credits_remaining")
    else:
        source, snapshot_age = "direct", None
        now = time.time()
        if now < _blocked_until:
            raise HTTPException(429, _budget_detail(_blocked_until - now))

        key = f"{domain}:{include_on_ground}"
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_S:
            cached = dict(hit[1])
            cached["properties"] = {**cached["properties"], "cached": True}
            return cached

        try:
            t, rows = opensky.fetch_states(AOI_AIR, _token_manager())
        except opensky.RateLimited as exc:
            # Checked BEFORE RuntimeError, which RateLimited subclasses and
            # which this route otherwise maps to "credentials missing" --
            # the second wrong diagnosis of the same event.
            wait = exc.retry_after_s or 900.0
            _blocked_until = now + wait
            raise HTTPException(429, _budget_detail(wait)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, f"opensky unreachable: {exc}") from exc
        remaining = opensky.LAST_BUDGET["remaining"]

    feats = []
    for row in rows:
        if not include_on_ground and row[opensky.ON_GROUND]:
            continue
        f = _feature(row)
        if f is not None:
            feats.append(f)

    payload = {
        "type": "FeatureCollection",
        "features": feats,
        "properties": {
            "domain": domain,
            "evidence": "cooperative",
            "bbox": AOI_AIR,
            "server_time": t.isoformat(),
            "server_time_unix": int(t.timestamp()),
            "n": len(feats),
            "cache_s": CACHE_S,
            "cached": False,
            # Where these positions came from and what they cost. "collector"
            # is free and is the normal case; "direct" spends a credit per
            # CACHE_S and means the collector is not running or is stale.
            "source": source,
            "snapshot_age_s": snapshot_age,
            "credits_remaining": remaining,
            # The air domain has NO independent observation channel available
            # to civilians. Six sources were tested and none returned MLAT or
            # any other non-cooperative position; that is a finding of this
            # project, not an omission from this endpoint. It travels in the
            # payload so the front end can say so rather than leaving a blank
            # panel that reads as "not built yet".
            "observation": None,
            "observation_note": (
                "No independent observation channel for aircraft is available "
                "to civilians. Six sources were tested and none returned "
                "MLAT. Anomalies in this domain are found INSIDE the "
                "cooperative record -- transponder gaps, identity changes, "
                "orbits -- not by subtracting an observation from it."),
        },
    }
    if source == "direct":
        _cache[f"{domain}:{include_on_ground}"] = (time.time(), payload)
    return payload


def _budget_detail(wait_s: float) -> str:
    return (
        "OpenSky's daily credits are spent. This is not an outage and not an "
        "empty sky: OpenSky is answering, and the answer is 'no more "
        f"requests today'. They return in about {wait_s / 3600:.1f} h. The "
        "collectors share this budget, so the aircraft archive has a gap "
        "until then -- it is recorded in the collector's uptime log.")


async def _live_sea(subtypes: str | None) -> dict[str, Any]:
    stream = sea_stream()
    try:
        await stream.start()
    except aisstream.MissingCredentials as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:                          # noqa: BLE001
        raise HTTPException(502, f"AIS stream unavailable: {exc}") from exc

    wanted = tuple(s.strip() for s in subtypes.split(",") if s.strip()) \
        if subtypes else None
    snap = stream.snapshot(wanted)

    return {
        "type": "FeatureCollection",
        "features": snap.features,
        "properties": {
            "domain": "sea",
            "evidence": "cooperative",
            "bbox": list(AOI_SEA),
            "server_time_unix": int(time.time()),
            "n": len(snap.features),
            # THE FIELDS THAT STOP A COLD START READING AS AN EMPTY SEA.
            # A client that draws `n` without consulting these is making the
            # exact claim this project refuses to make.
            "warming": snap.warming,
            "listening_s": snap.listening_s,
            "connected": snap.connected,
            "n_messages": snap.n_messages,
            "last_message_age_s": snap.last_message_age_s,
            "stream_error": snap.error,
            # The measurement `warming` is a threshold on. Published so the
            # front end can show the decay rather than a progress bar against
            # a constant -- the constant was measured and found wrong by a
            # factor of several on the first real run of this box.
            "discovery_per_min": snap.discovery_per_min,
            "peak_discovery_per_min": snap.peak_discovery_per_min,
            # How many of them actually identified themselves during this
            # connection. NOT how many have a name: aisstream enriches every
            # message with a ShipName from its own database, so a name is
            # free and a type is not.
            "n_typed": snap.n_typed,
            "n_heard_static": snap.n_heard_static,
            # Which halves of the static reports actually arrived. Without
            # this, a large 'unknown' bucket cannot be read: it is either
            # vessels that never identified themselves or a parser dropping
            # what they sent, and only these counts tell them apart.
            "static_parts": snap.static_parts,
            "by_subtype": snap.by_subtype,
            "subtypes": list(aisstream.SUBTYPES),
            # Unlike air, this domain DOES have an independent channel -- but
            # it is retrospective and it is not on this route. Naming it here
            # is what keeps "no AIS here right now" from being mistaken for
            # "a dark vessel", which is a claim only the SAR path can support.
            "observation": "sentinel1-sar",
            "observation_note": (
                "Independent observation of vessels exists (Sentinel-1 SAR) "
                "but is retrospective: hours to days of latency and twelve "
                "days between revisits. A vessel absent from this live feed "
                "is not a dark vessel. That finding comes from the SAR path, "
                "against a searched-water denominator, after the fact."),
        },
    }
