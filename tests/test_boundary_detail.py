"""The boundary-detail measurement, against geometry whose answer is known.

No network. The one impure function, `fetch`, is a urlopen and is not tested;
everything that turns a polygon into a number or a verdict is.

WHY THE FIRST TEST IS THE ONE THAT MATTERS

The tool prints a deviation in metres and G2b will set a border threshold
against it. A formula that LOOKS like geometry and is off by a factor of two
would produce a confident, plausible number -- the class this project
catalogues. So before anything else, the estimate is checked against a shape
where the true chord-to-arc gap is known exactly: a regular polygon inscribed
in a circle, where it is R(1 - cos(pi/n)). Measured 2026-10-23: 0.00 per cent
error at n=360, 0.57 per cent at a crude n=12.
"""

from __future__ import annotations

import math
from urllib.parse import parse_qs, urlparse

import pytest

from scripts.measure_boundary_detail import (EARTH_R_M, SHARP_DEG, measure,
                                             name_report, open_ring,
                                             quantiles, query_url, rings_of,
                                             segments, turn_rad, verdict)


def circle(radius_m: float, n: int, lon0: float = -77.0,
           lat0: float = 39.0) -> list[list[float]]:
    pts = []
    for k in range(n):
        a = 2 * math.pi * k / n
        lat = lat0 + math.degrees(radius_m * math.sin(a) / EARTH_R_M)
        lon = lon0 + math.degrees(radius_m * math.cos(a)
                                  / (EARTH_R_M * math.cos(math.radians(lat0))))
        pts.append([lon, lat])
    return pts + [pts[0]]


def feature(geom: dict) -> dict:
    return {"type": "Feature", "properties": {"STATE": "24", "NAME": "Maryland"},
            "geometry": geom}


# -- the formula, against an exact answer ----------------------------------

@pytest.mark.parametrize("radius_m,n,tolerance", [
    (10_000, 360, 0.001),
    (10_000, 36, 0.005),
    (50_000, 100, 0.005),
    (2_000, 12, 0.01),
])
def test_the_estimate_matches_the_exact_sagitta(radius_m, n, tolerance) -> None:
    segs, _ = segments(circle(radius_m, n))
    est = sum(s["deviation_m"] for s in segs) / len(segs)
    exact = radius_m * (1 - math.cos(math.pi / n))
    assert est == pytest.approx(exact, rel=tolerance)


def test_a_straight_line_has_no_deviation_however_long_its_segments() -> None:
    """A legally straight boundary -- Mason-Dixon -- is exact with two
    vertices a hundred kilometres apart, and must contribute nothing. A metric
    that penalised long segments as such would condemn it."""
    run = [[-77.0 + i * 0.5, 39.0] for i in range(10)]
    ring = run + [[-72.5, 39.5], [-77.0, 39.5], [-77.0, 39.0]]
    segs, _ = segments(ring)
    interior = [s for s in segs if s["turn_a_deg"] < 1 and s["turn_b_deg"] < 1]
    assert interior, "the straight run should produce segments with no turn"
    assert max(s["deviation_m"] for s in interior) < 1e-6
    assert max(s["length_m"] for s in interior) > 40_000


def test_halving_the_vertex_spacing_quarters_the_deviation() -> None:
    """Sagitta goes as L squared over R. If the tool reported a quantity that
    scaled any other way it would not be measuring discretisation."""
    def mean_dev(n: int) -> float:
        segs, _ = segments(circle(20_000, n))
        return sum(s["deviation_m"] for s in segs) / len(segs)
    assert mean_dev(200) / mean_dev(400) == pytest.approx(4.0, rel=0.01)


def test_turn_angles_read_straight_as_zero_and_reversal_as_pi() -> None:
    a, b, c = (-77.0, 39.0), (-76.9, 39.0), (-76.8, 39.0)
    assert turn_rad(a, b, c) == pytest.approx(0.0, abs=1e-9)
    assert turn_rad(a, b, a) == pytest.approx(math.pi, abs=1e-6)
    right = turn_rad(a, b, (-76.9, 39.1))
    assert math.degrees(right) == pytest.approx(90.0, abs=0.5)


# -- the geometry types this service actually returns ----------------------

def test_polygon_and_multipolygon_are_both_read() -> None:
    """The 2026-10-06 probe saw both types inside a three-feature page."""
    ring = circle(5_000, 40)
    assert len(rings_of({"type": "Polygon", "coordinates": [ring]})) == 1
    multi = {"type": "MultiPolygon", "coordinates": [[ring], [ring, ring]]}
    assert len(rings_of(multi)) == 3
    assert rings_of({"type": "Point", "coordinates": [0, 0]}) == []


def test_the_closing_vertex_is_not_a_zero_length_segment() -> None:
    pts, dupes = open_ring(circle(5_000, 40))
    assert len(pts) == 40 and dupes == 0


def test_repeated_vertices_are_dropped_and_counted() -> None:
    """A duplicate vertex has no defined turn angle, and its presence is a
    fact about how the geometry was produced, so it is reported."""
    ring = circle(5_000, 20)
    ring = ring[:5] + [ring[4], ring[4]] + ring[5:]
    pts, dupes = open_ring(ring)
    assert len(pts) == 20 and dupes == 2


def test_measure_counts_rings_vertices_and_perimeter() -> None:
    radius = 10_000
    m = measure([feature({"type": "Polygon", "coordinates": [circle(radius, 360)]})])
    assert m["rings"] == 1 and m["vertices"] == 360
    assert m["perimeter_km"] == pytest.approx(2 * math.pi * radius / 1000, rel=0.001)


# -- the trap the module docstring describes --------------------------------

def test_sharp_vertices_are_reported_both_ways_not_excluded() -> None:
    """A square's corners turn 90 degrees. They might be legal corners or
    collapsed meanders, and the tool will not decide which by rule: both
    distributions are reported so the gap is visible.

    Excluding them silently would be mechanism S -- wrong in the direction
    that makes the boundary look better, which is the direction that
    flatters a border effect."""
    side = 0.1
    square = [[-77, 39], [-77 + side, 39], [-77 + side, 39 + side],
              [-77, 39 + side], [-77, 39]]
    m = measure([feature({"type": "Polygon", "coordinates": [square]})])
    assert m["segments_touching_sharp_vertex"] == 4
    assert m["deviation_m_all"]["max"] > 0
    assert m["deviation_m_without_sharp"] is None, (
        "every segment of a square touches a corner, so excluding them leaves "
        "nothing -- and nothing must read as nothing, not as zero")


def test_the_worst_segments_carry_coordinates_a_person_can_look_up() -> None:
    m = measure([feature({"type": "Polygon", "coordinates": [circle(3_000, 8)]})])
    w = m["worst"][0]
    assert {"deviation_m", "length_m", "turn_a_deg", "turn_b_deg",
            "mid_lat", "mid_lon"} <= set(w)
    assert 38 < w["mid_lat"] < 40 and -78 < w["mid_lon"] < -76


def test_an_empty_distribution_is_none_not_zeros() -> None:
    assert quantiles([]) is None
    assert quantiles([3.0])["median"] == 3.0


# -- the verdict ------------------------------------------------------------

def _m(p99_all: float, p99_soft: float | None, sharp: int = 0) -> dict:
    allq = {"n": 100, "min": 0, "p10": 0, "median": p99_all / 10,
            "p90": p99_all / 2, "p99": p99_all, "max": p99_all * 2}
    soft = None if p99_soft is None else dict(allq, p99=p99_soft)
    return {"deviation_m_all": allq, "deviation_m_without_sharp": soft,
            "segments_touching_sharp_vertex": sharp, "sharp_deg": SHARP_DEG}


def test_five_situations_give_five_verdicts() -> None:
    """Rule 13. The count catches a swallowed branch."""
    said = [
        verdict({}, 500),
        verdict(_m(20, 20), 500),
        verdict(_m(300, 300), 500),
        verdict(_m(900, 200, sharp=40), 500),
        verdict(_m(900, 800), 500),
    ]
    assert len(set(said)) == 5, said


def test_a_coarse_result_caused_only_by_sharp_vertices_says_so() -> None:
    """That is exactly the corner-or-meander question, and the sentence has to
    send the reader to the worst list rather than settle it by rule."""
    s = verdict(_m(900, 200, sharp=40), 500)
    assert "ONLY because of sharp vertices" in s
    assert "on a map" in s


def test_the_verdict_is_judged_on_the_conservative_distribution() -> None:
    s = verdict(_m(900, 800), 500)
    assert s.startswith("too coarse")


# -- the request ------------------------------------------------------------

def test_the_query_asks_the_server_for_nothing_that_would_simplify() -> None:
    """maxAllowableOffset would ask ArcGIS to generalise the geometry, which
    is the property being measured. outSR is stated rather than assumed."""
    q = parse_qs(urlparse(query_url("24")).query)
    assert "maxAllowableOffset" not in q
    assert q["outSR"] == ["4326"]
    assert q["where"] == ["STATE='24'"], "FIPS stays a zero-padded string"
    assert q["returnGeometry"] == ["true"] and q["f"] == ["geojson"]


# -- the 28-character NAME --------------------------------------------------

def test_a_padded_name_is_detected() -> None:
    """Padded, "Maryland" will not equal it in a join, and the failure is a
    join that matches nothing rather than one that raises."""
    r = name_report("Maryland" + " " * 20)
    assert r["length"] == 28 and r["stripped_length"] == 8 and r["padded"]
    assert not name_report("Maryland")["padded"]
    assert name_report(None)["length"] == 0
