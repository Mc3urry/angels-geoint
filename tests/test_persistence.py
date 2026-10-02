"""Tests for the fixed-structure filter.

The filter removes detections from the candidate list, so every mistake it
makes is either a wind turbine counted as a dark vessel or a dark vessel
deleted as a wind turbine. The tests are about telling those two apart.
"""

from __future__ import annotations

import math

import pytest

from angels.core.detectors import persistence
from angels.core.detectors.persistence import Site, SiteIndex

DATES = ["2024-04-10", "2024-05-04", "2024-05-28", "2024-06-21"]


def east(lon, lat, metres):
    return lon + metres / (111_195.0 * math.cos(math.radians(lat))), lat


def test_detections_in_the_same_place_are_one_site() -> None:
    ix = SiteIndex()
    for d in DATES:
        ix.add(-75.45, 36.90, d)
    assert len(ix.sites) == 1
    assert ix.sites[0].n_detections == 4


def test_detections_further_apart_than_the_radius_are_separate() -> None:
    ix = SiteIndex(radius_m=300)
    ix.add(-75.45, 36.90, DATES[0])
    ix.add(east(-75.45, 36.90, 900)[0], 36.90, DATES[1])
    assert len(ix.sites) == 2


def test_a_structure_seen_every_pass_is_fixed() -> None:
    ix = SiteIndex()
    for d in DATES:
        ix.add(-75.45, 36.90, d, snr=120)
    for d in DATES:
        ix.mark_searched(d, lambda lon, lat: True)
    assert ix.fixed() == ix.sites
    assert ix.sites[0].verdict() == "fixed structure"


def test_one_sighting_is_never_fixed() -> None:
    ix = SiteIndex()
    ix.add(-75.45, 36.90, DATES[0])
    for d in DATES:
        ix.mark_searched(d, lambda lon, lat: True)
    assert not ix.fixed()
    assert ix.sites[0].verdict() == "one pass only"


def _berth() -> SiteIndex:
    """Cove Point in miniature: the same 300 m on every pass, AIS explaining
    exactly one of them. A working berth."""
    ix = SiteIndex()
    for i, d in enumerate(DATES):
        ix.add(-76.30, 38.13, d, matched=(i == 1))
    for d in DATES:
        ix.mark_searched(d, lambda lon, lat: True)
    return ix


def test_a_structure_is_still_a_structure_when_a_ship_ties_up_to_it() -> None:
    """2026-09-30. The never-matched veto exempted working berths, piers and
    terminals -- the fixed structures MOST likely to have a reporting vessel
    alongside. Cove Point LNG pier: 7 of 7 passes, hit_fraction 1.00, and
    called `traffic` because AIS explained one detection there once."""
    s = _berth().sites[0]
    assert s.ever_matched
    assert s.is_fixed()
    assert s.verdict() == "fixed structure (AIS explained it at least once)"


def test_a_fixed_site_ais_explained_never_reads_as_traffic() -> None:
    """The verdict is what a reader counts. If `fixed` is true and the string
    says `traffic`, one of them is lying and nothing downstream can tell."""
    s = _berth().sites[0]
    assert s.is_fixed()
    assert "traffic" not in s.verdict()
    assert "AIS explained it at least once" in s.verdict()


def test_the_old_rule_is_still_runnable_and_still_says_traffic() -> None:
    """An amendment that cannot reproduce what it amended is not an
    amendment, it is a rewrite. `--require-never-matched` restores the
    pre-2026-09-30 candidate list exactly."""
    ix = _berth()
    rule = {"require_never_matched": True}
    assert not ix.fixed(**rule)
    assert ix.sites[0].verdict(**rule) == "traffic (AIS explained it at least once)"


def test_an_anchorage_is_still_protected_by_min_dates() -> None:
    """THE REASONING THE VETO CARRIED IS NOT GONE, it moved. An anchorage is
    occupied by DIFFERENT vessels, which do not moor in the same 300 m on
    pass after pass; a site that is detected every single time is furniture
    whatever AIS says. What still protects a genuine anchorage is MIN_DATES:
    two passes do not make a structure, matched or not."""
    ix = SiteIndex()
    for i, d in enumerate(DATES[:2]):
        ix.add(-76.30, 38.13, d, matched=(i == 0))
    for d in DATES:
        ix.mark_searched(d, lambda lon, lat: True)
    s = ix.sites[0]
    assert s.n_dates == 2
    assert not s.is_fixed()
    assert s.verdict() == "traffic (AIS explained it at least once)"


def test_the_accepted_cost_is_named() -> None:
    """A vessel that goes dark at the SAME berth on three or more passes is
    now removed with the berth. That is the loss the veto prevented and it is
    accepted, not denied -- this test exists so nobody rediscovers it as a
    surprise."""
    ix = SiteIndex()
    for d in DATES[:3]:
        ix.add(-76.30, 38.13, d)          # never matched: goes dark every time
    for d in DATES[:3]:
        ix.mark_searched(d, lambda lon, lat: True)
    s = ix.sites[0]
    assert s.is_fixed() and not s.ever_matched
    assert s.verdict() == "fixed structure"


def test_the_denominator_is_the_passes_that_searched_the_spot() -> None:
    """Seen on 3 of 3 passes that looked there beats 3 of 12 passes, most of
    which had it under the land mask. Same principle as the detection rate."""
    ix = SiteIndex()
    for d in DATES[:3]:
        ix.add(-75.45, 36.90, d)
    # only those three dates searched it; a fourth pass never looked
    for d in DATES[:3]:
        ix.mark_searched(d, lambda lon, lat: True)
    ix.mark_searched(DATES[3], lambda lon, lat: False)
    s = ix.sites[0]
    assert (s.n_dates, s.n_searched) == (3, 3)
    assert s.is_fixed()


def test_a_site_searched_often_and_seen_rarely_is_not_fixed() -> None:
    ix = SiteIndex()
    dates = [f"2024-0{i}-01" for i in range(1, 9)]
    for d in dates[:3]:
        ix.add(-75.45, 36.90, d)
    for d in dates:
        ix.mark_searched(d, lambda lon, lat: True)
    s = ix.sites[0]
    assert s.n_dates == 3 and s.n_searched == 8
    assert s.hit_fraction == pytest.approx(0.375)
    assert not s.is_fixed()


def test_a_detected_date_counts_as_searched_even_if_the_grid_disagrees() -> None:
    """A site on a cell edge can be detected and still read as unsearched,
    which would divide by zero and call it fixed on no evidence."""
    ix = SiteIndex()
    for d in DATES:
        ix.add(-75.45, 36.90, d)
    for d in DATES:
        ix.mark_searched(d, lambda lon, lat: False)
    assert ix.sites[0].n_searched == 4


def test_the_site_position_is_the_mean_not_the_first_detection() -> None:
    ix = SiteIndex()
    ix.add(-75.4500, 36.90, DATES[0])
    ix.add(-75.4520, 36.90, DATES[1])
    assert ix.sites[0].lon == pytest.approx(-75.4510)


def test_is_near_fixed_answers_for_a_new_detection() -> None:
    ix = SiteIndex()
    for d in DATES:
        ix.add(-75.45, 36.90, d)
    for d in DATES:
        ix.mark_searched(d, lambda lon, lat: True)
    assert ix.is_near_fixed(*east(-75.45, 36.90, 100))
    assert not ix.is_near_fixed(*east(-75.45, 36.90, 5000))


def test_metres_between_is_symmetric_and_scaled() -> None:
    a, b = (-75.45, 36.90), east(-75.45, 36.90, 300)
    assert persistence.metres_between(a, b) == pytest.approx(300, abs=1)
    assert persistence.metres_between(b, a) == pytest.approx(300, abs=1)


def test_the_geojson_carries_the_evidence_not_just_the_verdict() -> None:
    ix = SiteIndex()
    for d in DATES:
        ix.add(-75.45, 36.90, d, snr=88.5)
    for d in DATES:
        ix.mark_searched(d, lambda lon, lat: True)
    p = ix.sites[0].to_geojson()["properties"]
    assert p["n_dates_seen"] == 4 and p["n_dates_searched"] == 4
    assert p["fixed"] is True and p["dates"] == DATES
    assert p["max_snr"] == 88.5


def test_the_thresholds_are_stated_constants() -> None:
    assert (persistence.RADIUS_M, persistence.MIN_DATES,
            persistence.MIN_FRACTION) == (300.0, 3, 0.5)


def test_an_empty_site_has_no_rate_rather_than_a_zero() -> None:
    assert math.isnan(Site(lon=0, lat=0).hit_fraction)


# -- things that get built halfway through the year --------------------------

def test_a_structure_built_mid_year_is_still_fixed() -> None:
    """THE WIND FARM. 85 monopiles went in off Virginia Beach between
    September and December 2024. Judged against all twelve passes they look
    like a 4-of-12 site and stay in the candidate list, where they produced
    a 'concentration' 1-5 nm outside the contiguous zone."""
    ix = SiteIndex()
    year = [f"2024-{m:02d}-15" for m in range(1, 13)]
    for d in year[8:]:                      # first seen in September
        ix.add(-75.44, 36.90, d, snr=90)
    for d in year:
        ix.mark_searched(d, lambda lon, lat: True)
    s = ix.sites[0]
    assert s.first_seen == year[8]
    assert s.n_dates == 4 and s.n_searched == 12
    assert s.n_searched_since_first == 4
    assert s.hit_fraction == pytest.approx(1.0)
    assert s.hit_fraction_all_passes == pytest.approx(4 / 12)
    assert s.is_fixed()
    assert "appeared during the year" in s.verdict()


def test_a_site_seen_early_and_never_again_is_not_fixed() -> None:
    """The mirror case: the denominator must not be shrunk to whatever
    window happens to contain the sightings."""
    ix = SiteIndex()
    year = [f"2024-{m:02d}-15" for m in range(1, 13)]
    for d in year[:3]:
        ix.add(-75.44, 36.90, d)
    for d in year:
        ix.mark_searched(d, lambda lon, lat: True)
    s = ix.sites[0]
    assert s.n_searched_since_first == 12
    assert s.hit_fraction == pytest.approx(0.25)
    assert not s.is_fixed()


def test_the_two_fractions_agree_for_something_there_all_along() -> None:
    ix = SiteIndex()
    year = [f"2024-{m:02d}-15" for m in range(1, 13)]
    for d in year:
        ix.add(-75.44, 36.90, d)
    for d in year:
        ix.mark_searched(d, lambda lon, lat: True)
    s = ix.sites[0]
    assert s.hit_fraction == s.hit_fraction_all_passes == 1.0
    assert s.verdict() == "fixed structure"
