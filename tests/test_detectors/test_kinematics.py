"""Tests for the kinematics detector.

Every case is a synthetic track with a known injected fault. The detector must
find exactly that fault and nothing else -- which is the whole reason
tests/synthetic.py exists. Point a detector at real data first and you cannot
tell a bug from a discovery.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from angels.adapters.aviation.plausibility import AviationPlausibility
from angels.adapters.maritime.plausibility import MaritimePlausibility
from angels.core.detectors import kinematics
from angels.core.models import Position, Report, Track
from tests.synthetic import T0, inject_jump, straight_track

AIR = AviationPlausibility()
SEA = MaritimePlausibility()


# -- the control case ------------------------------------------------------

def test_clean_track_produces_nothing() -> None:
    """The most important test here. A detector that fires on normal traffic
    is worse than no detector, because it buries the real ones."""
    assert kinematics.validate(straight_track(speed_mps=200.0), AIR) == []


def test_fast_but_legal_aircraft_is_not_flagged() -> None:
    """300 m/s is a real airliner. The limit is 340 for a reason."""
    assert kinematics.validate(straight_track(speed_mps=300.0), AIR) == []


def test_gps_noise_alone_does_not_trigger() -> None:
    """Two fixes 20 m apart with 10 m uncertainty each have not necessarily
    moved. At a one-second interval that noise would otherwise imply 20 m/s."""
    reports = []
    for i in range(20):
        lat = 39.0 + (0.0002 if i % 2 else 0.0)      # ~22 m jitter
        reports.append(Report("X", Position(
            lat, -77.0, T0 + timedelta(seconds=i), 10.0,
            speed_mps=0.0, heading_deg=0.0), "adsb"))
    assert kinematics.validate(Track("X", "air", reports), AIR) == []


# -- the positive case -----------------------------------------------------

def test_teleport_is_caught() -> None:
    track, bad_t = inject_jump(straight_track(n=60), at_index=30,
                               distance_m=500_000)
    events = kinematics.validate(track, AIR)
    assert events, "a 500 km jump between reports was not flagged"
    assert any(e.evidence["reason"].startswith("implied speed") for e in events)


def test_event_carries_auditable_evidence() -> None:
    track, _ = inject_jump(straight_track(n=60), at_index=30,
                           distance_m=500_000)
    e = kinematics.validate(track, AIR)[0]
    for key in ("implied_speed_mps", "max_speed_mps", "ratio",
                "distance_m", "interval_s", "from", "to"):
        assert key in e.evidence, f"missing {key}"
    assert e.evidence["implied_speed_mps"] > AIR.max_speed_mps
    assert e.kind == "kinematic"
    assert e.domain == "air"
    assert e.platform_ids == ["TEST01"]


def test_confidence_rises_with_severity() -> None:
    mild, _ = inject_jump(straight_track(n=60), at_index=30, distance_m=6_000)
    wild, _ = inject_jump(straight_track(n=60), at_index=30, distance_m=900_000)
    c_mild = kinematics.validate(mild, AIR)[0].confidence
    c_wild = kinematics.validate(wild, AIR)[0].confidence
    assert c_wild > c_mild


def test_confidence_saturates() -> None:
    """Twice impossible and ten times impossible are both just impossible.
    Pretending to distinguish them would be false precision."""
    t, _ = inject_jump(straight_track(n=60), at_index=30, distance_m=50_000_000)
    assert kinematics.validate(t, AIR)[0].confidence <= 0.95


# -- domain blindness ------------------------------------------------------

def test_same_speed_legal_for_aircraft_impossible_for_a_vessel() -> None:
    """The detector knows nothing about aircraft or ships. Everything comes
    through the PlausibilityModel, and this proves it."""
    track = straight_track(domain="sea", speed_mps=200.0)
    assert kinematics.validate(track, AIR) == []
    assert kinematics.validate(track, SEA) != []


def test_vessel_at_vessel_speed_is_clean() -> None:
    assert kinematics.validate(
        straight_track(domain="sea", speed_mps=8.0, interval_s=60), SEA) == []


# -- edge cases ------------------------------------------------------------

def test_zero_interval_is_skipped_not_divided_by() -> None:
    p = [Position(39.0, -77.0, T0, 10.0), Position(39.5, -77.0, T0, 10.0)]
    track = Track("X", "air", [Report("X", q, "adsb") for q in p])
    kinematics.validate(track, AIR)         # must not raise ZeroDivisionError


def test_single_report_track_is_fine() -> None:
    track = Track("X", "air", [Report("X", Position(39.0, -77.0, T0, 10.0), "adsb")])
    assert kinematics.validate(track, AIR) == []


def test_empty_track_is_fine() -> None:
    assert kinematics.validate(Track("X", "air", []), AIR) == []


# -- acceleration ----------------------------------------------------------

def test_impossible_acceleration_is_caught() -> None:
    """Position consistent, velocity field not. 0 to 300 m/s in one second."""
    reports = [
        Report("X", Position(39.0, -77.0, T0, 10.0,
                             speed_mps=0.0, heading_deg=90.0), "adsb"),
        Report("X", Position(39.0, -76.9999, T0 + timedelta(seconds=1), 10.0,
                             speed_mps=300.0, heading_deg=90.0), "adsb"),
    ]
    events = kinematics.validate(Track("X", "air", reports), AIR)
    assert any("acceleration" in e.evidence["reason"] for e in events)


def test_missing_speed_skips_the_acceleration_check() -> None:
    reports = [
        Report("X", Position(39.0, -77.0, T0, 10.0), "adsb"),
        Report("X", Position(39.0, -76.9999, T0 + timedelta(seconds=1), 10.0), "adsb"),
    ]
    assert kinematics.validate(Track("X", "air", reports), AIR) == []


# -- speed disagreement ----------------------------------------------------

def test_claimed_speed_contradicting_distance_is_flagged() -> None:
    """Partial spoofing signature: faking a position is easy, faking a
    self-consistent trajectory is not."""
    reports = [
        Report("X", Position(39.0, -77.0, T0, 10.0,
                             speed_mps=250.0, heading_deg=90.0), "adsb"),
        Report("X", Position(39.0, -77.0, T0 + timedelta(seconds=30), 10.0,
                             speed_mps=250.0, heading_deg=90.0), "adsb"),
    ]
    events = kinematics.speed_disagreement(Track("X", "air", reports), AIR)
    assert events, "claiming 250 m/s while not moving was not flagged"
    assert events[0].evidence["claimed_speed_mps"] == 250.0
    assert events[0].evidence["implied_speed_mps"] < 1.0


def test_consistent_speed_and_distance_agree() -> None:
    assert kinematics.speed_disagreement(
        straight_track(speed_mps=200.0, interval_s=10.0), AIR) == []


def test_stationary_platform_is_not_a_disagreement() -> None:
    reports = [
        Report("X", Position(39.0, -77.0, T0 + timedelta(seconds=60 * i), 10.0,
                             speed_mps=0.0, heading_deg=0.0), "ais")
        for i in range(5)
    ]
    assert kinematics.speed_disagreement(Track("X", "sea", reports), SEA) == []
