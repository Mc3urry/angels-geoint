"""Tests for the defended detection rate.

The rate is what licenses every dark-vessel claim, so the tests are about the
ways it could be quietly biased: a class boundary that moves with the result,
an exclusion rule that looks at whether a vessel was found, a default length
mistaken for a reported one, and an interval that lies at small counts.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from angels.adapters.maritime.ais import AISReport
from angels.core.detectors import calibration as cal_mod
from angels.core.detectors.calibration import (
    Calibration, Rate, calibrate, chance_of_coincidence, length_class,
    reported_length, wilson)
from angels.core.detectors.matching import associate
from angels.core.models import Observation, Position, Track

T = datetime(2024, 6, 21, 22, 59, 7, tzinfo=timezone.utc)
M_PER_DEG = 111_195.0


def at(lat, lon, t=T, unc=40.0):
    return Position(lat=lat, lon=lon, t=t, uncertainty_m=unc,
                    speed_mps=5.0, heading_deg=90.0)


def obs(lat, lon, unc=40.0):
    return Observation(position=at(lat, lon, unc=unc),
                       sensor="sentinel1-vv-cfar", attributes={})


def track(mmsi, lat, lon, *, length=None, unc=20.0):
    reps = [AISReport(mmsi, at(lat, lon, T + timedelta(seconds=s), unc), "ais",
                      length_m=length) for s in (-60, 60)]
    return Track(mmsi, "sea", reps)


# -- the interval ------------------------------------------------------------

def test_wilson_matches_the_worked_example() -> None:
    """8 of 28, the June slice 3 figure quoted in the write-up."""
    lo, hi = wilson(8, 28)
    assert lo == pytest.approx(0.1525, abs=1e-3)
    assert hi == pytest.approx(0.4706, abs=1e-3)


def test_wilson_stays_inside_zero_and_one_at_the_extremes() -> None:
    """Where the normal approximation breaks, which is where this project is."""
    lo, hi = wilson(0, 3)
    assert lo == 0.0 and 0 < hi < 1
    lo, hi = wilson(3, 3)
    assert 0 < lo < 1 and hi == 1.0


def test_no_vessels_is_no_rate_not_zero() -> None:
    assert all(math.isnan(v) for v in wilson(0, 0))
    assert math.isnan(Rate().rate)


# -- the chance model --------------------------------------------------------

def test_chance_grows_with_radius_and_density() -> None:
    assert chance_of_coincidence(0, 0.03) == 0.0
    assert chance_of_coincidence(500, 0.03) < chance_of_coincidence(1000, 0.03)
    assert chance_of_coincidence(500, 0.01) < chance_of_coincidence(500, 0.05)


def test_the_bailout_radius_is_mostly_coincidence() -> None:
    """The 2.9 km match that started this: ~5 km radius at June slice 3's
    375 detections over 16,316 km2."""
    assert chance_of_coincidence(5000, 375 / 16316.5) > 0.8


# -- length classes ----------------------------------------------------------

def test_a_default_length_is_not_a_reported_one() -> None:
    """matching substitutes 50 m for the radius. A vessel that never sent a
    length must not be counted as a 50 m ship."""
    assert reported_length(track("1", 37, -75)) is None
    assert length_class(track("1", 37, -75)) == "unknown length"
    assert length_class(track("1", 37, -75, length=50)) == ">=25 m"


def test_the_class_edge_is_inclusive_and_fixed() -> None:
    assert cal_mod.DETECTABLE_LENGTH_M == 25.0
    assert length_class(track("1", 37, -75, length=25)) == ">=25 m"
    assert length_class(track("1", 37, -75, length=24.9)) == "<25 m"


# -- scoring -----------------------------------------------------------------

def test_found_and_missed_land_in_their_classes() -> None:
    tracks = [track("big", 37.0, -75.0, length=120),
              track("small", 37.2, -75.0, length=12),
              track("anon", 37.4, -75.0)]
    observations = [obs(37.0, -75.0)]          # only the big one is seen
    r = associate(observations, tracks, T)
    c = calibrate(r, tracks, observations, T, searched_km2=10_000)
    assert (c.strata[">=25 m"].found, c.strata[">=25 m"].scored) == (1, 1)
    assert (c.strata["<25 m"].found, c.strata["<25 m"].scored) == (0, 1)
    assert (c.strata["unknown length"].found,
            c.strata["unknown length"].scored) == (0, 1)
    assert (c.strata["all scored"].found, c.strata["all scored"].scored) == (1, 3)
    assert c.n_unscorable == 0


def _loose_scene(detected: bool):
    """One tightly located and one loosely located vessel, the loose one
    either sitting on a detection or not."""
    tight = track("tight", 37.0, -75.0, length=100)
    loose = track("loose", 37.5, -75.0, length=100, unc=1500.0)
    observations = [obs(37.0, -75.0)]
    if detected:
        observations.append(obs(37.5 + 1000 / M_PER_DEG, -75.0))
    # Clutter everywhere else, so the density is high enough that a
    # ~4.5 km radius would catch a random detection.
    observations += [obs(36.0 + i * 0.01, -76.0) for i in range(200)]
    return [tight, loose], observations


@pytest.mark.parametrize("detected", [True, False])
def test_a_loosely_located_vessel_is_not_scored_either_way(detected) -> None:
    """THE RULE THAT MATTERS. Eligibility must not depend on the outcome:
    excluding only doubtful matches biases the rate down, excluding only
    doubtful misses biases it up."""
    tracks, observations = _loose_scene(detected)
    r = associate(observations, tracks, T)
    assert (1 in {c.track_index for c in r.pairs}) is detected
    c = calibrate(r, tracks, observations, T, searched_km2=5_000)
    assert c.unscorable == {1}
    assert c.n_unscorable_matched == int(detected)
    assert c.strata[">=25 m"].scored == 1          # only the tight one
    assert c.strata[">=25 m"].found == 1


def test_without_a_searched_area_nothing_is_excluded() -> None:
    """No density, no chance model -- and so no silent exclusions."""
    tracks, observations = _loose_scene(True)
    r = associate(observations, tracks, T)
    c = calibrate(r, tracks, observations, T, searched_km2=None)
    assert c.n_unscorable == 0 and c.strata["all scored"].scored == 2


def test_the_scorable_radius_is_where_chance_hits_the_limit() -> None:
    tracks, observations = _loose_scene(False)
    r = associate(observations, tracks, T)
    c = calibrate(r, tracks, observations, T, searched_km2=5_000)
    p = chance_of_coincidence(c.max_scorable_radius_m, c.density_per_km2)
    assert p == pytest.approx(cal_mod.MAX_CHANCE_MATCH, rel=1e-6)


def test_passes_pool_by_adding_counts() -> None:
    a, b = Calibration(), Calibration()
    a.strata[">=25 m"] = Rate(3, 5)
    b.strata[">=25 m"] = Rate(4, 6)
    a.n_unscorable, b.n_unscorable = 1, 2
    pooled = a + b
    assert (pooled.headline.found, pooled.headline.scored) == (7, 11)
    assert pooled.n_unscorable == 3


def test_the_json_is_plain_and_carries_the_rule() -> None:
    import json
    tracks, observations = _loose_scene(True)
    r = associate(observations, tracks, T)
    doc = calibrate(r, tracks, observations, T, searched_km2=5_000).to_json()
    json.dumps(doc)                                  # serialisable
    assert doc["detectable_length_m"] == 25.0
    assert doc["max_chance_match"] == 0.05
    assert doc["strata"][">=25 m"]["ci95"][1] == 1.0


def test_a_fixed_density_fixes_who_is_scored() -> None:
    """Gating thins the detections. Without the override, a stricter gate
    would also change which vessels count, and rates with different
    denominators would be compared as equals."""
    tracks, observations = _loose_scene(True)
    r = associate(observations[:2], tracks, T)        # a heavily gated set
    free = calibrate(r, tracks, observations[:2], T, searched_km2=5_000)
    fixed = calibrate(r, tracks, observations[:2], T, searched_km2=5_000,
                      density_per_km2=len(observations) / 5_000)
    assert free.n_unscorable == 0          # thin detections: everyone scores
    assert fixed.unscorable == {1}         # the ungated scene's rule holds
