"""The same-agency comparison's verdicts, against fleets whose answer is known.

The one that matters most: two silent feeds are UNDECIDED, never DIFFERENT.
An empty comparison on a Sunday night would otherwise turn "we could not
tell" into "keep both", and VRE's trains would be counted twice from Monday.
"""

from __future__ import annotations

import json

from scripts.compare_same_agency import (DIFFERENT, SAME_BY_ID, SAME_BY_LABEL,
                                         SAME_BY_POSITION, UNDECIDED_NO_ANSWER,
                                         UNDECIDED_ONE_EMPTY, UNDECIDED_SILENT,
                                         VERDICTS, compare, metres,
                                         provisional_pairs, urls_from, verdict)
from scripts.fetch_vehicles import SELECTION


def v(vid: str = "", label: str = "", lat: float = 42.1, lon: float = -72.6):
    return {"vehicle_id": vid, "label": label, "lat": lat, "lon": lon}


def test_the_pairs_are_read_from_g2b_not_restated() -> None:
    pairs = provisional_pairs()
    assert ("mdb-1734", "tld-1127-vp", "tld-1127-vp") in pairs
    assert ("mdb-2670", "tld-4679-vp", "tld-4679-vp") in pairs
    assert all(not a.startswith("mdb-185") for a, _, _ in pairs), (
        "WMATA bus and rail were decided 'both' and are not in question")


def test_every_feed_in_a_pair_has_a_url_in_the_g2b_artefact() -> None:
    by_id = urls_from(json.loads(SELECTION.read_text(encoding="utf-8")))
    for a, b, _ in provisional_pairs():
        assert by_id[a]["url"].startswith("http")
        assert by_id[b]["url"].startswith("http")


def test_shared_ids_win() -> None:
    a = [v("1"), v("2"), v("3")]
    b = [v("1"), v("2", lat=40.0)]
    assert verdict(compare(a, b)) == SAME_BY_ID


def test_two_vendors_numbering_differently_still_share_labels() -> None:
    a = [v("swift-1", "1401"), v("swift-2", "1402")]
    b = [v("ip-9", "1401", lat=41.0), v("ip-8", "1402", lat=41.1)]
    assert verdict(compare(a, b)) == SAME_BY_LABEL


def test_unlabelled_fleets_can_still_match_by_position() -> None:
    a = [v("x1", lat=42.100, lon=-72.600), v("x2", lat=42.200, lon=-72.500)]
    b = [v("y1", lat=42.1005, lon=-72.6005), v("y2", lat=42.2003, lon=-72.5)]
    c = compare(a, b)
    assert c["near_within_m"] == 2
    assert verdict(c) == SAME_BY_POSITION


def test_different_fleets_are_different() -> None:
    a = [v("1", "A", lat=42.0), v("2", "B", lat=42.5)]
    b = [v("9", "Z", lat=38.9), v("8", "Y", lat=39.0)]
    assert verdict(compare(a, b)) == DIFFERENT


def test_silence_is_undecided_and_never_different() -> None:
    assert verdict(compare([], [])) == UNDECIDED_SILENT
    assert verdict(compare([v("1")], [])) == UNDECIDED_ONE_EMPTY
    assert verdict(compare([v("1")], [v("1")]), answered=False) == \
        UNDECIDED_NO_ANSWER


def test_seven_verdicts_are_seven_strings() -> None:
    assert len(set(VERDICTS)) == len(VERDICTS) == 7


def test_the_distance_is_metres() -> None:
    # one thousandth of a degree of latitude is about 111 m
    assert 110 < metres(42.0, -72.0, 42.001, -72.0) < 112
