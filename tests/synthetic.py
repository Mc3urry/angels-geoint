"""Synthetic tracks with known, injected faults.

This is your ground truth for every detector you write in Phase 2. Build a
clean track, inject exactly one known anomaly, assert the detector finds
exactly that one. Without this you will spend weeks unable to tell a bug from
a discovery.

Not a test file itself -- a helper the tests import. Named without a test_
prefix so pytest does not try to collect it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from angels.core.models import Domain, Position, Report, Track, project

T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def straight_track(
    platform_id: str = "TEST01",
    domain: Domain = "air",
    *,
    start_lat: float = 39.29,
    start_lon: float = -76.61,          # Baltimore
    bearing_deg: float = 90.0,
    speed_mps: float = 200.0,
    interval_s: float = 10.0,
    n: int = 60,
    uncertainty_m: float = 10.0,
    t0: datetime = T0,
) -> Track:
    """A clean, well-behaved track. No faults. The control case."""
    reports = []
    lat, lon = start_lat, start_lon
    for i in range(n):
        t = t0 + timedelta(seconds=i * interval_s)
        reports.append(Report(
            platform_id=platform_id,
            position=Position(lat, lon, t, uncertainty_m,
                              speed_mps=speed_mps, heading_deg=bearing_deg),
            source="adsb" if domain == "air" else "ais",
        ))
        lat, lon = project(lat, lon, bearing_deg, speed_mps * interval_s)
    return Track(platform_id=platform_id, domain=domain, reports=reports)


def orbit_track(
    platform_id: str = "ORBIT1",
    *,
    center_lat: float = 39.29,
    center_lon: float = -76.61,
    radius_m: float = 3000.0,
    interval_s: float = 10.0,
    laps: int = 4,
    points_per_lap: int = 36,
    uncertainty_m: float = 10.0,
    t0: datetime = T0,
) -> Track:
    """Repeated circling about a fixed point.

    The positive case for orbits.classify -- and the shape a surveillance
    aircraft flies over a fixed ground location.
    """
    reports = []
    n = laps * points_per_lap
    for i in range(n):
        ang = (360.0 * i / points_per_lap) % 360.0
        lat, lon = project(center_lat, center_lon, ang, radius_m)
        t = t0 + timedelta(seconds=i * interval_s)
        reports.append(Report(
            platform_id=platform_id,
            position=Position(lat, lon, t, uncertainty_m,
                              speed_mps=120.0, heading_deg=(ang + 90) % 360),
            source="adsb",
        ))
    return Track(platform_id=platform_id, domain="air", reports=reports)


# --------------------------------------------------------------------------
# fault injection -- each returns (track, ground_truth)
# --------------------------------------------------------------------------

def inject_gap(track: Track, *, after_index: int = 20, drop: int = 15
               ) -> tuple[Track, tuple[datetime, datetime]]:
    """Delete a run of reports. Returns the track and the (start, end) of the gap."""
    kept = track.reports[:after_index] + track.reports[after_index + drop:]
    gap = (track.reports[after_index - 1].position.t,
           track.reports[after_index + drop].position.t)
    return Track(track.platform_id, track.domain, list(kept)), gap


def inject_jump(track: Track, *, at_index: int = 30, distance_m: float = 500_000.0,
                bearing_deg: float = 0.0) -> tuple[Track, datetime]:
    """Teleport one report. Returns the track and the timestamp of the bad fix."""
    reports = list(track.reports)
    bad = reports[at_index]
    lat, lon = project(bad.position.lat, bad.position.lon, bearing_deg, distance_m)
    moved = Position(lat, lon, bad.position.t, bad.position.uncertainty_m,
                     speed_mps=bad.position.speed_mps,
                     heading_deg=bad.position.heading_deg)
    reports[at_index] = Report(bad.platform_id, moved, bad.source)
    return Track(track.platform_id, track.domain, reports), moved.t


def duplicate_identity(track: Track, *, offset_m: float = 200_000.0
                       ) -> tuple[Track, Track]:
    """Two tracks broadcasting the SAME id from different places at the same time.

    Physically impossible, so any detector worth having flags it.
    """
    reports = []
    for r in track.reports:
        lat, lon = project(r.position.lat, r.position.lon, 180.0, offset_m)
        reports.append(Report(
            track.platform_id,
            Position(lat, lon, r.position.t, r.position.uncertainty_m,
                     speed_mps=r.position.speed_mps,
                     heading_deg=r.position.heading_deg),
            r.source,
        ))
    return track, Track(track.platform_id, track.domain, reports)
