"""Tests for the ship detector's arithmetic.

Every case is synthetic sea with planted vessels, so the right answer is known
exactly. Point a detector at a real scene first and you cannot tell a bug from
a discovery -- which is the same reason tests/synthetic.py exists for the
aviation side.
"""

from __future__ import annotations

import numpy as np
import pytest

from angels.adapters.maritime import cfar


def sea(h=400, w=400, scale=300.0, seed=0, gradient=False):
    """Rayleigh clutter, optionally brightening across the swath.

    Rayleigh rather than Gaussian because that is what SAR amplitude over
    water actually is, and a detector tuned on Gaussian noise behaves
    differently on the real thing.
    """
    rng = np.random.default_rng(seed)
    if gradient:
        s = np.linspace(scale / 2, scale * 3, w)[None, :] * np.ones((h, 1))
    else:
        s = np.full((h, w), scale)
    return rng.rayleigh(scale=s / 1.253).astype(np.uint16)


def plant(arr, row, col, dn, radius=2):
    arr[row - radius:row + radius + 1, col - radius:col + radius + 1] = dn
    return arr


def found_at(dets, row, col, tol=3.0):
    return any(np.hypot(d.row - row, d.col - col) <= tol for d in dets)


# -- the control case ------------------------------------------------------

def test_empty_sea_produces_nothing() -> None:
    """The most important test. A detector that fires on ordinary clutter
    buries every real vessel in its own false alarms."""
    assert cfar.detect(sea(), k=6.0) == []


def test_a_single_hot_pixel_is_not_a_vessel() -> None:
    """One bright pixel is speckle or thermal noise. A ship is several."""
    a = sea()
    a[200, 200] = 60000
    assert not found_at(cfar.detect(a, k=6.0, min_pixels=2), 200, 200)


# -- the positive case -----------------------------------------------------

def test_a_planted_vessel_is_found() -> None:
    a = plant(sea(), 150, 250, 4000)
    assert found_at(cfar.detect(a, k=6.0), 150, 250)


def test_the_centroid_is_sub_pixel_accurate() -> None:
    """matching.py pairs detections to AIS by distance. A centroid off by
    several pixels is 50+ m of error added to every comparison."""
    d = [x for x in cfar.detect(plant(sea(), 150, 250, 4000), k=6.0)
         if np.hypot(x.row - 150, x.col - 250) < 3][0]
    assert abs(d.row - 150) < 0.5
    assert abs(d.col - 250) < 0.5


def test_snr_rises_with_brightness() -> None:
    dim = cfar.detect(plant(sea(), 150, 250, 1500), k=5.0)[0]
    bright = cfar.detect(plant(sea(), 150, 250, 9000), k=5.0)[0]
    assert bright.snr > dim.snr


# -- why CFAR rather than a fixed threshold --------------------------------

def test_a_fixed_threshold_fails_where_cfar_does_not() -> None:
    """THE WHOLE ARGUMENT FOR THIS MODULE.

    Sea clutter rises with wind and with incidence angle across the swath. A
    threshold that finds vessels in the calm half drowns in the rough half,
    and one tuned for the rough half misses the calm one. CFAR thresholds
    against LOCAL clutter, so the same k works in both.
    """
    a = sea(gradient=True, seed=5)
    plant(a, 100, 60, 1800)          # in the dark half
    plant(a, 300, 340, 5200)         # in the bright half

    dets = cfar.detect(a, k=6.0)
    assert found_at(dets, 100, 60), "missed the vessel in calm water"
    assert found_at(dets, 300, 340), "missed the vessel in rough water"
    assert len(dets) == 2, f"CFAR also produced {len(dets) - 2} false alarms"

    # Now the comparison. Set a single global threshold low enough to catch
    # the vessel in the calm half -- the thing CFAR did for free -- and count
    # what else it admits from the rough half.
    fixed = a[98:103, 58:63].max() * 0.95
    hits = a > fixed
    assert hits[98:103, 58:63].any(), "threshold set wrong for this test"

    rough_half = hits[:, a.shape[1] // 2:].sum()
    assert rough_half > 500, (
        f"only {rough_half} false pixels in the rough half -- the gradient is "
        f"too weak for this comparison to mean anything"
    )


def test_the_guard_ring_stops_a_big_ship_hiding_itself() -> None:
    """A vessel large enough to fill much of the background window raises its
    own local mean. Without a guard ring it thresholds itself away -- and the
    biggest, most interesting targets are the ones that vanish."""
    # 290 m -- a Panamax container ship, entirely ordinary in the Chesapeake.
    a = plant(sea(500, 500), 250, 250, 5000, radius=14)
    with_guard = cfar.detect(a, guard=cfar.GUARD, background=cfar.BACKGROUND,
                             k=6.0, max_pixels=100000)
    no_guard = cfar.detect(a, guard=1, background=cfar.BACKGROUND, k=6.0,
                           max_pixels=100000)
    assert found_at(with_guard, 250, 250), "the guard ring failed to protect it"
    assert not found_at(no_guard, 250, 250), (
        "the no-guard case found it too, so this test proves nothing -- make "
        "the planted vessel larger"
    )


# -- numerical robustness --------------------------------------------------

def test_variance_never_goes_negative_on_flat_input() -> None:
    """E[X^2] - E[X]^2 can land at -1e-9 on constant input. sqrt of that is
    NaN, NaN compares False everywhere, and the detector silently finds
    nothing at all."""
    flat = np.full((100, 100), 12345, dtype=np.uint16)
    mu, sigma = cfar.local_stats(flat)
    assert np.isfinite(sigma).all()
    assert (sigma >= 0).all()
    assert np.isfinite(mu).all()


def test_large_dn_values_do_not_lose_precision() -> None:
    """uint16 squared overflows int32 and float32 carries too few digits for
    the identity above. The sums must be float64."""
    a = np.full((60, 60), 60000, dtype=np.uint16)
    a[30, 30] = 65535
    mu, sigma = cfar.local_stats(a)
    assert np.isfinite(sigma).all()
    assert abs(mu[5, 5] - 60000) < 50


def test_background_must_exceed_the_guard() -> None:
    with pytest.raises(ValueError):
        cfar.local_stats(sea(100, 100), guard=41, background=11)


# -- the runaway guard -----------------------------------------------------

def test_an_absurd_hit_rate_raises_instead_of_crawling() -> None:
    """Unmasked land sets tens of millions of pixels. The connected-components
    pass is Python-level, so it would run for hours and then return something
    meaningless -- and while running, look exactly like a big scene taking a
    while."""
    a = sea()
    a[:, 200:] = 40000                      # half the tile is "land"
    with pytest.raises(cfar.TooManyDetections) as e:
        cfar.detect(a, k=0.5)
    assert "land" in str(e.value) or "too low" in str(e.value)


def test_oversized_blobs_are_rejected() -> None:
    """A 40 km bright region is a peninsula, not a vessel."""
    a = sea()
    a[100:180, 100:180] = 30000
    assert not found_at(cfar.detect(a, k=6.0, max_pixels=500), 140, 140)


# -- masking ---------------------------------------------------------------

def test_a_valid_mask_suppresses_detections() -> None:
    a = plant(sea(), 150, 250, 8000)
    valid = np.ones(a.shape, dtype=bool)
    valid[140:160, 240:260] = False
    assert found_at(cfar.detect(a, k=6.0), 150, 250)
    assert not found_at(cfar.detect(a, k=6.0, valid=valid), 150, 250)


# -- clustering ------------------------------------------------------------

def test_adjacent_pixels_form_one_detection() -> None:
    """A vessel is several pixels. Reporting each separately would inflate
    the count by its size and make big ships look like fleets."""
    a = plant(sea(), 200, 200, 8000, radius=3)
    near = [d for d in cfar.detect(a, k=6.0)
            if np.hypot(d.row - 200, d.col - 200) < 6]
    assert len(near) == 1
    assert near[0].pixels >= 9


def test_two_separated_vessels_stay_separate() -> None:
    a = sea()
    plant(a, 100, 100, 6000)
    plant(a, 300, 300, 6000)
    dets = cfar.detect(a, k=6.0)
    assert found_at(dets, 100, 100)
    assert found_at(dets, 300, 300)


def test_empty_mask_labels_cleanly() -> None:
    labels, n = cfar.label_clusters(np.zeros((10, 10), dtype=bool))
    assert n == 0
    assert labels.sum() == 0


# -- edges -----------------------------------------------------------------

def test_a_vessel_at_the_edge_is_still_found() -> None:
    """Window clipping must produce a smaller but valid background, not a
    reflected one -- reflecting invents clutter that is not there."""
    a = plant(sea(), 5, 5, 8000)
    assert found_at(cfar.detect(a, k=6.0), 5, 5)


def test_detections_come_back_strongest_first() -> None:
    a = sea()
    plant(a, 100, 100, 2000)
    plant(a, 300, 300, 20000)
    dets = cfar.detect(a, k=5.0)
    assert dets == sorted(dets, key=lambda d: -d.snr)


def test_the_parameters_cover_the_largest_real_vessels() -> None:
    """The guard is sized by the biggest hull afloat, not by convenience.

    A vessel wider than the guard ring contaminates its own background and
    disappears -- silently, and preferentially at the top of the size range.
    The largest container ships are about 400 m; the guard must clear that.
    """
    assert cfar.GUARD * 10 >= 400, "guard is narrower than a Post-Panamax ship"
    assert cfar.BACKGROUND > cfar.GUARD * 2, (
        "the clutter ring needs real area outside the guard, or sigma is "
        "estimated from too few pixels and the threshold gets noisy"
    )
