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
