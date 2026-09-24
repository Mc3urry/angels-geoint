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
from angels.config import AOI_SEA, AOI_SEA_CONUS, AOIS, LIVE

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

# THE NATIONAL BOX IS SNAPSHOT-ONLY. There is no step 2 for it.
#
# OpenSky prices a request by the AREA of the box: the DC box is 1 credit,
# the whole country is 4. The CONUS collector already polls it every ten
# minutes and writes the same snapshot file the DC collector does, so the
# viewer costs nothing -- but if that snapshot is missing or stale, falling
# back to a direct national fetch would spend FOUR credits per CACHE_S from
# the budget the archive lives on. The whole reason this route reads a file
# instead of the network is the night a left-open tab cost 8 h 48 min of
# archive, and a national tab would do it four times faster.
#
# So a stale national snapshot is an error with an explanation, never a
# purchase.
SNAPSHOT_ONLY = {"conus"}
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

SEA_BOXES = {"air": AOI_SEA, "conus": AOI_SEA_CONUS}


def _contains(outer, inner) -> bool:
    return (outer[0] <= inner[0] and outer[1] <= inner[1]
            and outer[2] >= inner[2] and outer[3] >= inner[3])


async def sea_stream(box=AOI_SEA) -> aisstream.Stream:
    """The one long-lived socket, created on first use, WIDENED on demand.

    Not started at import. A module-level socket would make importing the app
    -- which the test suite does -- open a network connection, and the suite
    is forbidden from reaching the network at all.

    WHY IT WIDENS AND NEVER NARROWS.

    Unlike OpenSky, aisstream has no quota, so a national box costs nothing
    to ask for. What it does cost is the TABLE: the subscription decides
    which messages arrive, so changing it throws away everything heard for
    the old box and starts the cold start again -- and a vessel at anchor
    reports only every three minutes, so "again" means minutes of a map that
    is honestly incomplete.

    Paying that once is reasonable; paying it every time someone clicks
    between DC and CONUS is not. So the socket only ever grows: ask for the
    national box and it resubscribes, ask for the Chesapeake afterwards and
    it simply filters the national table, which is a superset and needs no
    warm-up at all. The narrowing is done per request, below.
    """
    global _sea
    if _sea is not None and _contains(_sea.bbox, box):
        return _sea
    if _sea is not None:
        await _sea.stop()
    _sea = aisstream.Stream(box)
    return _sea


@router.get("/live")
async def get_live(
    domain: Literal["air", "sea"] = "air",
    aoi: Literal["air", "conus"] = "air",
    include_on_ground: bool = False,
    subtypes: str | None = Query(
        None, description="comma-separated sea subtypes, e.g. cargo,tanker"),
) -> dict[str, Any]:
    if domain == "sea":
        return await _live_sea(subtypes, aoi)

    global _blocked_until
    spec = AOIS[aoi]
    box = tuple(spec["box"])
    interval_s = float(spec["interval_s"])
    # Three missed polls, the same rule at either cadence: 90 s for the DC
    # box at 30 s, 1,800 s for the country at 10 min. A fixed number would
    # call the national snapshot stale on arrival.
    max_age_s = 3 * interval_s

    source = "collector"
    snap = opensky.read_snapshot(LIVE / f"{spec['dataset']}.json",
                                 max_age_s=max_age_s, bbox=box)
    if snap is None and aoi in SNAPSHOT_ONLY:
        raise HTTPException(503, (
            f"No fresh {spec['label']} snapshot. The CONUS collector writes "
            f"one every {interval_s / 60:.0f} minutes and nothing newer than "
            f"{max_age_s / 60:.0f} minutes is on disk, so it is not running "
            f"or is being refused. This box is NOT fetched directly: at four "
            f"credits a request it would spend the archive's budget four "
            f"times faster than the incident that made this route read a "
            f"file. Start it with: .\\collector.ps1 start -Aoi conus"))
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

        key = f"{domain}:{aoi}:{include_on_ground}"
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_S:
            cached = dict(hit[1])
            cached["properties"] = {**cached["properties"], "cached": True}
            return cached

        try:
            t, rows = opensky.fetch_states(box, _token_manager())
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
            "aoi": aoi,
            "label": spec["label"],
            "evidence": "cooperative",
            "bbox": list(box),
            "server_time": t.isoformat(),
            "server_time_unix": int(t.timestamp()),
            "n": len(feats),
            "cache_s": CACHE_S,
            "cached": False,
            # HOW OLD A FIX HERE CAN BE, AND THEREFORE WHAT MAY BE DRAWN.
            #
            # The DC box is polled every 30 s. Between polls an airliner
            # moves about 2 km, so projecting it forward from its heading and
            # speed is a fair approximation and the icons glide instead of
            # jumping.
            #
            # The national box is polled every 10 MINUTES. The same aircraft
            # moves 150 km in that time. Dead reckoning there does not
            # produce a slightly stale map, it produces a map of aircraft
            # that are provably not where they are drawn -- and it would look
            # exactly as confident as the 30 s one. So the SERVER states the
            # policy rather than leaving the client to infer it from the
            # cadence: below, `dead_reckon` is false for the national box,
            # and the client is expected to draw the last heard position with
            # an uncertainty that grows as speed x age.
            "poll_interval_s": interval_s,
            "dead_reckon": interval_s <= 60.0,
            "max_fix_age_s": max_age_s,
            "fix_age_note": (
                "Positions are projected from heading and speed between "
                f"polls {interval_s:.0f} s apart."
                if interval_s <= 60.0 else
                f"Polled every {interval_s / 60:.0f} minutes. A fix here can "
                f"be that old, and an airliner covers ~150 km in it. Draw "
                f"each aircraft where it was last HEARD, with a radius of "
                f"speed x age -- never a projected position, which would be "
                f"as confident as it is wrong."),
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
        # The key must match the one the lookup above builds, INCLUDING the
        # aoi. It did not for one commit, and the cache silently never hit:
        # every tab went upstream on every poll, which is the exact failure
        # this cache exists to prevent.
        _cache[f"{domain}:{aoi}:{include_on_ground}"] = (time.time(), payload)
    return payload


def _budget_detail(wait_s: float) -> str:
    return (
        "OpenSky's daily credits are spent. This is not an outage and not an "
        "empty sky: OpenSky is answering, and the answer is 'no more "
        f"requests today'. They return in about {wait_s / 3600:.1f} h. The "
        "collectors share this budget, so the aircraft archive has a gap "
        "until then -- it is recorded in the collector's uptime log.")


# The maritime collector's datasets, by box. Same "one listener, two
# consumers" arrangement as the aviation side, and here it buys something the
# air side never needed: the API's own socket starts COLD on every uvicorn
# restart, and a cold AIS table is indistinguishable from an empty sea for the
# three minutes an anchored vessel takes to report. A collector that has been
# listening for hours removes the cold start entirely.
SEA_DATASETS = {"air": "maritime-live", "conus": "maritime-live-conus"}

# Three missed snapshots at the collector's five-second cadence. Past this the
# collector has stopped or is being refused, and serving its last table would
# show a sea frozen at the moment collection broke.
SEA_SNAPSHOT_MAX_AGE_S = 20.0


def _sea_from_collector(aoi: str, box):
    """The collector's table, or None if there is no usable one.

    None covers every reason not to trust it -- missing, unreadable, stale,
    or for a different box -- because the response is the same in all of
    them: open our own socket instead.
    """
    import json

    path = LIVE / f"{SEA_DATASETS.get(aoi, '')}.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    age = time.time() - float(doc.get("written", 0))
    if age > SEA_SNAPSHOT_MAX_AGE_S:
        return None
    if tuple(doc.get("bbox") or ()) != tuple(box):
        return None
    doc["age_s"] = age
    return doc


async def _live_sea(subtypes: str | None, aoi: str = "air") -> dict[str, Any]:
    box = SEA_BOXES.get(aoi, AOI_SEA)

    collected = _sea_from_collector(aoi, box)
    if collected is not None:
        wanted = {s.strip() for s in subtypes.split(",") if s.strip()} \
            if subtypes else None
        feats = [f for f in collected.get("features", [])
                 if not wanted or f["properties"].get("subtype") in wanted]
        return _sea_payload(
            feats, box=box, aoi=aoi, source="collector",
            subscribed=box, n_in_subscription=len(collected.get("features", [])),
            by_subtype=collected.get("by_subtype", {}),
            snapshot_age_s=round(collected["age_s"], 1), meta=collected)

    stream = await sea_stream(box)
    try:
        await stream.start()
    except aisstream.MissingCredentials as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:                          # noqa: BLE001
        raise HTTPException(502, f"AIS stream unavailable: {exc}") from exc

    wanted = tuple(s.strip() for s in subtypes.split(",") if s.strip()) \
        if subtypes else None
    snap = stream.snapshot(wanted)

    # NARROWED HERE, not at the socket. The subscription is a superset once
    # anyone has asked for the country, so the small box is a filter over a
    # table that is already warm rather than a resubscription that would
    # throw it away. See sea_stream.
    feats = snap.features
    by_subtype = snap.by_subtype
    if not _contains(box, stream.bbox):
        lomin, lamin, lomax, lamax = box
        feats = [f for f in feats
                 if lomin <= f["geometry"]["coordinates"][0] <= lomax
                 and lamin <= f["geometry"]["coordinates"][1] <= lamax]
        # The per-type counts feed the legend, which sits beside the map, so
        # they must count the same water the map is showing. Left as the
        # subscription's totals they would say "412 cargo" over a Chesapeake
        # view holding sixty, and the legend would be quietly describing
        # California.
        by_subtype = {}
        for f in feats:
            k = f["properties"].get("subtype", "unknown")
            by_subtype[k] = by_subtype.get(k, 0) + 1

    return _sea_payload(
        feats, box=box, aoi=aoi, source="stream",
        subscribed=stream.bbox, n_in_subscription=len(snap.features),
        by_subtype=by_subtype, snapshot_age_s=None, snap=snap)


def _sea_payload(feats, *, box, aoi, source, subscribed, n_in_subscription,
                 by_subtype, snapshot_age_s, snap=None, meta=None):
    """One shape, whether the table came from the collector or our own socket.

    Built as a function rather than duplicated because the honesty fields --
    `warming`, `listening_s`, `connected` -- are the whole reason this route
    exists, and a second copy of the payload is a second place for one of
    them to be quietly dropped.
    """
    g = (lambda k, d=None: getattr(snap, k)) if snap is not None \
        else (lambda k, d=None: (meta or {}).get(k, d))

    return {
        "type": "FeatureCollection",
        "features": feats,
        "properties": {
            "domain": "sea",
            "aoi": aoi,
            "evidence": "cooperative",
            "bbox": list(box),
            "subscribed_bbox": list(subscribed),
            "n_in_subscription": n_in_subscription,
            "server_time_unix": int(time.time()),
            "n": len(feats),
            # Where the table came from, and what that costs. "collector" is
            # a permanently running listener whose table is already warm;
            # "stream" is this process's own socket, which starts cold on
            # every restart.
            "source": source,
            "snapshot_age_s": snapshot_age_s,
            # THE FIELDS THAT STOP A COLD START READING AS AN EMPTY SEA.
            # A client that draws `n` without consulting these is making the
            # exact claim this project refuses to make.
            "warming": g("warming", True),
            "listening_s": g("listening_s", 0.0),
            "connected": g("connected", False),
            "n_messages": g("n_messages", 0),
            "last_message_age_s": g("last_message_age_s"),
            "stream_error": (snap.error if snap is not None
                             else (meta or {}).get("error")),
            # The measurement `warming` is a threshold on. Published so the
            # front end can show the decay rather than a progress bar against
            # a constant -- the constant was measured and found wrong by a
            # factor of several on the first real run of this box.
            "discovery_per_min": g("discovery_per_min", 0.0),
            "peak_discovery_per_min": g("peak_discovery_per_min", 0.0),
            # How many of them actually identified themselves during this
            # connection. NOT how many have a name: aisstream enriches every
            # message with a ShipName from its own database, so a name is
            # free and a type is not.
            "n_typed": g("n_typed", 0),
            "n_heard_static": g("n_heard_static", 0),
            # Which halves of the static reports actually arrived. Without
            # this, a large 'unknown' bucket cannot be read: it is either
            # vessels that never identified themselves or a parser dropping
            # what they sent, and only these counts tell them apart.
            "static_parts": g("static_parts", {}),
            "by_subtype": by_subtype,
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
