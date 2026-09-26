"""`pixel_of` must not return a pixel it did not find.

It ran thirty Newton steps and returned whatever it was holding. On 17 of
1,150 candidates (1.5%) that answer was 300 m to 10 km from the truth,
usually pinned against the raster's last column, and it looked exactly like
the 98.5% that were right to 0.04 px. Four of the 147 hand-read chips were
cut from the wrong ground because of it, and three of them produced a written,
committed, pushed finding -- "detections in the no-data ramp" -- that was
entirely false.
"""

from __future__ import annotations

import math

import pytest

from scripts.inspect_misses import NoPixel, pixel_of


class _GCP:
    def __init__(self, col, row, x, y):
        self.col, self.row, self.x, self.y = col, row, x, y


class _Affine:
    """A well-behaved, exactly invertible geolocator."""

    gcps = [_GCP(0, 0, -75.0, 37.0), _GCP(1000, 0, -74.0, 37.0),
            _GCP(0, 1000, -75.0, 38.0), _GCP(1000, 1000, -74.0, 38.0)]

    def lonlat(self, col, row):
        return -75.0 + col / 1000.0, 37.0 + row / 1000.0


class _Saturating:
    """Smooth, then flat: the shape that pins Newton against an edge.

    Beyond col 800 the forward map stops changing, so no iteration can reach
    a longitude past that point -- exactly the far-range behaviour observed.
    """

    gcps = _Affine.gcps

    def lonlat(self, col, row):
        # Smooth, so the Jacobian stays invertible and the failure is a
        # genuine non-convergence rather than a singular matrix. Longitude
        # asymptotes just short of -74.2, so -74.0 is unreachable.
        return -75.0 + 0.8 * math.tanh(col / 800.0), 37.0 + row / 1000.0


class _Curved:
    """Invertible everywhere, but not in one Newton step from the corner."""

    gcps = _Affine.gcps

    def lonlat(self, col, row):
        return (-75.0 + col / 1000.0 + 0.10 * (row / 1000.0) ** 2,
                37.0 + row / 1000.0 + 0.10 * (col / 1000.0) ** 2)


def test_finds_a_pixel_it_can_find():
    col, row = pixel_of(_Affine(), -74.5, 37.25)
    assert math.isclose(col, 500.0, abs_tol=0.5)
    assert math.isclose(row, 250.0, abs_tol=0.5)


def test_raises_rather_than_returning_the_edge():
    """The defect: a target past where the forward map can reach.

    Either refusal is correct -- the iteration ends against a flat spot, so
    it is a toss-up whether the Jacobian underflows first or the residual
    check catches it. What must never happen is a returned pixel.
    """
    with pytest.raises(NoPixel) as e:
        pixel_of(_Saturating(), -74.0, 37.25)
    assert "(-74.00000, 37.25000)" in str(e.value)


def test_non_convergence_is_reported_with_the_distance():
    """The tolerance branch, reached by stopping Newton early.

    A refusal that does not say how far off it was is the same defect one
    level up: it reports failure without reporting what failed.
    """
    with pytest.raises(NoPixel) as e:
        pixel_of(_Curved(), -74.5, 37.25, iters=1)
    msg = str(e.value)
    assert "did not converge" in msg
    assert "px" in msg and " m" in msg


def test_still_converges_inside_the_saturated_map():
    """Not over-eager: the region that works must keep working."""
    loc = _Saturating()
    col, row = pixel_of(loc, *loc.lonlat(500.0, 100.0))
    assert math.isclose(col, 500.0, abs_tol=0.5)
    assert math.isclose(row, 100.0, abs_tol=0.5)


def test_a_good_map_is_unaffected_by_a_tight_tolerance():
    col, row = pixel_of(_Affine(), -74.5, 37.25, tol_px=1e-6)
    assert math.isclose(col, 500.0, abs_tol=1e-3)
