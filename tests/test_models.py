"""Tests for the core domain model.

Note what is being pinned down here: the invariants that are expensive to fix
later. Uncertainty is always present. Time is always UTC. Reports stay sorted.
position_at never extrapolates backward.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from angels.core.models import (
    DiscrepancyEvent,
    Observation,
    Position,
    Report,
    Track,
    haversine_m,
    project,
)
from tests.synthetic import T0, duplicate_identity, inject_gap, straight_track


# -- geodesy ---------------------------------------------------------------

def test_haversine_known_distance() -> None:
    """Baltimore to Washington DC is about 56 km."""
    d = haversine_m(39.2904, -76.6122, 38.9072, -77.0369)
    assert 54_000 < d < 58_000


def test_project_round_trips() -> None:
    """Going out and coming back lands where you started."""
    lat, lon = project(39.29, -76.61, 45.0, 10_000)
    back = haversine_m(39.29, -76.61, lat, lon)
    assert math.isclose(back, 10_000, rel_tol=1e-3)


# -- Position --------------------------------------------------------------

def test_position_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Position(39.0, -76.0, datetime(2026, 9, 1, 12, 0), 10.0)


def test_position_rejects_bad_coordinates() -> None:
    with pytest.raises(ValueError, match="lat out of range"):
        Position(91.0, 0.0, T0, 10.0)
    with pytest.raises(ValueError, match="lon out of range"):
        Position(0.0, 181.0, T0, 10.0)


def test_position_rejects_negative_uncertainty() -> None:
    with pytest.raises(ValueError, match="uncertainty_m"):
        Position(39.0, -76.0, T0, -1.0)


def test_uncertainties_add_in_quadrature() -> None:
    a = Position(39.0, -76.0, T0, 30.0)
    b = Position(39.0, -76.0, T0, 40.0)
    assert math.isclose(a.combined_uncertainty(b), 50.0)


def test_agrees_with_respects_uncertainty() -> None:
    """Two sloppy fixes can agree where two precise ones would not."""
    t = T0
    a = Position(39.0000, -76.0000, t, 10.0)
    far = Position(39.0100, -76.0000, t, 10.0)          # ~1.1 km apart
    assert not a.agrees_with(far)

    sloppy_a = Position(39.0000, -76.0000, t, 500.0)
    sloppy_b = Position(39.0100, -76.0000, t, 500.0)
    assert sloppy_a.agrees_with(sloppy_b)


# -- Track -----------------------------------------------------------------

def test_track_sorts_reports_on_construction() -> None:
    p = [Position(39.0, -76.0, T0 + timedelta(seconds=s), 10.0) for s in (30, 0, 60)]
    tr = Track("X", "air", [Report("X", q, "adsb") for q in p])
    assert [r.position.t for r in tr.reports] == sorted(r.position.t for r in tr.reports)


def test_intervals_match_report_spacing() -> None:
    tr = straight_track(interval_s=10.0, n=5)
    assert [round(s) for _, _, s in tr.intervals()] == [10, 10, 10, 10]


def test_gap_injection_produces_a_measurable_hole() -> None:
    tr = straight_track(interval_s=10.0, n=60)
    holed, (start, end) = inject_gap(tr, after_index=20, drop=15)
    longest = max(s for _, _, s in holed.intervals())
    assert longest == pytest.approx((end - start).total_seconds())
    assert longest > 100          # 16 intervals of 10s


def test_straight_track_path_equals_displacement() -> None:
    """A straight line has no wasted distance. Guards the orbit detector later."""
    tr = straight_track(n=20)
    assert tr.path_length_m() == pytest.approx(tr.net_displacement_m(), rel=1e-3)


# -- the running fix -------------------------------------------------------

def test_position_at_interpolates_between_reports() -> None:
    tr = straight_track(interval_s=10.0, speed_mps=200.0, n=10)
    a, b = tr.reports[0].position, tr.reports[1].position
    mid = tr.position_at(a.t + timedelta(seconds=5))
    assert mid is not None
    assert mid.distance_to(a) == pytest.approx(1000.0, rel=0.02)
    assert mid.distance_to(b) == pytest.approx(1000.0, rel=0.02)


def test_interpolated_uncertainty_peaks_midway() -> None:
    """We are least sure halfway between two fixes. That must show up."""
    tr = straight_track(interval_s=10.0, n=10)
    t0 = tr.reports[0].position.t
    near = tr.position_at(t0 + timedelta(seconds=1))
    mid = tr.position_at(t0 + timedelta(seconds=5))
    assert mid.uncertainty_m > near.uncertainty_m


def test_position_at_dead_reckons_past_the_end() -> None:
    tr = straight_track(interval_s=10.0, speed_mps=200.0, n=10)
    future = tr.position_at(tr.t_end + timedelta(seconds=60))
    assert future is not None
    assert future.distance_to(tr.reports[-1].position) == pytest.approx(12_000, rel=0.02)
    # and it must be less certain than a real fix
    assert future.uncertainty_m > tr.reports[-1].position.uncertainty_m


def test_position_at_refuses_to_extrapolate_backward() -> None:
    tr = straight_track()
    assert tr.position_at(tr.t_start - timedelta(seconds=1)) is None


# -- identity --------------------------------------------------------------

def test_duplicate_identity_puts_one_id_in_two_places() -> None:
    a, b = duplicate_identity(straight_track(n=10))
    assert a.platform_id == b.platform_id
    same_t = a.reports[0].position.t == b.reports[0].position.t
    assert same_t
    assert a.reports[0].position.distance_to(b.reports[0].position) > 100_000


# -- Observation vs Report -------------------------------------------------

def test_observation_carries_no_identity_by_default() -> None:
    """The asymmetry the whole project rests on."""
    obs = Observation(Position(39.0, -76.0, T0, 50.0), sensor="sar",
                      attributes={"length_m": 84.0})
    assert obs.platform_id is None
    assert obs.attributes["length_m"] == 84.0


# -- DiscrepancyEvent ------------------------------------------------------

def test_event_rejects_impossible_confidence() -> None:
    with pytest.raises(ValueError, match="confidence"):
        DiscrepancyEvent("gap", "air", T0, T0, 39.0, -76.0, 1.5)


def test_event_rejects_reversed_times() -> None:
    with pytest.raises(ValueError, match="precedes"):
        DiscrepancyEvent("gap", "air", T0 + timedelta(seconds=10), T0,
                         39.0, -76.0, 0.5)


def test_event_geojson_shape() -> None:
    ev = DiscrepancyEvent("unmatched", "sea", T0, T0 + timedelta(minutes=5),
                          39.0, -76.0, 0.8, ["123456789"], {"sensor": "sar"})
    gj = ev.to_geojson()
    assert gj["type"] == "Feature"
    assert gj["geometry"]["coordinates"] == [-76.0, 39.0]      # lon, lat order
    assert gj["properties"]["duration_s"] == 300.0
