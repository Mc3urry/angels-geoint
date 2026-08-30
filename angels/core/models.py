"""Core domain model.

The single most important idea in this file: a Report and an Observation are
DIFFERENT TYPES.

    Report      = what a platform SAYS about itself   (cooperative, defeatable)
    Observation = what a SENSOR SEES                  (independent, non-cooperative)

The whole project is the asymmetry between those two. If you ever find yourself
wanting to merge them into one generic "position record", stop and reread this.

Deliberately depends on nothing outside the standard library. The core is the
part that must stay portable and fast to test; heavier geospatial machinery
belongs in angels.analysis. The spherical-earth math below is accurate to
roughly 0.3% -- far inside your positional uncertainties. If you ever need
ellipsoidal precision here, swap in pyproj.Geod and keep the signatures.

Nothing in this module may import from angels.adapters.
Enforced by tests/test_architecture.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

EARTH_RADIUS_M = 6_371_008.8

Domain = Literal["air", "sea"]

EventKind = Literal[
    "gap",          # reporting silence beyond what coverage explains
    "kinematic",    # implied motion exceeds platform limits
    "identity",     # same id in two places, or id reuse
    "unmatched",    # a sensor saw something no report accounts for
    "loiter",       # low net displacement over a sustained window
    "orbit",        # repeated circling about a centroid
    "rendezvous",   # two tracks proximate and slow, simultaneously
]


# --------------------------------------------------------------------------
# geodesy (spherical)
# --------------------------------------------------------------------------

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, degrees clockwise from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def project(lat: float, lon: float, bearing_deg: float, dist_m: float
            ) -> tuple[float, float]:
    """Travel dist_m from (lat, lon) along bearing. Returns (lat, lon)."""
    d = dist_m / EARTH_RADIUS_M
    b = math.radians(bearing_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(
        math.sin(b) * math.sin(d) * math.cos(p1),
        math.cos(d) - math.sin(p1) * math.sin(p2),
    )
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def _utc(t: datetime, name: str) -> datetime:
    """All internal time is timezone-aware UTC. Convert at the edges, never here."""
    if t.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware UTC; got naive {t!r}")
    return t.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# the five types
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Position:
    """A point in space and time, always carrying its own uncertainty.

    uncertainty_m is NOT optional. Every confidence score downstream depends on
    it, and retrofitting error propagation into a codebase that assumed point
    estimates is genuinely miserable. Carry it from the start.
    """

    lat: float
    lon: float
    t: datetime
    uncertainty_m: float
    speed_mps: float | None = None
    heading_deg: float | None = None
    alt_m: float | None = None          # altitude (air) or draft (sea)

    def __post_init__(self) -> None:
        if not -90.0 <= self.lat <= 90.0:
            raise ValueError(f"lat out of range: {self.lat}")
        if not -180.0 <= self.lon <= 180.0:
            raise ValueError(f"lon out of range: {self.lon}")
        if self.uncertainty_m < 0:
            raise ValueError(f"uncertainty_m must be >= 0; got {self.uncertainty_m}")
        object.__setattr__(self, "t", _utc(self.t, "Position.t"))

    def distance_to(self, other: Position) -> float:
        return haversine_m(self.lat, self.lon, other.lat, other.lon)

    def bearing_to(self, other: Position) -> float:
        return initial_bearing_deg(self.lat, self.lon, other.lat, other.lon)

    def combined_uncertainty(self, other: Position) -> float:
        """Independent errors add in quadrature."""
        return math.hypot(self.uncertainty_m, other.uncertainty_m)

    def agrees_with(self, other: Position, k: float = 3.0) -> bool:
        """True if the two positions are consistent within k sigma."""
        return self.distance_to(other) <= k * self.combined_uncertainty(other)


@dataclass(frozen=True)
class Report:
    """What the platform claims about itself. ADS-B, AIS.

    Cooperative and unauthenticated: it can be switched off, spoofed, or given
    someone else's identity, and the receiving infrastructure will believe it.
    """

    platform_id: str            # ICAO24 hex (air) | MMSI (sea)
    position: Position
    source: str                 # "adsb" | "ais"


@dataclass(frozen=True)
class Observation:
    """What an independent sensor saw. MLAT, SAR.

    platform_id is usually None -- a sensor sees a thing, not an identity.
    That is precisely the point of the project.
    """

    position: Position
    sensor: str                                     # "mlat" | "sar"
    attributes: dict[str, Any] = field(default_factory=dict)
    platform_id: str | None = None


@dataclass
class Track:
    """A platform's reported history. Reports are kept time-ordered."""

    platform_id: str
    domain: Domain
    reports: list[Report] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.reports.sort(key=lambda r: r.position.t)

    def __len__(self) -> int:
        return len(self.reports)

    @property
    def t_start(self) -> datetime:
        return self.reports[0].position.t

    @property
    def t_end(self) -> datetime:
        return self.reports[-1].position.t

    @property
    def duration(self) -> timedelta:
        return self.t_end - self.t_start

    def intervals(self) -> list[tuple[datetime, datetime, float]]:
        """Every inter-report interval as (start, end, seconds).

        Raw intervals only -- no judgement about which are suspicious. That
        belongs in core.detectors.gaps, which weights these by coverage.
        """
        return [
            (a.position.t, b.position.t,
             (b.position.t - a.position.t).total_seconds())
            for a, b in zip(self.reports, self.reports[1:])
        ]

    def net_displacement_m(self) -> float:
        if len(self.reports) < 2:
            return 0.0
        return self.reports[0].position.distance_to(self.reports[-1].position)

    def path_length_m(self) -> float:
        return sum(
            a.position.distance_to(b.position)
            for a, b in zip(self.reports, self.reports[1:])
        )

    # -- the running fix -------------------------------------------------

    def position_at(self, t: datetime) -> Position | None:
        """Where was this platform at time t, according to its own reports?

        Three cases:

          1. t falls between two reports  -> interpolate along the great circle
          2. t is beyond the last report  -> dead reckon from speed and heading,
                                             inflating uncertainty as we go
          3. t precedes the first report  -> None. Never extrapolate backward.

        core.detectors.matching uses this to advance a report to the instant a
        sensor observation was made, so the two can be compared honestly.
        """
        t = _utc(t, "t")
        if not self.reports or t < self.t_start:
            return None

        # 1. bracketed
        for a, b in zip(self.reports, self.reports[1:]):
            pa, pb = a.position, b.position
            if pa.t <= t <= pb.t:
                span = (pb.t - pa.t).total_seconds()
                if span == 0:
                    return pa
                frac = (t - pa.t).total_seconds() / span
                brg = pa.bearing_to(pb)
                dist = pa.distance_to(pb)
                lat, lon = project(pa.lat, pa.lon, brg, dist * frac)
                # least certain midway between two fixes
                base = max(pa.uncertainty_m, pb.uncertainty_m)
                penalty = dist * 0.5 * (1 - abs(2 * frac - 1))
                return Position(lat, lon, t, base + penalty,
                                speed_mps=pa.speed_mps, heading_deg=brg,
                                alt_m=pa.alt_m)

        # 2. past the end -- dead reckon
        last = self.reports[-1].position
        if last.speed_mps is None or last.heading_deg is None:
            return None
        elapsed = (t - last.t).total_seconds()
        lat, lon = project(last.lat, last.lon, last.heading_deg,
                           last.speed_mps * elapsed)
        # Uncertainty grows with how long we have been guessing. Crude but
        # honest: assume the heading could be off by ~10%.
        drift = abs(last.speed_mps * elapsed) * 0.10
        return Position(lat, lon, t, last.uncertainty_m + drift,
                        speed_mps=last.speed_mps, heading_deg=last.heading_deg,
                        alt_m=last.alt_m)


@dataclass
class DiscrepancyEvent:
    """The product. Something reported and something observed do not agree.

    Carries its evidence so a human can audit why it fired. Keep the raw values
    -- the dossier view is only ever as good as this dict.
    """

    kind: EventKind
    domain: Domain
    t_start: datetime
    t_end: datetime
    lat: float
    lon: float
    confidence: float                                   # 0..1, and mean it
    platform_ids: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0,1]; got {self.confidence}")
        self.t_start = _utc(self.t_start, "t_start")
        self.t_end = _utc(self.t_end, "t_end")
        if self.t_end < self.t_start:
            raise ValueError("t_end precedes t_start")

    @property
    def duration_s(self) -> float:
        return (self.t_end - self.t_start).total_seconds()

    def to_geojson(self) -> dict[str, Any]:
        """One event as a GeoJSON Feature, for the map layer."""
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [self.lon, self.lat]},
            "properties": {
                "kind": self.kind,
                "domain": self.domain,
                "t_start": self.t_start.isoformat(),
                "t_end": self.t_end.isoformat(),
                "duration_s": self.duration_s,
                "confidence": self.confidence,
                "platform_ids": self.platform_ids,
                "evidence": self.evidence,
            },
        }
