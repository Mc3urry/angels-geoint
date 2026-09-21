"""Real-time AIS over a WebSocket. The live sea feed.

    from angels.adapters.maritime import aisstream
    stream = aisstream.Stream(bbox=AOI_SEA)
    await stream.start()
    stream.snapshot()          # every vessel heard, with its age

THIS FEED IS THE OPPOSITE SHAPE FROM THE AIR ONE, AND THE DIFFERENCE MATTERS

OpenSky is PULL with a hard quota: the server asks, pays a credit, and caches
the answer for six seconds so that fifty browser tabs cost one request. Nothing
is missed by not asking, because asking returns the current state of everything.

aisstream.io is PUSH with no quota: vessels transmit when they transmit, the
socket delivers each message once, and **anything broadcast while the socket
was closed is gone**. There is no backfill and no "current state" endpoint. So
the server holds the socket open permanently and maintains a table; the HTTP
endpoint reads the table and never reaches upstream.

Two consequences that the API must not paper over:

  A NEWLY CONNECTED STREAM HAS HEARD ALMOST NOTHING. Three seconds after
  connecting, the table holds the handful of vessels that happened to transmit
  in those three seconds -- perhaps five of four hundred. A map drawn from it
  looks like an empty sea. This is the project's standing rule in its most
  literal form: an error, or a cold start, must never be reported as an
  absence. Every snapshot carries `listening_s` and `warming`, and the front
  end is required to say "still listening" rather than draw a number.

  SILENCE IS AMBIGUOUS AND STAYS AMBIGUOUS. A vessel that stops appearing has
  either left the box, switched off, or simply not transmitted yet -- Class A
  reports every 2-10 s under way but only every 3 minutes at anchor, and
  Class B every 30 s to 3 minutes. So vessels are aged, not deleted, and the
  age travels with them. Deciding that a 4-minute silence means something is
  an analysis question, not a transport one, and it is not decided here.

WHY THIS SOURCE

It is free, it needs only an email to register, it filters server-side by
bounding box, and it is not the same organisation that supplies the historical
store -- so the live feed and the archive fail independently. Its own
documentation offers no SLA, which is the honest position for a free service
and is recorded here so that an outage is read as an outage.

WHAT IT IS NOT

It is not independent observation. Every vessel here is one that CHOSE to
broadcast, exactly like the aircraft on the air layer. Nothing on this feed
can establish that a vessel was absent, and a dark vessel cannot be found in
it -- that needs the SAR channel, which is retrospective by construction and
lands hours to days later. The live map and the discrepancy product are
different claims about different evidence, and the front end has to keep them
apart.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

ENDPOINT = "wss://stream.aisstream.io/v0/stream"

WARMUP_S = 180.0
"""FLOOR, not the answer. The table is incomplete for at least this long.

The first version set this to 120 s by reasoning from AIS reporting intervals
-- a Class A vessel at anchor transmits every 3 minutes, so a table younger
than that has provably not heard the stationary traffic. Then it was measured
over the real box, and the reasoning turned out to be right about the
direction and badly wrong about the size:

    t=5s     5 vessels        t=60s    109 vessels
    t=30s   58 vessels        t=120s   225 vessels

New vessels were still arriving at 123/min in the last quarter of the window,
against 129/min in the first. That is not a curve flattening out. It is a
straight line -- at two minutes the stream had not begun to converge, and the
historical store suggests the true population of this box is five to ten
times what had been heard.

So a fixed constant cannot answer the question, because how long convergence
takes depends on the box, the hour and the mix of Class A to Class B. Three
minutes is kept as a floor with a reason behind it, and the actual decision is
made by measuring the discovery rate -- see `settled` below."""

SETTLED_FRACTION = 0.3
"""The table is called settled once new vessels are arriving at less than
this fraction of the fastest rate seen on this connection.

Measuring the thing itself rather than a proxy for it. In a bounded box the
discovery rate decays as the resident population is heard, then levels off at
a low baseline of genuine new entrants; the shape of that decay, not a
stopwatch, is what says the table is worth reading."""

DISCOVERY_WINDOW_S = 60.0
"""How far back the discovery rate looks. Long enough that one quiet minute
does not declare victory, short enough to notice when the decay happens."""

STALE_S = 360.0
"""A vessel unheard this long is shown faded rather than moved or removed.

Six minutes is two missed Class A anchor reports. Shorter and a normally
behaving anchored vessel flickers; much longer and a vessel that has genuinely
left the box sits on the map pretending to be there."""

DROP_S = 3600.0
"""And this long, forgotten entirely -- only so the table cannot grow without
bound over a week of uptime. It is a memory bound, not a claim about the
vessel, and nothing downstream should read it as one."""

SUBSCRIBE_DEADLINE_S = 3.0
"""The service closes the socket if no subscription arrives within three
seconds of connecting."""

# -- AIS ship type -> the categories a filter can offer --------------------
#
# From ITU-R M.1371 message 5. The first digit carries the class and the
# second a cargo qualifier that no filter needs, so this maps ranges.
#
# 'government' deliberately groups military and law enforcement. Both are
# state platforms operating lawfully, both broadcast irregularly by design,
# and a meaningful share of any dark-vessel count off Norfolk will be the US
# Navy going about entirely normal business. Making them a visible category
# rather than an invisible confounder is the point -- config.py already notes
# that Navy operating areas must be masked and the masking declared.
_TYPE_BANDS: tuple[tuple[int, int, str], ...] = (
    (20, 29, "highspeed"),      # wing-in-ground
    (30, 30, "fishing"),
    (31, 32, "tug"),            # towing
    (33, 34, "service"),        # dredging, diving
    (35, 35, "government"),     # military
    (36, 37, "pleasure"),       # sailing, pleasure craft
    (40, 49, "highspeed"),
    (50, 51, "service"),        # pilot, search and rescue
    (52, 53, "tug"),            # tug, port tender
    (54, 54, "service"),        # anti-pollution
    (55, 55, "government"),     # law enforcement
    (58, 59, "service"),        # medical, non-combatant
    (60, 69, "passenger"),
    (70, 79, "cargo"),
    (80, 89, "tanker"),
)

SUBTYPES = ("cargo", "tanker", "passenger", "fishing", "tug",
            "pleasure", "highspeed", "service", "government", "other",
            "unknown")


def subtype(code: int | None) -> str:
    """AIS ship-and-cargo type to a filter category.

    'unknown' and 'other' are DIFFERENT CATEGORIES, and the first version
    merged them. 'other' is a vessel that declared itself -- type 90-99, or a
    code outside every band. 'unknown' is a vessel that has not told us: no
    static data yet on this connection, only the name half of a Class B
    report, or the AIS default of 0, which the standard defines as "not
    available". On a ten-minute run of the real box, 419 of 511 vessels came
    out as 'other' -- a single chip dwarfing every real category, presented
    to a user as though four hundred vessels had declared an unusual type.
    Almost all of them had declared nothing.
    """
    if code is None or code == 0:
        return "unknown"
    for lo, hi, name in _TYPE_BANDS:
        if lo <= code <= hi:
            return name
    return "other"


class StreamError(RuntimeError):
    """The stream could not be established, or died and could not recover."""


class MissingCredentials(StreamError):
    """No API key. Distinct from a stream that ran and found nothing."""


@dataclass
class Vessel:
    """One vessel's merged state.

    Position and identity arrive in SEPARATE MESSAGES and at wildly different
    rates: a position report every few seconds, static data (name, type,
    dimensions) every six minutes. So a vessel is known by position long
    before it is known by name, and the name must not be waited for -- a
    freshly seen vessel renders as its MMSI and acquires a name later.
    """

    mmsi: str
    lat: float = 0.0
    lon: float = 0.0
    sog_kn: float | None = None
    cog_deg: float | None = None
    heading_deg: float | None = None
    nav_status: int | None = None
    name: str | None = None
    call_sign: str | None = None
    ship_type: int | None = None
    length_m: float | None = None
    width_m: float | None = None
    draught_m: float | None = None
    destination: str | None = None
    ais_class: str = "A"
    t_position: float = 0.0        # monotonic-ish wall clock of last position
    t_static: float = 0.0
    first_seen: float = 0.0
    n_positions: int = 0

    @property
    def typed(self) -> bool:
        """Do we know what KIND of vessel it is?

        Wrong twice before it was right, and both times in the flattering
        direction:

          1. "has a name". aisstream enriches every message's MetaData with a
             ShipName from its own database, so a name arrives free with the
             first position report. The probe reported "215 of 225 had sent
             static data" for a window in which almost none had.

          2. "received any static message". Better, still wrong. A Class B
             vessel's message 24 comes in two halves sent separately -- part
             A is only the name, part B carries the type -- so a vessel that
             has sent part A has sent static data and still not said what it
             is. On the ten-minute run, 276 vessels were counted as typed and
             only 92 of them had a type that mapped to anything.

        So: a type code we actually hold, other than the AIS default of 0,
        which the standard defines as "not available".
        """
        return self.ship_type is not None and self.ship_type != 0

    @property
    def heard_static(self) -> bool:
        """Any static message at all on this connection, typed or not."""
        return self.t_static > 0.0

    @property
    def subtype(self) -> str:
        return subtype(self.ship_type)

    def to_feature(self, now: float) -> dict[str, Any]:
        age = now - self.t_position
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [self.lon, self.lat]},
            "properties": {
                "domain": "sea",
                "id": self.mmsi,
                "mmsi": self.mmsi,
                # Falls back to the MMSI rather than to blank or to "Unknown".
                # A vessel heard once but not yet identified is a real vessel;
                # rendering it as "Unknown" invites reading it as suspicious,
                # which is precisely the inference this project exists to
                # discipline.
                "label": self.name or self.mmsi,
                "name": self.name,
                "call_sign": self.call_sign,
                "subtype": self.subtype,
                "ship_type": self.ship_type,
                "ais_class": self.ais_class,
                "nav_status": self.nav_status,
                "length_m": self.length_m,
                "width_m": self.width_m,
                "draught_m": self.draught_m,
                "destination": self.destination,
                # Named to match the air layer so one dead-reckoner serves
                # both: the browser extrapolates from heading and speed
                # without caring which domain a feature came from.
                "heading": self.cog_deg if self.cog_deg is not None
                           else self.heading_deg,
                "speed_mps": None if self.sog_kn is None
                             else self.sog_kn * 0.514444,
                "sog_kn": self.sog_kn,
                "t": self.t_position,
                "age_s": round(age, 1),
                "stale": age > STALE_S,
                "n_positions": self.n_positions,
            },
        }


@dataclass
class Snapshot:
    """What the table holds, and how much it is worth.

    `warming` and `listening_s` are not diagnostics bolted on the side. They
    are the difference between "no vessels in this box" and "this socket has
    been open for four seconds", and a payload that carried only a count would
    make those two indistinguishable at exactly the moment a user is most
    likely to look.
    """

    features: list[dict[str, Any]]
    listening_s: float
    connected: bool
    warming: bool
    n_messages: int
    last_message_age_s: float | None
    error: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    by_subtype: dict[str, int] = field(default_factory=dict)
    # How fast previously unheard vessels are still turning up, and the
    # fastest it ever was on this connection. Their ratio is what decides
    # `warming`, and both are published so a reader can watch the decay
    # instead of trusting a flag.
    discovery_per_min: float = 0.0
    peak_discovery_per_min: float = 0.0
    n_typed: int = 0
    n_heard_static: int = 0
    static_parts: dict[str, int] = field(default_factory=dict)


class Stream:
    """Holds the socket open and maintains the vessel table.

    One instance per process. Reconnects with backoff on its own, because the
    alternative -- surfacing every transient socket close to the browser -- is
    a map that reports an outage for a blip the library would have healed.
    A failure that OUTLASTS the backoff does surface, in `Snapshot.error`.
    """

    def __init__(self, bbox: tuple[float, float, float, float],
                 api_key: str | None = None) -> None:
        self.bbox = bbox
        self._key = api_key or os.environ.get("AISSTREAM_API_KEY") or ""
        self._vessels: dict[str, Vessel] = {}
        self._task: asyncio.Task | None = None
        self._started_at: float | None = None
        self._connected = False
        self._n_messages = 0
        self._last_message: float | None = None
        self._error: str | None = None
        # When each MMSI was first heard, kept only long enough to compute a
        # discovery rate. Trimmed on read rather than on write so that a
        # burst of arrivals does no extra work on the socket's hot path.
        self._discovered: list[float] = []
        self._peak_rate = 0.0
        # Which static messages actually arrived. Kept because the type
        # histogram cannot be read without it: a pile of 'unknown' is either
        # vessels that never identified themselves or a parser that is
        # dropping what they sent, and these counts are the only way to tell
        # those apart from outside.
        self._static_parts: dict[str, int] = {"msg5": 0, "A": 0, "B": 0,
                                              "neither": 0}

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if not self._key:
            raise MissingCredentials(
                "AISSTREAM_API_KEY is not set. Register free at "
                "https://aisstream.io and put the key in .env, then restart "
                "the server -- .env is read at startup, not per request.")
        self._started_at = time.time()
        self._task = asyncio.create_task(self._run(), name="aisstream")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        self._connected = False

    # -- the socket --------------------------------------------------------

    def _subscription(self) -> str:
        w, s, e, n = self.bbox
        return json.dumps({
            "APIKey": self._key,
            # The service wants [[[lat, lon], [lat, lon]]] -- LATITUDE FIRST,
            # which is the opposite order from the (lon, lat) this project
            # uses everywhere else and from GeoJSON. Getting it backwards does
            # not raise; it subscribes to a box in the Indian Ocean and
            # delivers an empty stream that looks exactly like a quiet sea.
            "BoundingBoxes": [[[s, w], [n, e]]],
            # StaticDataReport IS NOT OPTIONAL, and leaving it out was a bug.
            #
            # ShipStaticData is AIS message 5, which only CLASS A transmits.
            # Class B identifies itself with message 24, StaticDataReport,
            # and on the first real run of this box 127 of 225 vessels were
            # Class B. Without message 24 they can never acquire a ship type,
            # so they all fall into the 'other' bucket -- which is exactly
            # what happened: 215 of 225 came out as 'other', against 4 cargo
            # and 1 tanker in the Chesapeake approaches. A type histogram
            # that implausible is the subscription's fault, not the sea's.
            "FilterMessageTypes": ["PositionReport",
                                   "StandardClassBPositionReport",
                                   "ExtendedClassBPositionReport",
                                   "ShipStaticData",
                                   "StaticDataReport"],
        })

    async def _run(self) -> None:
        import websockets

        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                        ENDPOINT, ping_interval=20, max_size=2 ** 20) as ws:
                    await asyncio.wait_for(ws.send(self._subscription()),
                                           timeout=SUBSCRIBE_DEADLINE_S)
                    self._connected = True
                    self._error = None
                    backoff = 1.0
                    async for raw in ws:
                        self._ingest(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:                  # noqa: BLE001
                self._connected = False
                self._error = f"{type(exc).__name__}: {exc}"
            # Backoff caps at a minute. Faster would hammer a free service
            # during an outage; slower would leave a recovered service unused
            # for longer than someone watching the map will wait.
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    # -- ingest ------------------------------------------------------------

    def _ingest(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        self.ingest_message(msg)

    def ingest_message(self, msg: dict[str, Any]) -> Vessel | None:
        """Fold one decoded message into the table. Public so it can be tested.

        Kept separate from the socket on purpose: everything interesting here
        is the merge, and a test that needs a live WebSocket to exercise a
        dictionary merge is a test nobody runs.
        """
        if not isinstance(msg, dict):
            return None
        kind = msg.get("MessageType")
        meta = msg.get("MetaData") or {}
        body = (msg.get("Message") or {}).get(kind) or {}

        mmsi = str(meta.get("MMSI") or body.get("UserID") or "").strip()
        if not mmsi or mmsi == "0":
            return None

        now = time.time()
        self._n_messages += 1
        self._last_message = now
        v = self._vessels.get(mmsi)
        if v is None:
            v = Vessel(mmsi=mmsi, first_seen=now)
            self._vessels[mmsi] = v
            self._discovered.append(now)

        if kind in ("ShipStaticData", "StaticDataReport"):
            # Message 5 (Class A) puts everything in one flat body. Message 24
            # (Class B) is split across two parts -- A carries the name, B the
            # type, call sign and dimensions -- and they arrive as separate
            # transmissions, so a Class B vessel is typed only once part B
            # turns up. Both shapes are read here rather than branching on
            # the message name, because a decoder that flattens part B into
            # the body is just as likely as one that nests it.
            # THE HALF THAT WAS NOT SENT IS STILL IN THE JSON. The decoder
            # emits both ReportA and ReportB on every message 24 and marks
            # the absent one Valid: false, filled with zeros -- ShipType 0,
            # CallSign "", Dimension {A:0, B:0, C:0, D:0}. Read without the
            # Valid flag, every name-only part A carries a "length" of 0 m,
            # which overwrites the real length a previous part B supplied and
            # shrinks the vessel to the smallest circle on the map.
            def usable(part):
                if not isinstance(part, dict):
                    return {}
                return part if part.get("Valid", True) else {}

            a_part = usable(body.get("ReportA"))
            b_part = usable(body.get("ReportB"))
            if kind == "StaticDataReport":
                self._static_parts["A" if a_part else
                                   "B" if b_part else "neither"] += 1
            else:
                self._static_parts["msg5"] += 1

            def pick(*keys):
                for src in (body, a_part, b_part):
                    for k in keys:
                        val = src.get(k)
                        if val not in (None, "", 0):
                            return val
                return None

            name = pick("Name", "ShipName")
            if name:
                v.name = str(name).strip() or v.name
            cs = pick("CallSign")
            if cs:
                v.call_sign = str(cs).strip() or v.call_sign
            st = pick("Type", "ShipType")
            if st is not None:
                v.ship_type = int(st)

            # Zero means "not available" in AIS dimensions, not zero metres.
            # A length is only believed when the two halves sum to something.
            dim = body.get("Dimension") or b_part.get("Dimension") or {}
            a, b = dim.get("A") or 0, dim.get("B") or 0
            c, d = dim.get("C") or 0, dim.get("D") or 0
            if a + b > 0:
                v.length_m = float(a) + float(b)
            if c + d > 0:
                v.width_m = float(c) + float(d)
            mx = body.get("MaximumStaticDraught")
            if mx:
                v.draught_m = float(mx)
            dest = (body.get("Destination") or "").strip()
            if dest:
                v.destination = dest
            v.t_static = now
            return v

        lat = body.get("Latitude", meta.get("latitude"))
        lon = body.get("Longitude", meta.get("longitude"))
        if lat is None or lon is None:
            return v
        # 91/181 are the AIS "not available" sentinels, and they are valid
        # numbers. Trusting them puts vessels at the north pole and on the
        # date line, where they are invisible rather than obviously wrong.
        if abs(float(lat)) > 90.0 or abs(float(lon)) > 180.0:
            return v

        v.lat = float(lat)
        v.lon = float(lon)
        v.t_position = now
        v.n_positions += 1
        if kind in ("StandardClassBPositionReport",
                    "ExtendedClassBPositionReport"):
            v.ais_class = "B"

        sog = body.get("Sog")
        # 102.3 kn is the AIS sentinel for "speed not available", and 1023 is
        # the raw form some decoders pass through. Both are plausible-looking
        # numbers that would be dead-reckoned into a vessel crossing the
        # Atlantic between two polls.
        if sog is not None and float(sog) < 102.0:
            v.sog_kn = float(sog)
        cog = body.get("Cog")
        if cog is not None and float(cog) < 360.0:
            v.cog_deg = float(cog)
        hdg = body.get("TrueHeading")
        if hdg is not None and int(hdg) < 511:
            v.heading_deg = float(hdg)
        st = body.get("NavigationalStatus")
        if st is not None and int(st) < 15:
            v.nav_status = int(st)
        name = (meta.get("ShipName") or "").strip()
        if name:
            v.name = name
        return v

    # -- read --------------------------------------------------------------

    def discovery_rate(self, now: float | None = None) -> tuple[float, float]:
        """New vessels per minute over the recent window, and the peak so far.

        The peak only starts being tracked once a full window has elapsed.
        Otherwise the very first partial window -- five vessels in five
        seconds -- sets a peak of 60/min that nothing later can fall to 30%
        of, and the stream declares itself settled on its second sample.
        """
        now = now or time.time()
        cutoff = now - DISCOVERY_WINDOW_S
        if len(self._discovered) > 4096:
            self._discovered = [t for t in self._discovered if t >= cutoff]
        recent = sum(1 for t in self._discovered if t >= cutoff)
        rate = recent * 60.0 / DISCOVERY_WINDOW_S

        listening = 0.0 if self._started_at is None else now - self._started_at
        if listening >= DISCOVERY_WINDOW_S:
            self._peak_rate = max(self._peak_rate, rate)
        return rate, self._peak_rate

    def snapshot(self, subtypes: tuple[str, ...] | None = None) -> Snapshot:
        now = time.time()
        listening = 0.0 if self._started_at is None else now - self._started_at

        feats = []
        counts: dict[str, int] = {}
        typed = heard = 0
        for mmsi, v in list(self._vessels.items()):
            if now - v.t_position > DROP_S:
                del self._vessels[mmsi]
                continue
            if v.t_position == 0.0:        # static data only, never located
                continue
            counts[v.subtype] = counts.get(v.subtype, 0) + 1
            if v.typed:
                typed += 1
            if v.heard_static:
                heard += 1
            if subtypes and v.subtype not in subtypes:
                continue
            feats.append(v.to_feature(now))

        rate, peak = self.discovery_rate(now)
        # Three ways to be incomplete, and all of them must set the flag:
        #
        #   not connected        a reconnecting stream is as empty as a cold
        #                        one, and the reader should not have to
        #                        combine two fields to discover that
        #   under the floor      three minutes is one Class A anchor
        #                        reporting interval; below it the stationary
        #                        traffic has provably not been heard
        #   still discovering    the measurement that replaced the guess.
        #                        The first real run of this box was still
        #                        finding 123 vessels a minute at t=120s,
        #                        against 129/min at the start -- a straight
        #                        line, not a curve flattening out. A
        #                        stopwatch would have called that ready.
        settled = peak > 0 and rate <= SETTLED_FRACTION * peak
        return Snapshot(
            features=feats,
            listening_s=round(listening, 1),
            connected=self._connected,
            warming=(not self._connected
                     or listening < WARMUP_S
                     or not settled),
            n_messages=self._n_messages,
            last_message_age_s=(None if self._last_message is None
                                else round(now - self._last_message, 1)),
            error=self._error,
            bbox=self.bbox,
            by_subtype=counts,
            discovery_per_min=round(rate, 1),
            peak_discovery_per_min=round(peak, 1),
            n_typed=typed,
            n_heard_static=heard,
            static_parts=dict(self._static_parts),
        )
