"""Tests for the observation-channel probe.

The probe cannot be tested against the live networks in CI, and should not be
-- a test that needs the internet fails for reasons that have nothing to do
with the code. What CAN be tested is everything that decides what the answer
MEANS, and that is where the risk actually sits: this script exists to produce
a go/no-go verdict on the central question of the project, so a
misclassification here would send the whole thesis down the wrong road while
looking perfectly confident.

The first run of this probe got a 403 from a proxy and printed "NO independent
observation channel found over this region" -- a decisive negative answer to a
question it had not asked. test_unreachable_is_not_the_same_as_empty below is
that bug, nailed down.
"""

from __future__ import annotations

import math

import pytest

from angels.config import AOI_AIR
from scripts.probe_observations import (
    NETWORKS,
    OBSERVATION_TYPES,
    REPORT_TYPES,
    circle_for,
    classify,
    in_box,
    caveats,
    report,
)


# -- the classification, which is the whole point --------------------------

@pytest.mark.parametrize("t", [
    "adsb_icao",        # the aircraft broadcasting its own position
    "adsb_icao_nt",
    "adsb_other",
    "adsr_icao",        # relayed, but still the aircraft's own claim
    "adsr_other",
    "adsc",
])
def test_self_reported_positions_are_reports(t) -> None:
    assert classify(t) == "report"


@pytest.mark.parametrize("t", [
    "mlat",             # solved from receiver geometry, not from the aircraft
    "tisb_icao",        # secondary radar, rebroadcast
    "tisb_trackfile",   # primary radar -- target may carry nothing at all
    "tisb_other",
])
def test_independently_sensed_positions_are_observations(t) -> None:
    assert classify(t) == "observation"


def test_relayed_adsb_is_not_an_observation() -> None:
    """The subtle one, and the one worth getting wrong only once.

    ADS-R is an ADS-B message relayed across datalinks by ground infrastructure.
    It ARRIVES the same way TIS-B does, from a ground station, which makes it
    look like an observation. It is not: the position inside it is still the
    aircraft's own assertion, merely forwarded. Counting it as independent
    would mean subtracting a report from itself and finding, reliably, no
    discrepancy anywhere.
    """
    assert classify("adsr_icao") == "report"
    assert classify("adsr_other") == "report"


def test_mode_s_carries_no_position() -> None:
    """Present in the feed, useless as either a report or an observation --
    it proves the aircraft exists without saying where it is."""
    assert classify("mode_s") == "no position"


def test_the_two_sets_do_not_overlap() -> None:
    assert not (REPORT_TYPES & OBSERVATION_TYPES)


def test_an_unknown_type_is_not_silently_counted() -> None:
    """readsb adds types over time. A new one must not be quietly absorbed
    into either bucket -- it should show up as unknown and get looked at."""
    assert classify("some_future_type") == "unknown"


# -- geometry --------------------------------------------------------------

def test_the_circle_covers_the_whole_box() -> None:
    """These APIs are radial and the config is rectangular. The circle has to
    CIRCUMSCRIBE the box; one that fits inside would miss the corners and
    undercount in a way no error would reveal."""
    clat, clon, nm = circle_for(AOI_AIR)
    lomin, lamin, lomax, lamax = AOI_AIR
    radius_km = nm * 1.852

    for lat, lon in ((lamin, lomin), (lamin, lomax),
                     (lamax, lomin), (lamax, lomax)):
        dy = (lat - clat) * 110.57
        dx = (lon - clon) * 111.32 * math.cos(math.radians(clat))
        assert math.hypot(dx, dy) <= radius_km + 0.001, \
            f"corner {lat},{lon} falls outside the probe circle"


def test_the_circle_is_centred_on_the_box() -> None:
    clat, clon, _ = circle_for(AOI_AIR)
    assert clon == pytest.approx((AOI_AIR[0] + AOI_AIR[2]) / 2)
    assert clat == pytest.approx((AOI_AIR[1] + AOI_AIR[3]) / 2)


def test_results_outside_the_study_box_are_filtered_out() -> None:
    """The circle necessarily returns aircraft outside AOI_AIR. Counting them
    would produce a number over a footprint that matches nothing else in the
    project, and so could not be compared to anything."""
    assert in_box({"lat": 38.9, "lon": -77.0}, AOI_AIR)
    assert not in_box({"lat": 41.9, "lon": -87.6}, AOI_AIR)     # Chicago


def test_aircraft_without_a_position_are_not_counted_as_inside() -> None:
    """mode_s targets have no lat/lon at all. Treating a missing position as
    inside the box would inflate every count."""
    assert not in_box({}, AOI_AIR)
    assert not in_box({"lat": None, "lon": None}, AOI_AIR)
    assert not in_box({"lat": "n/a", "lon": "n/a"}, AOI_AIR)


# -- the verdict logic -----------------------------------------------------

def _counter(**kw):
    from collections import Counter
    return Counter(kw)


def _ids(**kw):
    return {t: {f"{t}{i}" for i in range(n)} for t, n in kw.items()}


def test_unreachable_is_not_the_same_as_empty() -> None:
    """THE REGRESSION TEST.

    Zero rounds succeeded, so there is no evidence either way. Reporting this
    as "no observation channel" is a false negative on the single question the
    script exists to answer, and it is exactly what the first version did when
    a proxy returned 403.
    """
    out = report("net", _counter(), {}, rounds=5,
                 errors=["ProxyError: 403 Forbidden"] * 5, ok_rounds=0)
    assert out["reachable"] is False
    assert out["observation"] == 0


def test_reached_but_empty_is_a_real_negative() -> None:
    """Distinct from the above: we got answers, they contained nothing. That
    IS evidence, and should be treated as such."""
    out = report("net", _counter(), {}, rounds=5, errors=[], ok_rounds=5)
    assert out["reachable"] is True
    assert out["observation"] == 0


def test_observations_are_counted_separately_from_reports() -> None:
    out = report("net",
                 _counter(adsb_icao=100, mlat=7, tisb_trackfile=3, mode_s=11),
                 _ids(adsb_icao=40, mlat=4, tisb_trackfile=2, mode_s=5),
                 rounds=5, errors=[], ok_rounds=5)
    assert out["total"] == 121
    assert out["observation"] == 10          # mlat + tisb_trackfile only
    assert out["reachable"] is True


def test_a_feed_of_pure_adsb_yields_no_observations() -> None:
    """The honest negative. Plenty of traffic, none of it independently
    sensed -- which is a real and reportable finding about the region."""
    out = report("net", _counter(adsb_icao=250, adsr_icao=10, mode_s=30),
                 _ids(adsb_icao=90, adsr_icao=4, mode_s=12),
                 rounds=5, errors=[], ok_rounds=5)
    assert out["observation"] == 0
    assert out["total"] == 290


# -- configuration ---------------------------------------------------------

def test_several_networks_are_queried() -> None:
    """Asking one network and concluding from it would confuse 'this feeder
    network has no receivers here', and 'this endpoint is filtered', with
    'no observations exist here'. Three sources, so one going closed or
    feeder-only cannot by itself produce a false negative."""
    assert len(NETWORKS) >= 3


def test_every_endpoint_takes_the_same_three_placeholders() -> None:
    for name, candidates in NETWORKS.items():
        assert candidates, f"{name} has no candidate URLs"
        for url in candidates:
            for ph in ("{lat}", "{lon}", "{nm}"):
                assert ph in url, f"{name}: {url} is missing {ph}"


# -- what an absence can and cannot support --------------------------------

def test_missing_mode_s_is_no_longer_treated_as_evidence() -> None:
    """THE THIRD REGRESSION TEST, and the most embarrassing.

    An earlier version flagged a feed as "filtered" whenever it returned no
    mode_s targets. That was wrong by construction: these are RADIUS queries,
    selecting aircraft within N miles of a point requires a position to test,
    and a mode_s target has none. No spatial query from any provider can ever
    return one. The absence was arithmetic, not evidence -- and the script
    printed it as a reason to distrust two feeds that were behaving normally.
    """
    notes = caveats(_counter(adsb_icao=295))
    assert not any("filtered" in n for n in notes), \
        "absence of mode_s must not be reported as a filtered feed"


def test_absent_observations_are_reported_as_a_real_absence() -> None:
    """mlat and tisb_* DO carry positions, so a radius query would return
    them. Their absence is genuine evidence -- and must be stated as such,
    with the reason it is nonetheless expected."""
    notes = caveats(_counter(adsb_icao=295))
    assert any("real absence" in n for n in notes)
    assert any("aggregators" in n for n in notes)


def test_a_feed_carrying_observations_raises_no_absence_note() -> None:
    notes = caveats(_counter(adsb_icao=250, mlat=4))
    assert not any("absence" in n for n in notes)


def test_a_small_sample_is_judged_as_too_small() -> None:
    """Five aircraft over a quiet box supports no conclusion in any
    direction, and saying so is the only honest output."""
    assert caveats(_counter(adsb_icao=5)) == ["sample too small to conclude anything"]
