"""Tests for the identity detector.

The highest-confidence detector in the set, because it asks a physics question
with no threshold to tune: could one object have been in both places? When the
answer is no, something is wrong with the data, full stop.
"""

from __future__ import annotations

from datetime import timedelta

from angels.adapters.aviation.plausibility import AviationPlausibility
from angels.adapters.maritime.plausibility import MaritimePlausibility
from angels.core.detectors import identity
from angels.core.models import Position, Report, Track, project
from tests.synthetic import T0, duplicate_identity, straight_track

AIR = AviationPlausibility()
SEA = MaritimePlausibility()


def _rep(pid, lat, lon, t, unc=10.0):
    return Report(pid, Position(lat, lon, t, unc), "adsb")


# -- collisions ------------------------------------------------------------

def test_clean_traffic_produces_nothing() -> None:
    tracks = [straight_track("AAA111"), straight_track("BBB222")]
    assert identity.collisions(tracks, AIR) == []


def test_one_id_in_two_places_is_caught() -> None:
    a, b = duplicate_identity(straight_track("AAA111", n=20), offset_m=200_000)
    events = identity.collisions([a, b], AIR)
    assert events, "the same ICAO24 200 km apart simultaneously was not flagged"
    assert events[0].platform_ids == ["AAA111"]
    assert events[0].kind == "identity"


def test_collision_confidence_is_high() -> None:
    """Physically impossible, so this is not a judgement call."""
    a, b = duplicate_identity(straight_track("AAA111", n=20), offset_m=500_000)
    assert identity.collisions([a, b], AIR)[0].confidence > 0.75


def test_only_the_worst_pair_is_reported() -> None:
    """A spoofed aircraft produces hundreds of impossible pairs. Reporting
    every one buries the finding in its own evidence."""
    a, b = duplicate_identity(straight_track("AAA111", n=60), offset_m=300_000)
    assert len(identity.collisions([a, b], AIR)) == 1


def test_evidence_names_both_positions() -> None:
    a, b = duplicate_identity(straight_track("AAA111", n=20), offset_m=200_000)
    ev = identity.collisions([a, b], AIR)[0].evidence
    for key in ("separation_m", "interval_s", "max_reachable_m",
                "excess_m", "position_a", "position_b", "t_a", "t_b"):
        assert key in ev, f"missing {key}"
    assert ev["separation_m"] > ev["max_reachable_m"]


def test_a_reachable_separation_is_not_a_collision() -> None:
    """Two reports 60s apart and 10 km apart is 167 m/s. An aircraft does
    that all day."""
    t = T0
    tracks = [Track("AAA111", "air", [
        _rep("AAA111", 39.0, -77.0, t),
        _rep("AAA111", *project(39.0, -77.0, 90.0, 10_000), t + timedelta(seconds=60)),
    ])]
    assert identity.collisions(tracks, AIR) == []


def test_domain_limits_decide_what_is_impossible() -> None:
    """Identical geometry. Legal for an aircraft, impossible for a ship --
    and the detector itself knows nothing about either."""
    t = T0
    lat2, lon2 = project(39.0, -77.0, 90.0, 15_000)
    tracks = [Track("X", "air", [
        _rep("X", 39.0, -77.0, t),
        _rep("X", lat2, lon2, t + timedelta(seconds=60)),
    ])]
    assert identity.collisions(tracks, AIR) == []
    assert identity.collisions(tracks, SEA) != []


def test_reports_outside_the_window_are_not_compared() -> None:
    """Two hours apart is not simultaneous, whatever the distance."""
    t = T0
    tracks = [Track("X", "air", [
        _rep("X", 39.0, -77.0, t),
        _rep("X", 10.0, 20.0, t + timedelta(hours=2)),
    ])]
    assert identity.collisions(tracks, AIR, window_s=60) == []


# -- reuse -----------------------------------------------------------------

def test_resuming_beyond_reachable_range_is_flagged() -> None:
    """One hour of silence, then the same MMSI appears 5000 km away. A ship
    does not cross an ocean in an hour."""
    t = T0
    tracks = [Track("999888777", "sea", [
        _rep("999888777", 39.0, -77.0, t),
        _rep("999888777", *project(39.0, -77.0, 90.0, 5_000_000),
             t + timedelta(hours=1)),
    ])]
    events = identity.reuse(tracks, SEA)
    assert events
    assert events[0].evidence["implied_speed_mps"] > SEA.max_speed_mps


def test_a_plausible_journey_is_not_reuse() -> None:
    """Twelve hours of silence, then 300 km further on. Entirely normal for
    a vessel that simply left receiver coverage."""
    t = T0
    tracks = [Track("999888777", "sea", [
        _rep("999888777", 39.0, -77.0, t),
        _rep("999888777", *project(39.0, -77.0, 90.0, 300_000),
             t + timedelta(hours=12)),
    ])]
    assert identity.reuse(tracks, SEA) == []


def test_short_gaps_are_not_considered_reuse() -> None:
    """Below min_gap_s this is collisions()'s question, not reuse()'s."""
    t = T0
    tracks = [Track("X", "air", [
        _rep("X", 39.0, -77.0, t),
        _rep("X", 10.0, 20.0, t + timedelta(seconds=120)),
    ])]
    assert identity.reuse(tracks, AIR, min_gap_s=3600) == []


# -- duplicates ------------------------------------------------------------

def test_two_differing_reports_at_one_timestamp() -> None:
    """A data-quality problem, not a physics one -- but it would quietly
    inflate every count computed downstream."""
    t = T0
    tracks = [Track("X", "air", [
        _rep("X", 39.0, -77.0, t),
        _rep("X", 39.5, -77.0, t),
    ])]
    events = identity.duplicates(tracks)
    assert events
    assert "timestamp" in events[0].evidence["reason"]


def test_identical_reports_are_not_duplicates() -> None:
    """The same message ingested twice is harmless. Two DIFFERENT positions
    at one instant is the problem."""
    t = T0
    tracks = [Track("X", "air", [
        _rep("X", 39.0, -77.0, t),
        _rep("X", 39.0, -77.0, t),
    ])]
    assert identity.duplicates(tracks) == []


# -- edges -----------------------------------------------------------------

def test_empty_input_is_fine() -> None:
    assert identity.collisions([], AIR) == []
    assert identity.reuse([], AIR) == []
    assert identity.duplicates([]) == []


def test_gap_split_tracks_are_rejoined_by_identifier() -> None:
    """split_into_tracks may cut one platform into several Tracks. Identity
    works on the identifier, so it has to see across that split -- otherwise
    a spoof spanning a gap boundary would be invisible."""
    t = T0
    a = Track("SAME", "air", [_rep("SAME", 39.0, -77.0, t)])
    b = Track("SAME", "air", [_rep("SAME", 10.0, 20.0, t + timedelta(seconds=30))])
    assert identity.collisions([a, b], AIR) != []
