"""G3's fetch decisions, without the network.

The one that matters: a layer is written only if it is the whole layer. A
page of cameras written as "the cameras" would make every road beyond the
page read as unobserved and quiet, which is a hole with the shape of a
finding.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from scripts.fetch_road_mask import (LAYERS, MaskRefused, count_url, describe,
                                     merge_pages, page_url, parse_count,
                                     refuse_credentials, serialise, without,
                                     year_columns)


def feat(oid, x=-76.6, y=39.3, **props):
    return {"type": "Feature", "id": oid,
            "geometry": {"type": "Point", "coordinates": [x, y]},
            "properties": {"OBJECTID": oid, **props}}


def test_paging_is_ordered_and_asks_for_wgs84() -> None:
    q = parse_qs(urlparse(page_url(LAYERS["cameras"]["service"], 500)).query)
    assert q["orderByFields"] == ["OBJECTID ASC"], "unordered paging is unstable"
    assert q["resultOffset"] == ["500"] and q["outSR"] == ["4326"]
    assert int(q["resultRecordCount"][0]) < 550, "under the camera layer's cap"


def test_the_count_is_asked_for_separately() -> None:
    q = parse_qs(urlparse(count_url(LAYERS["aadt"]["service"])).query)
    assert q["returnCountOnly"] == ["true"]


@pytest.mark.parametrize("doc", [{}, {"count": "8773"}, {"count": -1},
                                 {"count": True}, [8773], None])
def test_anything_but_a_count_is_refused(doc) -> None:
    with pytest.raises(MaskRefused):
        parse_count(doc)


def test_the_whole_layer_is_accepted() -> None:
    pages = [{"features": [feat(1), feat(2)]}, {"features": [feat(3)]}]
    assert len(merge_pages(pages, 3)) == 3


def test_a_short_layer_is_refused() -> None:
    with pytest.raises(MaskRefused, match="quiet road"):
        merge_pages([{"features": [feat(1), feat(2)]}], 550)


def test_a_feature_fetched_twice_is_refused() -> None:
    """Unordered paging can return a row on two pages and skip another; the
    count would still match."""
    with pytest.raises(MaskRefused, match="twice"):
        merge_pages([{"features": [feat(1), feat(2)]},
                     {"features": [feat(2)]}], 3)


def test_a_feature_without_an_id_is_refused() -> None:
    f = feat(1)
    f["properties"].pop("OBJECTID")
    f.pop("id")
    with pytest.raises(MaskRefused, match="no OBJECTID"):
        merge_pages([{"features": [f]}], 1)


def test_camera_stream_links_are_dropped_by_name() -> None:
    f = feat(1, location="I-95 at MD 32", url="https://x/stream",
             iframe="<iframe src='https://x'>")
    out = without(f, LAYERS["cameras"]["drop"])
    assert set(out["properties"]) == {"OBJECTID", "location"}


def test_a_credential_shaped_string_refuses_the_write() -> None:
    raw = serialise([feat(1, note="key=AIza" + "A" * 35)])
    with pytest.raises(MaskRefused, match="google api key"):
        refuse_credentials(raw, "cameras")
    refuse_credentials(serialise([feat(1, location="clean")]), "cameras")


def test_the_description_counts_bad_points_and_nulls() -> None:
    bad = feat(3)
    bad["geometry"] = None
    d = describe([feat(1, CAR_AADT=None), feat(2, x=-77.0, CAR_AADT=5), bad])
    assert d["n"] == 3 and d["without_usable_point"] == 1
    assert d["extent"] == [-77.0, 39.3, -76.6, 39.3]
    assert d["nulls_by_field"] == {"CAR_AADT": 1}


def test_the_bare_aadt_year_is_not_inferred() -> None:
    y = year_columns([feat(1, AADT=1, AADT_2017=2, AADT_2018=3, AAWDT_2018=4)])
    assert y["year_columns"] == ["AADT_2017", "AADT_2018"]
    assert y["bare_column"] == "AADT"
    assert "NOT INFERRED" in y["bare_column_year"]


def test_output_is_stable_bytes() -> None:
    a = serialise([feat(1, b=1, a=2)])
    b = serialise([json.loads(json.dumps(feat(1, a=2, b=1)))])
    assert a == b, "the sha256 in the manifest must not depend on key order"


# -- camera streams for the viewer, local only (2026-10-27) -----------------

from scripts.fetch_road_mask import camera_streams, iframe_src, stream_link  # noqa: E402


@pytest.mark.parametrize("url,ok", [
    ("https://chart.maryland.gov/Video/GetVideo/abc", True),
    ("https://strmr5.sha.md.gov/rtplive/abc/playlist.m3u8", True),
    ("http://chart.maryland.gov/x", True),
    ("https://evil.example.com/chart.maryland.gov", False),
    ("https://maryland.gov.evil.com/x", False),
    ("javascript:alert(1)", False),
    ("", False), (None, False),
])
def test_only_maryland_government_hosts_are_kept(url, ok) -> None:
    got, why = stream_link(url)
    assert (got is not None) == ok
    if not ok and url:
        assert why


def test_only_the_src_is_taken_from_the_publishers_markup() -> None:
    markup = "<iframe width='560' src='https://chart.maryland.gov/v/1' onload='x()'></iframe>"
    assert iframe_src(markup) == "https://chart.maryland.gov/v/1"
    assert iframe_src("<div>no frame</div>") is None and iframe_src(None) is None


def test_streams_are_keyed_by_objectid_and_foreign_hosts_counted() -> None:
    feats = [feat(1, url="https://chart.maryland.gov/v/1",
                  iframe="<iframe src='https://chart.maryland.gov/e/1'>"),
             feat(2, url="https://cdn.example.net/v/2", iframe=""),
             feat(3, location="no links")]
    streams, rep = camera_streams(feats)
    assert streams == {"1": {"embed": "https://chart.maryland.gov/e/1",
                             "page": "https://chart.maryland.gov/v/1"}}
    assert rep["refused_by_host"] == {"cdn.example.net": 1}
    assert (rep["n_cameras"], rep["n_with_stream"]) == (3, 1)


def test_a_one_layer_fetch_keeps_the_other_layers_record() -> None:
    from scripts.fetch_road_mask import merge_manifest
    old = {"layers": {"aadt": {"n": 8773, "sha256": "a"}, "cameras": {"n": 450}}}
    new = {"_fetched_at_utc": "t2", "_partial": True,
           "layers": {"cameras": {"n": 451}}}
    m = merge_manifest(old, new)
    assert m["layers"]["aadt"] == {"n": 8773, "sha256": "a"}
    assert m["layers"]["cameras"] == {"n": 451}
    assert m["_partial"] is False, "both layers are on record"
    assert merge_manifest(None, new)["_partial"] is True


# -- CHART's own feed, because the layer's links are dead (2026-10-28) -------

from scripts.fetch_road_mask import (chart_streams, cross_check,  # noqa: E402
                                     frame_policy, parse_chart_feed)

CHART_ROW = {"id": "7a00", "lat": 39.27, "lon": -77.32, "name": "I-270 & MD 109",
             "description": "I-270 & Old Hundred Rd (MD 109)(CAM 165)",
             "cameraCategories": ["Wash. DC"], "opStatus": "OK",
             "commMode": "ONLINE",
             "publicVideoURL": "https://chart.maryland.gov/Video/GetVideo/7a00"}


def test_the_chart_feed_is_refused_unless_every_row_has_an_id_and_a_point() -> None:
    assert parse_chart_feed([CHART_ROW]) == [CHART_ROW]
    for bad in ([], {"cameras": []}, [{**CHART_ROW, "lat": None}], [{"lat": 1, "lon": 2}]):
        with pytest.raises(MaskRefused):
            parse_chart_feed(bad)


def test_chart_streams_carry_what_the_viewer_needs() -> None:
    s, rep = chart_streams([CHART_ROW, {**CHART_ROW, "id": "x9",
                                        "publicVideoURL": "https://elsewhere.example/v"}])
    assert s["7a00"]["embed"] == "https://chart.maryland.gov/Video/GetVideo/7a00"
    assert s["7a00"]["category"] == "Wash. DC" and s["7a00"]["op_status"] == "OK"
    assert s["x9"]["embed"] is None, "a foreign host is listed but never linked"
    assert rep["refused_by_host"] == {"elsewhere.example": 1}
    assert rep["n_with_link"] == 1


def test_the_layer_is_checked_against_chart_on_the_feed_id() -> None:
    layer = [feat(1, feedID="7a00"), feat(2, feedID="dead")]
    assert cross_check(layer, [CHART_ROW, {**CHART_ROW, "id": "new"}]) == {
        "layer": 2, "chart": 2, "both": 1, "layer_only": 1, "chart_only": 1}


@pytest.mark.parametrize("headers,ok", [
    ({}, True),
    ({"X-Frame-Options": "SAMEORIGIN"}, False),
    ({"X-Frame-Options": "DENY"}, False),
    ({"Content-Security-Policy": "default-src 'self'; frame-ancestors 'self'"}, False),
    ({"Content-Security-Policy": "frame-ancestors *"}, True),
    (None, True),
])
def test_the_frame_policy_is_read_not_assumed(headers, ok) -> None:
    assert frame_policy(headers)["embeddable"] is ok
