"""Tests for the limit-line geometry.

Distance to a legal line is what the research question is asked in, so the
failure modes here are the ones that would move a candidate into the wrong
band without anything looking wrong: vertex distance instead of segment
distance, a grid search that stops one ring too early, a shapefile record
read at the wrong offset.
"""

from __future__ import annotations

import json
import struct

import pytest

from angels.adapters.maritime import limits
from angels.adapters.maritime.limits import LineSet

NM_M = 1852.0


def meridian(lon=-75.0, lat0=36.0, lat1=40.0, step=2.0):
    """A north-south line with deliberately SPARSE vertices."""
    n = int((lat1 - lat0) / step)
    return [[(lon, lat0 + i * step) for i in range(n + 1)]]


# -- distance ---------------------------------------------------------------

def test_distance_is_to_the_segment_not_the_nearest_vertex() -> None:
    """The vertices here are 222 km apart. A point beside the middle of the
    line is 8.9 km from the LINE and 111 km from either vertex."""
    ls = LineSet(meridian())
    d = ls.distance_m(-74.9, 37.0)
    assert d == pytest.approx(8880, rel=0.02)


def test_a_point_on_the_line_is_at_zero() -> None:
    assert LineSet(meridian()).distance_m(-75.0, 37.3) == pytest.approx(0, abs=1)


def test_distance_grows_the_right_way_in_metres() -> None:
    ls = LineSet(meridian())
    near, far = ls.distance_m(-74.95, 37.0), ls.distance_m(-74.5, 37.0)
    assert near < far
    assert far == pytest.approx(44_400, rel=0.02)


def test_the_search_does_not_stop_one_ring_too_early() -> None:
    """A grid search that returns at the first cell with any segment reports
    a distance that is merely a little too large -- silent, and enough to put
    a candidate in the wrong band."""
    ls = LineSet([[(-75.0, 37.0), (-75.0, 37.001)],      # tiny, far
                  [(-74.0, 36.0), (-74.0, 40.0)]])       # long, near
    assert ls.distance_m(-74.05, 38.0) == pytest.approx(4380, rel=0.05)


def test_beyond_the_search_radius_is_inf_not_zero() -> None:
    """inf must reach the caller: treating "not found" as 0 would put every
    far-offshore point on top of a boundary."""
    assert LineSet(meridian()).distance_m(-60.0, 37.0) == float("inf")


def test_an_empty_set_is_inf_everywhere() -> None:
    assert LineSet([]).distance_m(-75.0, 37.0) == float("inf")
    assert len(LineSet([])) == 0


def test_a_long_segment_is_found_from_beside_its_middle() -> None:
    """A segment spanning many index cells has to be registered in all of
    them, or a query in the middle finds an empty cell and misses it."""
    ls = LineSet([[(-77.0, 37.0), (-71.0, 37.0)]])
    assert ls.distance_m(-74.0, 37.05) == pytest.approx(5560, rel=0.02)


def test_twelve_nautical_miles_is_where_it_should_be() -> None:
    """The unit the bands are stated in."""
    ls = LineSet(meridian())
    lon = -75.0 + (12 * NM_M) / (111_195.0 * 0.8)      # ~12 nm east at 37N
    assert ls.distance_m(lon, 37.0) / NM_M == pytest.approx(12, rel=0.05)


# -- reading ----------------------------------------------------------------

def test_geojson_lines_and_polygons_both_become_runs(tmp_path) -> None:
    doc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {
            "type": "LineString", "coordinates": [[-75, 36], [-75, 37]]}},
        {"type": "Feature", "geometry": {
            "type": "MultiLineString",
            "coordinates": [[[-74, 36], [-74, 37]], [[-73, 36], [-73, 37]]]}},
        {"type": "Feature", "geometry": {
            "type": "Polygon",
            "coordinates": [[[-72, 36], [-72, 37], [-71, 37], [-72, 36]]]}},
    ]}
    p = tmp_path / "limits.geojson"
    p.write_text(json.dumps(doc))
    runs = limits.read_lines(p)
    assert len(runs) == 4
    assert runs[0] == [(-75.0, 36.0), (-75.0, 37.0)]


def test_a_third_coordinate_does_not_break_the_reader(tmp_path) -> None:
    """NOAA's exports carry Z values. Unpacking x, y only would raise."""
    p = tmp_path / "z.geojson"
    p.write_text(json.dumps({"type": "Feature", "geometry": {
        "type": "LineString", "coordinates": [[-75, 36, 0], [-75, 37, 0]]}}))
    assert limits.read_lines(p)[0] == [(-75.0, 36.0), (-75.0, 37.0)]


def _shp(records):
    """A minimal polyline shapefile, built to the spec being parsed."""
    body = b""
    for i, run in enumerate(records, 1):
        xs = [p[0] for p in run]
        ys = [p[1] for p in run]
        shape = struct.pack("<i", 3)
        shape += struct.pack("<4d", min(xs), min(ys), max(xs), max(ys))
        shape += struct.pack("<ii", 1, len(run))
        shape += struct.pack("<i", 0)
        for x, y in run:
            shape += struct.pack("<2d", x, y)
        body += struct.pack(">ii", i, len(shape) // 2) + shape
    header = struct.pack(">i", 9994) + b"\x00" * 20
    header += struct.pack(">i", (100 + len(body)) // 2)
    header += struct.pack("<ii", 1000, 3) + b"\x00" * 64
    return header + body


def test_a_shapefile_is_read_without_geopandas(tmp_path) -> None:
    p = tmp_path / "limit.shp"
    p.write_bytes(_shp([[(-75.0, 36.0), (-75.0, 37.0)],
                        [(-74.0, 36.0), (-74.0, 36.5), (-74.0, 37.0)]]))
    runs = limits.read_shapefile(p)
    assert [len(r) for r in runs] == [2, 3]
    assert runs[1][1] == (-74.0, 36.5)


def test_something_that_is_not_a_shapefile_is_refused(tmp_path) -> None:
    p = tmp_path / "nope.shp"
    p.write_bytes(b"not a shapefile at all" + b"\x00" * 200)
    with pytest.raises(ValueError, match="not a shapefile"):
        limits.read_shapefile(p)


def test_an_unknown_extension_says_what_it_wants(tmp_path) -> None:
    p = tmp_path / "limits.kml"
    p.write_text("<kml/>")
    with pytest.raises(ValueError, match="geojson"):
        limits.read_lines(p)


def test_from_file_names_itself_after_the_file(tmp_path) -> None:
    p = tmp_path / "territorial_sea_12nm.geojson"
    p.write_text(json.dumps({"type": "Feature", "geometry": {
        "type": "LineString", "coordinates": [[-75, 36], [-75, 37]]}}))
    assert LineSet.from_file(p).name == "territorial_sea_12nm"
