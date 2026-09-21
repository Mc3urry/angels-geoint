"""Tests for the scene footprint -- the denominator of the detection rate.

The first real run scored every slice against every vessel in AOI_SEA. All
four slices reported the same ~1,227 tracks, which is impossible: slice 1
covers offshore Virginia at 36N and slice 4 covers Pennsylvania at 40N. The
detection rate came out at 1% for reasons that had nothing to do with the
detector.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mm = _load("match_maritime")
ds = _load("detect_ships")

from angels.core.models import Position, Report, Track  # noqa: E402

T = datetime(2024, 9, 25, 22, 58, tzinfo=timezone.utc)

# A box over the Chesapeake approaches, counter-clockwise.
BOX = [[-76.0, 37.0], [-75.0, 37.0], [-75.0, 38.0], [-76.0, 38.0]]


def trk(mmsi, lat, lon):
    rs = [Report(mmsi, Position(lat=lat, lon=lon, t=T + timedelta(seconds=s),
                                uncertainty_m=20.0, speed_mps=4.0,
                                heading_deg=90.0), "ais")
          for s in (-300, 300)]
    return Track(mmsi, "sea", rs)


# -- point in polygon ------------------------------------------------------

def test_a_point_inside_is_inside() -> None:
    assert mm.inside(-75.5, 37.5, BOX)


@pytest.mark.parametrize("lon,lat", [(-77.0, 37.5), (-74.0, 37.5),
                                     (-75.5, 36.0), (-75.5, 39.0)])
def test_points_outside_are_outside(lon, lat) -> None:
    assert not mm.inside(lon, lat, BOX)


def test_a_concave_footprint_is_handled() -> None:
    """A Sentinel-1 swath curves in range, so the sampled edges are not
    straight and the polygon can be slightly non-convex. Ray casting does not
    care; a convex-hull test would."""
    notch = [[-76.0, 37.0], [-75.0, 37.0], [-75.0, 38.0],
             [-75.5, 37.5], [-76.0, 38.0]]
    # The top-left edge runs (-75.5, 37.5) -> (-76.0, 38.0) with slope -1, so
    # at lon -75.95 the boundary is at lat 37.95. Points are chosen clear of
    # it: a point exactly ON an edge is degenerate for ray casting and tests
    # nothing about concavity.
    assert mm.inside(-75.95, 37.80, notch), "left lobe"
    assert mm.inside(-75.20, 37.20, notch), "right lobe"
    assert not mm.inside(-75.40, 37.90, notch), "the notch itself"


# -- the denominator -------------------------------------------------------

def test_only_tracks_inside_the_scene_are_counted() -> None:
    """THE POINT OF THE FILE. A slice must not be charged with missing a
    vessel 300 km outside its own swath."""
    tracks = [trk("in1", 37.5, -75.5), trk("in2", 37.2, -75.8),
              trk("far", 39.4, -75.2), trk("also_far", 36.2, -74.1)]
    kept = mm.in_footprint(tracks, T, BOX)
    assert [t.platform_id for t in kept] == ["in1", "in2"]


def test_a_track_with_no_fix_at_t_is_dropped() -> None:
    """It cannot be placed, so it cannot be shown to be inside. It is already
    excluded from the detection rate by matching.associate; dropping it here
    keeps the two counts consistent."""
    ghost = Track("ghost", "sea", [
        Report("ghost", Position(lat=37.5, lon=-75.5,
                                 t=T + timedelta(hours=5), uncertainty_m=20.0),
               "ais")])
    assert mm.in_footprint([ghost], T, BOX) == []


def test_an_empty_footprint_keeps_nothing() -> None:
    assert mm.in_footprint([trk("a", 37.5, -75.5)], T, []) == []


# -- building the footprint ------------------------------------------------

class FakeLoc:
    """A locator over a simple rotated, slightly curved swath."""

    def __init__(self, w, h):
        self.w, self.h = w, h

    def lonlat(self, col, row):
        u, v = col / self.w, row / self.h
        return (-76.5 + u * 2.0 + 0.05 * v, 36.5 + v * 3.0 - 0.04 * u)


def test_the_footprint_encloses_the_raster_corners() -> None:
    w, h = 25_000, 16_000
    loc = FakeLoc(w - 1, h - 1)
    poly = ds.scene_footprint(loc, w, h)
    for col, row in ((0, 0), (w - 1, 0), (w - 1, h - 1), (0, h - 1)):
        lon, lat = loc.lonlat(col, row)
        # corners sit ON the boundary; nudge inward before testing
        cx = sum(p[0] for p in poly) / len(poly)
        cy = sum(p[1] for p in poly) / len(poly)
        assert mm.inside(lon + (cx - lon) * 0.01, lat + (cy - lat) * 0.01, poly)


def test_the_footprint_excludes_a_point_well_outside() -> None:
    loc = FakeLoc(24_999, 15_999)
    poly = ds.scene_footprint(loc, 25_000, 16_000)
    assert not mm.inside(-79.0, 37.0, poly)
    assert not mm.inside(-70.0, 37.0, poly)


def test_edges_are_sampled_not_just_cornered() -> None:
    """A swath curves in range. Four corners would cut the curve off and
    exclude vessels the sensor really did see."""
    loc = FakeLoc(24_999, 15_999)
    assert len(ds.scene_footprint(loc, 25_000, 16_000)) == 48
