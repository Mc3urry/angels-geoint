"""Tests for the collection footprints and the quota arithmetic.

These guard a failure mode with no error message. Overspending OpenSky credits
does not raise -- every poll after the bucket empties returns 429, so the
archive grows a hole at roughly the same hour every day, which looks exactly
like a diurnal pattern in the traffic. By the time anyone notices, months of
data carry an artefact that no analysis can distinguish from a finding.

So the budget is asserted here, not remembered.
"""

from __future__ import annotations

import pytest

from angels.config import (
    AOI_AIR,
    AOI_CONUS,
    AOIS,
    REGION,
    box_area_sqdeg,
    contains,
    daily_credits,
    opensky_credits,
)

DAILY_ALLOWANCE = 4000          # a registered, non-feeding account


# -- the credit tiers ------------------------------------------------------

@pytest.mark.parametrize("area_box,expected", [
    ((0.0, 0.0, 5.0, 5.0), 1),        #   25 sq deg, on the boundary
    ((0.0, 0.0, 4.0, 4.0), 1),        #   16
    ((0.0, 0.0, 10.0, 10.0), 2),      #  100, on the boundary
    ((0.0, 0.0, 6.0, 6.0), 2),        #   36
    ((0.0, 0.0, 20.0, 20.0), 3),      #  400, on the boundary
    ((0.0, 0.0, 15.0, 15.0), 3),      #  225
    ((0.0, 0.0, 30.0, 30.0), 4),      #  900
])
def test_credit_tiers(area_box, expected) -> None:
    assert opensky_credits(area_box) == expected


def test_billing_area_ignores_latitude() -> None:
    """OpenSky bills in raw square degrees with no cosine correction.

    box_area_km2 does apply one, correctly, because it answers a physical
    question. Using it for pricing would quietly underestimate the cost of
    every northern box -- so the two must stay separate functions.
    """
    equator = (0.0, 0.0, 10.0, 10.0)
    arctic = (0.0, 60.0, 10.0, 70.0)
    assert box_area_sqdeg(equator) == box_area_sqdeg(arctic)
    assert opensky_credits(equator) == opensky_credits(arctic)


# -- the two footprints ----------------------------------------------------

def test_the_metro_box_is_the_cheapest_tier() -> None:
    assert opensky_credits(AOI_AIR) == 1


def test_the_national_box_costs_four_not_six_hundred() -> None:
    """The fact the whole expansion rests on.

    CONUS is roughly 600x the area of the DC box and costs 4x as much,
    because the tiers are coarse and top out. If OpenSky ever moves to
    area-proportional pricing this test fails and the two-collector design
    needs rethinking rather than quietly bankrupting the account.
    """
    assert box_area_sqdeg(AOI_CONUS) / box_area_sqdeg(AOI_AIR) > 100
    assert opensky_credits(AOI_CONUS) == 4


def test_both_collectors_together_fit_the_daily_allowance() -> None:
    spend = sum(daily_credits(a["box"], a["interval_s"]) for a in AOIS.values())
    assert spend < DAILY_ALLOWANCE, (
        f"the configured collectors spend {spend} credits/day against an "
        f"allowance of {DAILY_ALLOWANCE}. Polls after the bucket empties get "
        f"429s, leaving a hole at the same hour every day."
    )


def test_the_budget_has_real_headroom() -> None:
    """Not merely under the line.

    Ad-hoc queries, a restart that re-polls, and the API's /live endpoint all
    draw on the same bucket. A configuration that fits with ten credits to
    spare is one that fails the first afternoon you use the viewer.
    """
    spend = sum(daily_credits(a["box"], a["interval_s"]) for a in AOIS.values())
    assert DAILY_ALLOWANCE - spend >= 300


# -- registry integrity ----------------------------------------------------

def test_every_footprint_is_fully_specified() -> None:
    for name, a in AOIS.items():
        for key in ("box", "dataset", "collector", "label",
                    "interval_s", "max_gap_s"):
            assert key in a, f"AOIS[{name!r}] is missing {key!r}"


def test_datasets_and_collectors_are_unique() -> None:
    """Two footprints sharing a dataset would interleave twentyfold-different
    sample rates into one archive; sharing a collector name would make the
    lock refuse to run them side by side, and merge their uptime histories."""
    assert len({a["dataset"] for a in AOIS.values()}) == len(AOIS)
    assert len({a["collector"] for a in AOIS.values()}) == len(AOIS)


def test_legacy_names_are_preserved() -> None:
    """The air footprint must keep writing where it always has.

    Renaming it would orphan the existing Parquet archive and every heartbeat
    recorded so far -- and heartbeats cannot be regenerated, because they are
    assertions about moments that have passed.
    """
    assert AOIS["air"]["dataset"] == "aviation"
    assert AOIS["air"]["collector"] == "aviation"


def test_gap_threshold_scales_with_sample_rate() -> None:
    """max_gap_s decides where one track ends and the next begins, and is only
    meaningful relative to the polling interval. Too tight and every dropped
    poll splits a track, filling the archive with fragments that the detectors
    read as evidence."""
    for name, a in AOIS.items():
        ratio = a["max_gap_s"] / a["interval_s"]
        assert ratio >= 3, (
            f"{name}: max_gap_s is only {ratio:.1f} polls, so a single "
            f"missed poll would split tracks"
        )


# -- geography -------------------------------------------------------------

def test_the_study_region_sits_inside_the_national_box() -> None:
    """The national feed has to be a superset, or the two are not comparable
    and the metro findings cannot be checked against the national baseline."""
    lomin, lamin, lomax, lamax = REGION
    for lat, lon in ((lamin, lomin), (lamax, lomax)):
        assert contains(AOI_CONUS, lat, lon)


def test_boxes_are_well_formed() -> None:
    for name, a in AOIS.items():
        lomin, lamin, lomax, lamax = a["box"]
        assert lomin < lomax, f"{name}: longitudes inverted"
        assert lamin < lamax, f"{name}: latitudes inverted"
        assert -180 <= lomin and lomax <= 180
        assert -90 <= lamin and lamax <= 90
