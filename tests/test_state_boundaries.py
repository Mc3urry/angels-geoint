"""The state-boundary fetch's decisions, without the network.

What is tested is everything that decides whether a boundary set is complete,
correct and honestly described: which states are fetched and which skipped,
whether a response is accepted, and whether the server really simplified.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from scripts.fetch_state_boundaries import (GENERALISE_DEG,
                                            GENERALISE_M_UPPER_BOUND,
                                            MARYLAND_FULL_RES_VERTICES,
                                            NO_LAND_NEIGHBOUR, BoundaryRefused,
                                            check_plan, extent_url,
                                            generalisation_verdict,
                                            parse_extent, plan, state_url,
                                            validate_state, vertex_count)

LISTED = [{"STATE": f, "NAME": n} for f, n in (
    ("01", "Alabama"), ("02", "Alaska"), ("11", "District of Columbia"),
    ("15", "Hawaii"), ("24", "Maryland"), ("51", "Virginia"),
    ("72", "Puerto Rico"), ("78", "United States Virgin Islands"))]


def gj(fips="24", n=5, page=False):
    ring = [[-77 + i * 0.01, 39.0] for i in range(n - 1)] + [[-77, 39.0]]
    d = {"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {"STATE": fips, "NAME": "x"},
        "geometry": {"type": "Polygon", "coordinates": [ring]}}]}
    if page:
        d["exceededTransferLimit"] = True
    return d


def test_states_with_no_land_neighbour_are_skipped_by_name() -> None:
    fetch, skip = plan(LISTED)
    fetched = {s["STATE"] for s in fetch}
    assert fetched == {"01", "11", "24", "51"}, "DC is in: it borders MD and VA"
    assert set(skip) == {"02", "15", "72", "78"}
    assert all(skip[f] == NO_LAND_NEIGHBOUR[f] for f in skip)


def test_every_listed_state_is_either_fetched_or_skipped() -> None:
    fetch, skip = plan(LISTED)
    check_plan(LISTED, fetch, skip)
    assert len(fetch) + len(skip) == len(LISTED)


def test_only_marks_everything_else_as_partial() -> None:
    """A test fetch must never be mistaken for a complete set."""
    fetch, skip = plan(LISTED, only=("24",))
    assert [s["STATE"] for s in fetch] == ["24"]
    assert "PARTIAL" in skip["51"]


def test_a_fips_code_that_lost_its_leading_zero_is_refused() -> None:
    """If the service ever returned STATE as a number, "01" becomes "1" and
    every join on it matches nothing. Refuse before writing anything."""
    bad = [{"STATE": "1", "NAME": "Alabama"}]
    fetch, skip = plan(bad)
    with pytest.raises(BoundaryRefused, match="zero-padded"):
        check_plan(bad, fetch, skip)


def test_the_request_states_its_spatial_reference_and_its_simplification() -> None:
    """maxAllowableOffset is in the units of outSR. Stated as 4326 it means
    degrees; left to a projected default, 0.001 would be a millimetre."""
    q = parse_qs(urlparse(state_url("24")).query)
    assert q["outSR"] == ["4326"]
    assert float(q["maxAllowableOffset"][0]) == GENERALISE_DEG
    assert q["where"] == ["STATE='24'"]


def test_the_tolerance_in_metres_is_an_upper_bound() -> None:
    assert GENERALISE_M_UPPER_BOUND == pytest.approx(111.3, abs=0.1)


@pytest.mark.parametrize("doc,match", [
    (gj(page=True), "PAGE"),
    ({"features": []}, "expected 1 feature"),
    (gj(fips="51"), "asked for STATE"),
    ({"features": [{"properties": {"STATE": "24"}, "geometry": None}]}, "no geometry"),
])
def test_an_incomplete_or_wrong_response_is_refused(doc, match) -> None:
    with pytest.raises(BoundaryRefused, match=match):
        validate_state("24", doc)


def test_a_good_response_is_accepted() -> None:
    validate_state("24", gj())


def test_vertices_are_counted_in_both_geometry_types() -> None:
    ring = [[0, 0], [1, 0], [1, 1], [0, 0]]
    multi = {"features": [{"geometry": {"type": "MultiPolygon",
                                        "coordinates": [[ring], [ring, ring]]}}]}
    assert vertex_count(multi) == 12
    assert vertex_count(gj(n=5)) == 5


def test_whether_the_server_simplified_is_checked_not_assumed() -> None:
    """Maryland's full-resolution count was measured on 2026-10-23. A
    simplified fetch should come back well under it; one that does not means
    the server ignored the request, and the manifest must not claim
    otherwise."""
    ignored, why = generalisation_verdict({"24": MARYLAND_FULL_RES_VERTICES - 50})
    assert ignored is False and "did NOT simplify" in why
    done, why = generalisation_verdict({"24": 3_000})
    assert done is True and "simplified as asked" in why


def test_the_extent_request_asks_for_four_numbers_in_wgs84() -> None:
    q = parse_qs(urlparse(extent_url("02")).query)
    assert q["returnExtentOnly"] == ["true"]
    assert q["outSR"] == ["4326"]
    assert q["where"] == ["STATE='02'"]


@pytest.mark.parametrize("doc,expect", [
    ({"extent": {"xmin": -179.2, "ymin": 51.2, "xmax": 179.8, "ymax": 71.4}},
     [-179.2, 51.2, 179.8, 71.4]),
    ({"extent": None}, None),
    ({}, None),
    ({"extent": {"xmin": "NaN", "ymin": 0, "xmax": 1, "ymax": 1}}, None),
    ({"extent": {"xmin": 5, "ymin": 0, "xmax": 1, "ymax": 1}}, None),     # inverted
    ({"extent": {"xmin": 0, "ymin": 0}}, None),                           # incomplete
])
def test_an_unusable_extent_is_none_and_never_a_guess(doc, expect) -> None:
    """None leaves a feed flagged rather than explained, which over-reports;
    a made-up extent would explain away a real gap, which is the unsafe side."""
    assert parse_extent(doc) == expect


def test_without_maryland_the_answer_is_unknown_not_yes() -> None:
    v, why = generalisation_verdict({"51": 4_000})
    assert v is None and "unknown" in why
