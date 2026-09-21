"""Tests for the core subtraction.

Everything this project claims comes out of this module, so the tests are
mostly about the ways it could produce a confident number that means nothing:
association that invents dark vessels through its own ordering, a detection
rate diluted by vessels the sensor was never shown, and a headline figure that
reads the same whether the run went well or badly.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from angels.core.detectors import matching
from angels.core.detectors.matching import associate, unmatched
from angels.core.models import Observation, Position, Report, Track

T = datetime(2024, 9, 13, 22, 58, 0, tzinfo=timezone.utc)
M_PER_DEG = 111_195.0


def at(lat, lon, t=T, unc=40.0, **kw):
    return Position(lat=lat, lon=lon, t=t, uncertainty_m=unc, **kw)


def obs(lat, lon, t=T, unc=40.0, **attrs):
    return Observation(position=at(lat, lon, t, unc), sensor="sentinel1-vv-cfar",
                       attributes=attrs)


def track(mmsi, lat, lon, *, t=T, unc=20.0, span_s=600, speed=5.0,
          heading=90.0, dlat=0.0, dlon=0.0):
    """A two-report track bracketing t, so position_at interpolates."""
    a = Report(mmsi, at(lat - dlat, lon - dlon, t - timedelta(seconds=span_s),
                        unc, speed_mps=speed, heading_deg=heading), "ais")
    b = Report(mmsi, at(lat + dlat, lon + dlon, t + timedelta(seconds=span_s),
                        unc, speed_mps=speed, heading_deg=heading), "ais")
    return Track(mmsi, "sea", [a, b])


def north(metres):
    return metres / M_PER_DEG


# -- the ordinary case -----------------------------------------------------

def test_a_reported_vessel_is_matched() -> None:
    r = associate([obs(38.9, -76.2)], [track("1", 38.9, -76.2)], T)
    assert len(r.pairs) == 1
    assert not r.unmatched_observations
    assert not r.unmatched_tracks


def test_a_vessel_far_from_any_report_is_unmatched() -> None:
    r = associate([obs(38.9, -76.2)], [track("1", 39.5, -76.2)], T)
    assert r.unmatched_observations == [0]
    assert r.unmatched_tracks == [0]


def test_nothing_reported_means_every_detection_is_unmatched() -> None:
    r = associate([obs(38.9, -76.2), obs(38.95, -76.3)], [], T)
    assert len(r.unmatched_observations) == 2
    assert math.isnan(r.detection_rate)


# -- both directions, which is the point -----------------------------------

def test_a_reported_vessel_the_sensor_missed_is_recorded() -> None:
    """THE CALIBRATION. Without this direction, 'no report explains this
    detection' and 'this detector cannot see things like that' produce
    identical output, and only one of them is a finding."""
    r = associate([], [track("1", 38.9, -76.2)], T)
    assert r.unmatched_tracks == [0]
    assert r.detection_rate == 0.0


def test_detection_rate_is_found_over_findable() -> None:
    tracks = [track(str(i), 38.9 + north(3000 * i), -76.2) for i in range(4)]
    seen = [obs(38.9, -76.2), obs(38.9 + north(3000), -76.2)]
    r = associate(seen, tracks, T)
    assert len(r.pairs) == 2
    assert r.detection_rate == pytest.approx(0.5)


# -- the denominator -------------------------------------------------------

def test_a_track_with_no_fix_is_not_counted_as_a_miss() -> None:
    """A vessel whose reports do not reach this instant was never shown to
    the sensor. Counting it as a miss would push the detection rate down with
    arithmetic instead of evidence -- and a depressed rate silently weakens
    every finding in the run."""
    later = Track("ghost", "sea", [
        Report("ghost", at(38.9, -76.2, T + timedelta(hours=3), 20.0), "ais")])
    r = associate([obs(38.9, -76.2)], [track("1", 38.9, -76.2), later], T)
    assert r.n_tracks_without_fix == 1
    assert r.detection_rate == pytest.approx(1.0), "1 of 1 findable, not 1 of 2"
    assert r.unmatched_tracks == [1], "still listed -- just not a miss"


def test_a_wildly_extrapolated_fix_is_refused() -> None:
    """position_at dead-reckons past the last report and inflates uncertainty
    as it goes. Past a point the honest response is to stop calling the
    result a position: a 13 km radius matches most of the bay, so every
    detection would find a partner and the sea would look immaculate."""
    stale = Track("stale", "sea", [
        Report("stale", at(38.9, -76.2, T - timedelta(hours=2), 20.0,
                           speed_mps=6.0, heading_deg=90.0), "ais")])
    r = associate([obs(38.9, -76.2)], [stale], T)
    assert r.n_tracks_without_fix == 1
    assert r.unmatched_observations == [0]


def test_the_extrapolation_limit_is_configurable_and_biting() -> None:
    stale = Track("stale", "sea", [
        Report("stale", at(38.9, -76.2, T - timedelta(hours=2), 20.0,
                           speed_mps=6.0, heading_deg=90.0), "ais")])
    loose = associate([obs(38.9, -76.2)], [stale], T,
                      max_fix_uncertainty_m=1e9)
    assert loose.n_tracks_without_fix == 0


# -- assignment ------------------------------------------------------------

def test_two_detections_do_not_share_one_report() -> None:
    """Nearest-neighbour hands both to the same track and calls the spare
    one dark. Each side may be used once."""
    a = obs(38.9, -76.2)
    b = obs(38.9 + north(60), -76.2)
    r = associate([a, b], [track("1", 38.9 + north(30), -76.2)], T)
    assert len(r.pairs) == 1
    assert len(r.unmatched_observations) == 1


def test_greedy_would_invent_a_dark_vessel_and_this_does_not() -> None:
    """THE POINT OF THE FILE.

    Four detections and tracks on a line, with a radius of about 121 m:

        A-t1  105 m   admissible, and the shortest link on the board
        B-t1  110 m   admissible
        A-t2  115 m   admissible
        B-t2  330 m   out of range

    Greedy takes the shortest link first -- A to t1 -- and then B has only t1,
    which is spent. B is reported as a dark vessel. But A-t2 with B-t1
    explains both, and every link in it is admissible.

    The error only ever runs one way: greedy INVENTS dark vessels. Maximum
    matching makes 'unmatched' mean 'no assignment explains this', which is
    the conservative reading and the one a reviewer cannot take away.
    """
    lat, lon = 38.9, -76.2
    a = obs(lat, lon, unc=30.0)
    b = obs(lat + north(215), lon, unc=30.0)

    t1 = track("1", lat + north(105), lon, unc=10.0)
    t2 = track("2", lat - north(115), lon, unc=10.0)

    # The premise, asserted rather than assumed -- if the radius ever changes,
    # this test must fail loudly instead of passing for a different reason.
    radius = matching.pair_radius(a, at(lat, lon, unc=10.0),
                                  matching.DEFAULT_VESSEL_LENGTH_M)
    assert 120 < radius < 125, f"premise broken: radius is {radius:.0f} m"

    r = associate([a, b], [t1, t2], T)
    assert len(r.pairs) == 2, (
        "both detections are explicable; reporting either as dark would be "
        "an artefact of assignment order")
    assert not r.unmatched_observations


def test_greedy_really_does_fail_that_case() -> None:
    """The test above claims maximum matching beats greedy. This runs greedy
    on the same input and checks it actually loses -- otherwise the claim is
    an assertion about an algorithm nobody ran, and the choice is unjustified.
    """
    lat, lon = 38.9, -76.2
    seen = [obs(lat, lon, unc=30.0), obs(lat + north(215), lon, unc=30.0)]
    tracks = [track("1", lat + north(105), lon, unc=10.0),
              track("2", lat - north(115), lon, unc=10.0)]

    cands, _ = matching.candidates(seen, tracks, T)

    used_o: set[int] = set()
    used_t: set[int] = set()
    greedy = 0
    for c in sorted(cands, key=lambda c: c.distance_m):
        if c.obs_index in used_o or c.track_index in used_t:
            continue
        used_o.add(c.obs_index)
        used_t.add(c.track_index)
        greedy += 1

    assert greedy == 1, "greedy should strand one detection here"
    assert len(matching._maximum_matching(cands, len(seen))) == 2


def test_matching_is_not_sensitive_to_input_order() -> None:
    """If the count of dark vessels depends on the order rows came off disk,
    it is not a measurement."""
    lat, lon = 38.9, -76.2
    seen = [obs(lat, lon), obs(lat + north(700), lon),
            obs(lat + north(1400), lon)]
    tracks = [track("a", lat + north(60), lon),
              track("b", lat + north(760), lon),
              track("c", lat + north(1460), lon)]

    base = len(associate(seen, tracks, T).pairs)
    assert len(associate(seen[::-1], tracks, T).pairs) == base
    assert len(associate(seen, tracks[::-1], T).pairs) == base
    assert len(associate(seen[::-1], tracks[::-1], T).pairs) == base


# -- the radius ------------------------------------------------------------

def test_the_radius_grows_with_both_uncertainties() -> None:
    near = matching.pair_radius(obs(38.9, -76.2, unc=10.0),
                                at(38.9, -76.2, unc=10.0), 50.0)
    far = matching.pair_radius(obs(38.9, -76.2, unc=200.0),
                               at(38.9, -76.2, unc=10.0), 50.0)
    assert far > near


def test_the_radius_grows_with_vessel_length() -> None:
    """AIS reports the transponder; SAR reports the brightest scatterer. On a
    300 m hull those are not the same place, and a radius that ignores it
    calls the bow of a container ship a separate, unreported vessel."""
    small = matching.pair_radius(obs(38.9, -76.2), at(38.9, -76.2), 20.0)
    large = matching.pair_radius(obs(38.9, -76.2), at(38.9, -76.2), 350.0)
    assert large > small * 1.2


def test_length_is_taken_from_the_reports_when_present() -> None:
    class Sized(Report):
        length_m = 320.0

    tr = Track("big", "sea", [
        Sized("big", at(38.9, -76.2, T - timedelta(seconds=60), 20.0), "ais"),
        Sized("big", at(38.9, -76.2, T + timedelta(seconds=60), 20.0), "ais")])
    assert matching.vessel_length_m(tr) == pytest.approx(320.0)


def test_an_unsized_track_falls_back_rather_than_crashing() -> None:
    assert matching.vessel_length_m(track("1", 38.9, -76.2)) == \
        matching.DEFAULT_VESSEL_LENGTH_M


# -- events ----------------------------------------------------------------

def test_confidence_falls_when_the_sensor_is_doing_badly() -> None:
    """A bright unambiguous return that no AIS explains is strong evidence of
    a vessel, and weak evidence about hiding if the sensor found only half of
    what reported itself. Coupling them makes a bad run produce weaker claims
    automatically, rather than the same claims with nothing to flag them."""
    lat, lon = 38.9, -76.2
    dark = obs(lat + north(20_000), lon, confidence=0.9)

    good = [track(str(i), lat + north(3000 * i), lon) for i in range(4)]
    seen_all = [obs(lat + north(3000 * i), lon) for i in range(4)]

    strong = unmatched(seen_all + [dark], good, T)
    weak = unmatched([seen_all[0], dark], good, T)

    assert len(strong) == len(weak) == 1
    assert strong[0].confidence > weak[0].confidence
    assert strong[0].evidence["detection_rate_this_pass"] == pytest.approx(1.0)
    assert weak[0].evidence["detection_rate_this_pass"] == pytest.approx(0.25)


def test_an_event_carries_the_evidence_to_argue_with_it() -> None:
    e = unmatched([obs(38.9, -76.2, confidence=0.8, snr=42.0, scene="S1A_x",
                       pixels=120, length_m_approx=110.0)],
                  [track("1", 39.6, -76.2)], T)[0]
    assert e.kind == "unmatched"
    assert e.evidence["snr"] == 42.0
    assert e.evidence["scene"] == "S1A_x"
    assert e.evidence["reports_considered"] == 1
    assert e.evidence["search_radius_m"] > 0


def test_a_matched_detection_produces_no_event() -> None:
    assert unmatched([obs(38.9, -76.2)], [track("1", 38.9, -76.2)], T) == []


def test_confidence_stays_in_range_even_for_a_certain_detector() -> None:
    e = unmatched([obs(38.9, -76.2, confidence=1.0)], [], T)[0]
    assert 0.0 <= e.confidence <= 1.0


# -- the summary line ------------------------------------------------------

def test_str_reports_both_directions() -> None:
    s = str(associate([obs(38.9, -76.2)], [track("1", 39.6, -76.2)], T))
    assert "unmatched detections" in s and "missed reports" in s


def test_unmatched_fraction_is_not_readable_alone() -> None:
    """Both runs report 100% unmatched. One found nothing because nothing was
    reported; the other because the sensor missed everything. The fraction
    cannot tell them apart -- which is why detection_rate sits beside it."""
    nothing_reported = associate([obs(38.9, -76.2)], [], T)
    everything_missed = associate([obs(38.9, -76.2)],
                                  [track("1", 39.6, -76.2)], T)
    assert nothing_reported.unmatched_fraction == 1.0
    assert everything_missed.unmatched_fraction == 1.0
    assert math.isnan(nothing_reported.detection_rate)
    assert everything_missed.detection_rate == 0.0


# -- scale -----------------------------------------------------------------

def test_the_spatial_index_finds_exactly_what_brute_force_finds() -> None:
    """THE RISK OF THE INDEX.

    Bucketing cuts the comparison count by orders of magnitude, and a cell
    even slightly too small drops a real pairing outside the searched
    neighbourhood -- which is reported as a dark vessel. That is the precise
    error this module exists to avoid, so the index must return the same
    candidate set as comparing everything against everything.
    """
    import random
    rnd = random.Random(7)
    seen, tracks = [], []
    for i in range(120):
        lat = 37.0 + rnd.uniform(0, 2.0)
        lon = -75.0 + rnd.uniform(0, 2.0)
        seen.append(obs(lat, lon, unc=rnd.uniform(10, 60)))
        # half the tracks sit right on a detection, half are far away
        jitter = north(rnd.uniform(0, 150)) if i % 2 else north(rnd.uniform(0, 5e4))
        tracks.append(track(str(i), lat + jitter, lon, unc=rnd.uniform(10, 40)))

    got, _ = matching.candidates(seen, tracks, T)

    brute = []
    fixes = [tr.position_at(T) for tr in tracks]
    for i, o in enumerate(seen):
        for j, f in enumerate(fixes):
            if f is None or f.uncertainty_m > matching.MAX_FIX_UNCERTAINTY_M:
                continue
            d = o.position.distance_to(f)
            r = matching.pair_radius(o, f, matching.vessel_length_m(tracks[j]))
            if d <= r:
                brute.append((i, j))

    assert {(c.obs_index, c.track_index) for c in got} == set(brute)
    assert brute, "the fixture must actually produce pairings"


def test_vessel_length_is_computed_once_per_track() -> None:
    """THE FREEZE.

    vessel_length_m scans a track's reports. Called inside the inner loop
    against 845 detections and 1,500 tracks that is tens of millions of
    lookups, and a vessel reporting every two seconds for an hour has 1,800
    reports on its own. The run stopped responding.
    """
    calls = {"n": 0}
    real = matching.vessel_length_m

    def counting(tr):
        calls["n"] += 1
        return real(tr)

    seen = [obs(38.9 + north(300 * i), -76.2) for i in range(30)]
    tracks = [track(str(i), 38.9 + north(300 * i), -76.2) for i in range(30)]

    matching.vessel_length_m = counting
    try:
        matching.candidates(seen, tracks, T)
    finally:
        matching.vessel_length_m = real

    assert calls["n"] == len(tracks), (
        f"called {calls['n']} times for {len(tracks)} tracks -- it is back "
        f"inside the pair loop")


def test_a_realistic_scene_completes_quickly() -> None:
    """845 detections against 1,500 tracks is the real workload. Brute force
    is 1.27 million pair comparisons; this must stay well under a second."""
    import random
    import time
    rnd = random.Random(1)
    seen = [obs(36.0 + rnd.uniform(0, 3.5), -77.0 + rnd.uniform(0, 5.0))
            for _ in range(845)]
    tracks = [track(str(i), 36.0 + rnd.uniform(0, 3.5), -77.0 + rnd.uniform(0, 5.0))
              for i in range(1500)]

    t0 = time.perf_counter()
    matching.associate(seen, tracks, T)
    assert time.perf_counter() - t0 < 2.0
