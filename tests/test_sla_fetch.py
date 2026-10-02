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


# -- the fetch envelope ------------------------------------------------------
#
# Added 2026-10-02. Two days of HTTP 500 from BOEM were recorded here as "the
# publisher is down". The publisher was not down: a bare returnCountOnly
# query answers {"count":5746} instantly. The request was asking for all
# 5,746 national polylines with geometry in one GeoJSON call, and the server
# answered 500 rather than a clean exceededTransferLimit. Clipped to this
# study area the same query returns 427.

from scripts.fetch_limits import (SLA_BBOX, SLA_ENVELOPE, SLA_MARGIN_DEG,
                                  SLA_SOURCES, stamp_envelope)
from angels.config import AOI_SEA


def test_the_envelope_is_the_aoi_plus_the_margin_limit_sets_uses() -> None:
    """Hardcoding the numbers would let the clip drift away from the AOI it
    is supposed to cover. `limit_sets` trims with a 2 degree margin; the
    fetch must not be narrower than what the analysis will look at."""
    lo0, la0, lo1, la1 = AOI_SEA
    assert SLA_BBOX == (lo0 - SLA_MARGIN_DEG, la0 - SLA_MARGIN_DEG,
                        lo1 + SLA_MARGIN_DEG, la1 + SLA_MARGIN_DEG)
    assert SLA_MARGIN_DEG == 2.0


def test_the_sla_query_is_clipped_and_carries_no_outsr() -> None:
    """The two things that made it a 500: the whole national layer, and an
    outSR that GeoJSON does not need."""
    url = next(u for name, u, _ in SLA_SOURCES if name == "boem-sla-geojson")
    assert "esriGeometryEnvelope" in url
    assert SLA_ENVELOPE in url
    assert "esriSpatialRelIntersects" in url
    assert "outSR" not in url
    assert "outFields=*" not in url


def test_a_clipped_file_says_so_in_its_own_properties(tmp_path) -> None:
    """A limit file clipped to one study area and reused for another would
    put the line's edge wherever the clip stopped. Nothing downstream reads
    this; a person does, which is the point."""
    p = tmp_path / "submerged_lands_act_3nm.geojson"
    p.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [line("Submerged Lands Act Boundary")]}), encoding="utf-8")
    stamp_envelope(p)
    props = json.loads(p.read_text(encoding="utf-8"))["properties"]
    assert props["_fetch_envelope_lonlat"] == list(SLA_BBOX)
    assert props["_fetch_aoi"] == list(AOI_SEA)
    assert "NOT the national extent" in props["_fetch_note"]


def test_stamping_does_not_disturb_the_features(tmp_path) -> None:
    p = tmp_path / "submerged_lands_act_3nm.geojson"
    before = {"type": "FeatureCollection",
              "features": [line(), line("Submerged Lands Act Boundary")]}
    p.write_text(json.dumps(before), encoding="utf-8")
    stamp_envelope(p)
    after = json.loads(p.read_text(encoding="utf-8"))
    assert after["features"] == before["features"]
    assert describe_sla(p)[0]
