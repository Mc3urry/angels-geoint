"""Tests for AIS ingest.

Almost every failure mode here produces output rather than an error: a
sentinel value taken as data, a berth interpolated across, a read failure
returning an empty sea. So the tests are mostly about values that parse
cleanly and mean the opposite of what they say.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from angels.adapters.maritime import ais
from angels.adapters.maritime.ais import (
    AISReadError, AISReport, load, row_to_report, to_tracks,
)
from angels.core.detectors import matching

T = datetime(2024, 9, 25, 22, 58, 0, tzinfo=timezone.utc)


def row(**kw):
    base = {
        "MMSI": 366999123, "BaseDateTime": T, "LAT": 38.9, "LON": -76.2,
        "SOG": 12.0, "COG": 91.0, "Heading": 90, "VesselName": "EVER GIVEN",
        "IMO": "IMO9811000", "CallSign": "H3RC", "VesselType": 70,
        "Status": 0, "Length": 399.0, "Width": 59.0, "Draft": 14.5,
        "Cargo": 70, "TransceiverClass": "A",
    }
    base.update(kw)
    return base


# -- the ordinary case -----------------------------------------------------

def test_a_row_becomes_a_report() -> None:
    r = row_to_report(row())
    assert isinstance(r, AISReport)
    assert r.platform_id == "366999123"
    assert r.source == "ais"
    assert r.position.lat == 38.9
    assert r.name == "EVER GIVEN"


def test_speed_is_converted_out_of_knots() -> None:
    """Knots, nautical miles and feet all try to leak in from source data.
    Stop them in the adapter; nothing downstream should ever see one."""
    r = row_to_report(row(SOG=10.0))
    assert r.position.speed_mps == pytest.approx(5.14444, abs=1e-4)


def test_length_reaches_the_match_radius() -> None:
    """The core never learns that AIS exists -- matching reads length through
    getattr. If that link breaks, a 399 m hull is sized as a 50 m default and
    its bow becomes a separate, unreported vessel."""
    r = row_to_report(row(Length=399.0))
    from angels.core.models import Track
    assert matching.vessel_length_m(Track("x", "sea", [r])) == pytest.approx(399.0)


# -- sentinel values, which all parse --------------------------------------

def test_heading_511_is_not_a_heading() -> None:
    """THE CLASSIC ONE. 511 means 'not available'. Taken as data, every vessel
    without a gyro reading dead-reckons east-south-east, and produces a
    position while doing it."""
    r = row_to_report(row(COG=ais.COG_UNAVAILABLE, Heading=511))
    assert r.position.heading_deg is None


def test_sog_102_3_is_not_a_speed() -> None:
    """52 m/s. Dead reckoning takes a vessel 3 km in a minute, silently."""
    assert row_to_report(row(SOG=102.3)).position.speed_mps is None


def test_cog_360_is_not_north() -> None:
    """360 encodes 'unavailable', not a bearing. Read as 0 it points every
    unknown vessel due north, which is worse than admitting ignorance."""
    r = row_to_report(row(COG=360.0, Heading=511))
    assert r.position.heading_deg is None


def test_an_implausible_speed_is_refused() -> None:
    assert row_to_report(row(SOG=300.0)).position.speed_mps is None


def test_lat_91_and_lon_181_are_dropped() -> None:
    assert row_to_report(row(LAT=91.0)) is None
    assert row_to_report(row(LON=181.0)) is None


def test_course_over_ground_beats_heading() -> None:
    """COG is where the vessel is GOING; heading is where it is POINTING. In a
    cross-current those differ, and dead reckoning needs the first."""
    assert row_to_report(row(COG=45.0, Heading=90)).position.heading_deg == 45.0


def test_heading_is_the_fallback_when_cog_is_absent() -> None:
    r = row_to_report(row(COG=ais.COG_UNAVAILABLE, Heading=90))
    assert r.position.heading_deg == 90.0


def test_a_row_without_an_mmsi_is_not_a_platform() -> None:
    assert row_to_report(row(MMSI=0)) is None
    assert row_to_report(row(MMSI=None)) is None


def test_a_missing_length_is_none_not_zero() -> None:
    """Zero is what the bulk files write for 'not reported'. Carried through
    as a real length it would shrink the match radius instead of widening it,
    and the vessels it shrinks for are the ones with no dimensions on file."""
    assert row_to_report(row(Length=0)).length_m is None


def test_draft_lands_in_alt_m() -> None:
    """The sea domain's vertical axis. alt_m is altitude for aircraft and
    draft for vessels -- one field, because the core is domain-blind."""
    assert row_to_report(row(Draft=14.5)).position.alt_m == pytest.approx(14.5)


def test_uncertainty_is_stated_not_invented_per_row() -> None:
    """AIS does not report positional uncertainty, so it is assumed. The
    assumption is a named constant rather than a literal buried in a
    constructor, because a reader has to be able to find and argue with it."""
    assert row_to_report(row()).position.uncertainty_m == ais.POSITION_UNCERTAINTY_M


def test_uncertainty_does_not_double_count_hull_length() -> None:
    """matching.pair_radius already adds half the hull. Inflating the fix by
    it here as well would widen every radius by a ship and quietly explain
    away real discrepancies."""
    big = row_to_report(row(Length=399.0))
    small = row_to_report(row(Length=12.0))
    assert big.position.uncertainty_m == small.position.uncertainty_m


# -- splitting -------------------------------------------------------------

def reports_at(offsets_s, mmsi="1", lat=38.9, lon=-76.2):
    return [row_to_report(row(MMSI=int(mmsi), LAT=lat, LON=lon,
                              BaseDateTime=T + timedelta(seconds=s)))
            for s in offsets_s]


def test_one_mmsi_reporting_steadily_is_one_track() -> None:
    assert len(to_tracks(reports_at([0, 60, 120, 180]))) == 1


def test_a_berth_splits_the_track() -> None:
    """THE POINT OF THE FILE.

    A vessel reports, goes quiet for six hours alongside, then sails. Joined
    into one track, position_at interpolates a straight line across that
    silence and places the ship mid-bay while it was tied up -- a fabricated
    position that fails to match a real detection and is then counted as a
    dark vessel.
    """
    tracks = to_tracks(reports_at([0, 60, 6 * 3600, 6 * 3600 + 60]))
    assert len(tracks) == 2
    assert all(len(t) == 2 for t in tracks)


def berth():
    """Alongside at A, then a six-hour silence, then reporting at B."""
    return [row_to_report(row(MMSI=1, LAT=38.9, LON=-76.2, BaseDateTime=T)),
            row_to_report(row(MMSI=1, LAT=39.2, LON=-76.5,
                              BaseDateTime=T + timedelta(hours=6)))]


def test_the_middle_of_a_long_silence_is_already_defended() -> None:
    """Not everything here needs splitting to fix it.

    position_at's interpolation penalty is proportional to how far apart the
    bracketing reports are and peaks halfway between them, so a fix three
    hours into a 42 km silence claims 21 km of uncertainty and matching
    refuses it on its own.
    """
    from angels.core.models import Track
    fix = Track("1", "sea", berth()).position_at(T + timedelta(hours=3))
    assert fix.uncertainty_m > matching.MAX_FIX_UNCERTAINTY_M


def test_the_end_of_a_long_silence_is_not() -> None:
    """THE POINT OF THE FILE, and it is the opposite end from the obvious one.

    That penalty falls to ZERO at both ends of the gap. Five minutes before
    the second report, a vessel that sat alongside the whole time is placed
    near its destination -- 41.6 km from where it was -- claiming 607 m of
    precision. Matching accepts that fix, fails to find the vessel where it
    really was (calling a real detection dark), and records a miss where it
    never went. One silence, two fabricated results, no warning.

    Splitting closes exactly this. The two mechanisms are complementary: the
    penalty covers the middle, the split covers the ends.
    """
    from angels.core.models import Track
    rs = berth()
    late = T + timedelta(minutes=355)
    alongside = rs[0].position

    whole = Track("1", "sea", rs).position_at(late)
    assert whole.uncertainty_m < matching.MAX_FIX_UNCERTAINTY_M, (
        "the unsplit fix is accepted...")
    assert alongside.distance_to(whole) > 40_000, (
        "...while being tens of kilometres from where the vessel was")

    for seg in to_tracks(rs):
        fix = seg.position_at(late)
        assert fix is None or fix.uncertainty_m > matching.MAX_FIX_UNCERTAINTY_M, (
            "no split segment may supply a usable fix inside the silence")


def test_splitting_is_the_callers_choice_not_a_property_of_the_data() -> None:
    """core.detectors.gaps exists to find exactly the silence that splitting
    removes. Baking the split into ingest would delete every gap before the
    gap detector ever ran."""
    rs = reports_at([0, 60, 6 * 3600])
    assert len(to_tracks(rs, split_gap_s=None)) == 1
    assert len(to_tracks(rs, split_gap_s=ais.SPLIT_GAP_S)) == 2


def test_class_b_cadence_does_not_split_a_healthy_track() -> None:
    """Class B reports every 30 s to 3 minutes under way. A split threshold
    tight enough to catch a berth must still be loose enough to leave those
    alone, or half the small-craft fleet arrives as single-report fragments."""
    assert len(to_tracks(reports_at([0, 180, 360, 540]))) == 1


def test_two_vessels_are_two_tracks() -> None:
    rs = reports_at([0, 60], mmsi="1") + reports_at([0, 60], mmsi="2")
    assert len(to_tracks(rs)) == 2


def test_reports_are_ordered_within_a_track() -> None:
    rs = reports_at([120, 0, 60])
    assert to_tracks(rs)[0].reports[0].position.t == T


def test_duplicate_timestamps_are_collapsed() -> None:
    """Two receivers hearing the same message. Left in, they inflate the
    report count, which coverage modelling later reads as evidence of how well
    a vessel was heard."""
    assert len(to_tracks(reports_at([0, 0, 60]))[0]) == 2


def test_class_b_is_identifiable() -> None:
    """Class B is voluntary, lower-powered and fitted to smaller craft -- less
    likely to be heard AND less likely to be seen by radar. Confusing the two
    turns a reporting property into a sensing one."""
    assert row_to_report(row(TransceiverClass="B")).is_class_b
    assert not row_to_report(row(TransceiverClass="A")).is_class_b


# -- reading ---------------------------------------------------------------

def test_a_missing_store_raises_rather_than_returning_an_empty_sea(tmp_path):
    """THE RECURRING MISTAKE, refused once more. An unreadable path and a sea
    with no vessels in it must not arrive downstream looking the same."""
    with pytest.raises(AISReadError) as e:
        load(tmp_path / "nothing")
    assert "clip_ais" in str(e.value)


def test_the_error_says_why_it_matters(tmp_path) -> None:
    with pytest.raises(AISReadError) as e:
        load(tmp_path)
    assert "no vessels" in str(e.value)


# -- end to end against matching -------------------------------------------

def test_a_reported_vessel_matches_a_detection_on_top_of_it() -> None:
    """The join this whole module exists for."""
    from angels.core.models import Observation, Position
    tracks = to_tracks(reports_at([-300, 300]))
    det = Observation(
        position=Position(lat=38.9, lon=-76.2, t=T, uncertainty_m=40.0),
        sensor="sentinel1-vv-cfar", attributes={"confidence": 0.8})

    r = matching.associate([det], tracks, T)
    assert len(r.pairs) == 1
    assert r.detection_rate == pytest.approx(1.0)
    assert not matching.unmatched([det], tracks, T)
