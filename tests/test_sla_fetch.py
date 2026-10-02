"""Tests for what the 3 nm fetch REPORTS, not for the fetch.

Nothing here touches the network. The subject is `describe_sla`, which
exists because of a specific failure: two SLA sources in this project
returned HTTP 400/404 for eighteen days and were read as "the download is
flaky", when in fact they were URLs for services that have never existed.
The real service is BOEM's, and it is a general boundary layer -- so the
next version of that mistake is a file named submerged_lands_act_3nm.geojson
that contains something else, or two thirds of the line, with nothing
downstream able to tell because the analysis identifies a line by filename.
"""

from __future__ import annotations

import json

from scripts.fetch_limits import describe_sla


def write(tmp_path, doc):
    p = tmp_path / "submerged_lands_act_3nm.geojson"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def line(name="Submerged Lands Act Boundary"):
    return {"type": "Feature",
            "geometry": {"type": "LineString",
                         "coordinates": [[-75.0, 36.0], [-75.0, 37.0]]},
            "properties": {"BDRY_NAME_TEXT": name}}


def test_a_good_response_names_the_boundaries_it_contains(tmp_path) -> None:
    ok, msg = describe_sla(write(tmp_path, {
        "type": "FeatureCollection", "features": [line(), line()]}))
    assert ok
    assert "2 features" in msg
    assert "Submerged Lands Act Boundary" in msg
    assert "LineString" in msg


def test_more_than_one_boundary_kind_is_reported_not_hidden(tmp_path) -> None:
    """The whole point. If the layer hands back lateral state boundaries as
    well as the seaward line, the reader has to see that before the file is
    used as "the 3 nm line"."""
    ok, msg = describe_sla(write(tmp_path, {
        "type": "FeatureCollection",
        "features": [line(), line("Unofficial State Lateral Boundary")]}))
    assert ok
    assert "Unofficial State Lateral Boundary" in msg


def test_an_arcgis_error_object_with_http_200_is_a_failure(tmp_path) -> None:
    ok, msg = describe_sla(write(tmp_path, {
        "error": {"code": 400, "message": "Unable to complete operation."}}))
    assert not ok
    assert "400" in msg and "Unable to complete operation" in msg


def test_valid_geojson_of_nothing_is_a_failure(tmp_path) -> None:
    ok, msg = describe_sla(write(tmp_path, {
        "type": "FeatureCollection", "features": []}))
    assert not ok
    assert "0 features" in msg


def test_a_truncated_response_is_a_failure_and_says_why(tmp_path) -> None:
    """ArcGIS caps a query at maxRecordCount and announces it in a flag most
    callers never read. A partial line read as a whole line moves every band
    assignment, which is the one error this project cannot absorb."""
    ok, msg = describe_sla(write(tmp_path, {
        "type": "FeatureCollection", "features": [line()],
        "exceededTransferLimit": True}))
    assert not ok
    assert "TRUNCATED" in msg
    assert "paging" in msg


def test_truncation_is_caught_when_the_flag_sits_under_properties(tmp_path) -> None:
    ok, _ = describe_sla(write(tmp_path, {
        "type": "FeatureCollection", "features": [line()],
        "properties": {"exceededTransferLimit": True}}))
    assert not ok


def test_something_that_is_not_json_is_a_failure(tmp_path) -> None:
    p = tmp_path / "submerged_lands_act_3nm.geojson"
    p.write_text("<html>Service Unavailable</html>", encoding="utf-8")
    ok, msg = describe_sla(p)
    assert not ok
    assert "not JSON" in msg
