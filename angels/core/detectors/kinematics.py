"""Impossible movement.

PHASE 2.

The cheapest detector worth having. Between any two consecutive reports there
is an implied speed -- distance over elapsed time -- and an implied
acceleration. Either exceeding what the platform can physically do means the
report is wrong: a bad fix, a decoding error, or a spoof.

Domain-blind. What "impossible" means arrives entirely through the
PlausibilityModel: 340 m/s for an aircraft, 26 for a vessel. This module does
not know which it is looking at.

A note on why this uses IMPLIED speed rather than the reported speed field.
A spoofed or corrupt report can claim any velocity it likes; the distance it
travelled between two fixes is arithmetic on the positions themselves. When
the two disagree, that disagreement is itself worth flagging -- see
`speed_disagreement` below.
"""

from __future__ import annotations

from angels.core.models import DiscrepancyEvent, Track
from angels.core.plausibility import PlausibilityModel

# How far over the limit before we care. Reports are noisy, and a fix that is
# 5% too fast is measurement error rather than a finding. Ten percent is
# comfortably outside GPS noise at any realistic report interval.
TOLERANCE = 1.10


def _confidence(ratio: float) -> float:
    """Confidence from how badly the limit was exceeded.

    Deliberately saturating: twice the maximum speed and ten times the maximum
    speed are both simply impossible, and pretending to distinguish them would
    be false precision. Reaches ~0.95 at 3x.
    """
    if ratio <= TOLERANCE:
        return 0.0
    over = ratio - TOLERANCE
    return min(0.95, 0.5 + 0.225 * over)


def validate(track: Track, plausibility: PlausibilityModel
             ) -> list[DiscrepancyEvent]:
    """Consecutive reports implying motion the platform cannot perform.

    Returns one event per offending interval. The event is placed at the
    SECOND report of the pair -- the one that arrived somewhere it could not
    have reached.
    """
    events: list[DiscrepancyEvent] = []

    for a, b in zip(track.reports, track.reports[1:]):
        pa, pb = a.position, b.position
        dt = (pb.t - pa.t).total_seconds()
        if dt <= 0:
            continue          # duplicate or out-of-order; identity.py's problem

        dist = pa.distance_to(pb)

        # Subtract the combined positional uncertainty before computing speed.
        # Two fixes 20 m apart with 10 m uncertainty each have not necessarily
        # moved at all, and at a one-second interval that noise alone would
        # otherwise imply 20 m/s.
        effective = max(0.0, dist - pa.combined_uncertainty(pb))
        implied = effective / dt
        ratio = implied / plausibility.max_speed_mps

        if ratio > TOLERANCE:
            events.append(DiscrepancyEvent(
                kind="kinematic",
                domain=track.domain,
                t_start=pa.t,
                t_end=pb.t,
                lat=pb.lat,
                lon=pb.lon,
                confidence=_confidence(ratio),
                platform_ids=[track.platform_id],
                evidence={
                    "reason": "implied speed exceeds platform maximum",
                    "implied_speed_mps": round(implied, 1),
                    "max_speed_mps": plausibility.max_speed_mps,
                    "ratio": round(ratio, 2),
                    "distance_m": round(dist, 1),
                    "interval_s": round(dt, 1),
                    "uncertainty_m": round(pa.combined_uncertainty(pb), 1),
                    "from": [pa.lat, pa.lon],
                    "to": [pb.lat, pb.lon],
                },
            ))
            continue      # a teleport makes the acceleration check meaningless

        # Acceleration, only where both reports carry a speed.
        if pa.speed_mps is not None and pb.speed_mps is not None:
            accel = abs(pb.speed_mps - pa.speed_mps) / dt
            aratio = accel / plausibility.max_accel_mps2
            if aratio > TOLERANCE:
                events.append(DiscrepancyEvent(
                    kind="kinematic",
                    domain=track.domain,
                    t_start=pa.t,
                    t_end=pb.t,
                    lat=pb.lat,
                    lon=pb.lon,
                    confidence=_confidence(aratio),
                    platform_ids=[track.platform_id],
                    evidence={
                        "reason": "implied acceleration exceeds platform maximum",
                        "implied_accel_mps2": round(accel, 2),
                        "max_accel_mps2": plausibility.max_accel_mps2,
                        "ratio": round(aratio, 2),
                        "speed_from_mps": pa.speed_mps,
                        "speed_to_mps": pb.speed_mps,
                        "interval_s": round(dt, 1),
                    },
                ))

    return events


def speed_disagreement(track: Track, plausibility: PlausibilityModel,
                       *, factor: float = 3.0) -> list[DiscrepancyEvent]:
    """Reported velocity contradicting the distance actually covered.

    A weaker signal than `validate`, and a more interesting one. Both values
    can individually be plausible while being inconsistent with each other,
    which is the signature of a position field and a velocity field that are
    not describing the same object -- exactly what partial spoofing looks
    like, since faking a position is easy and faking a self-consistent
    trajectory is not.

    Kept separate from validate() because it produces softer evidence and you
    will want to tune `factor` independently.
    """
    events: list[DiscrepancyEvent] = []

    for a, b in zip(track.reports, track.reports[1:]):
        pa, pb = a.position, b.position
        if pa.speed_mps is None:
            continue
        dt = (pb.t - pa.t).total_seconds()
        if dt <= 0:
            continue

        implied = pa.distance_to(pb) / dt
        claimed = pa.speed_mps

        # Both directions matter: claiming to move while stationary, and
        # covering ground while claiming to be still.
        if claimed < 1.0 and implied < 1.0:
            continue
        hi, lo = max(implied, claimed), max(0.5, min(implied, claimed))
        if hi / lo < factor:
            continue

        events.append(DiscrepancyEvent(
            kind="kinematic",
            domain=track.domain,
            t_start=pa.t,
            t_end=pb.t,
            lat=pb.lat,
            lon=pb.lon,
            confidence=min(0.7, 0.3 + 0.05 * (hi / lo)),
            platform_ids=[track.platform_id],
            evidence={
                "reason": "reported speed disagrees with distance covered",
                "claimed_speed_mps": round(claimed, 1),
                "implied_speed_mps": round(implied, 1),
                "ratio": round(hi / lo, 1),
                "interval_s": round(dt, 1),
            },
        ))

    return events
