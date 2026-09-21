"""Tests for pixel-to-position, and for the proof that it worked.

This is the step that fails without failing. A wrong transform still produces
detections in the ocean, still shaped like shipping lanes, simply displaced.
matching.py then finds no AIS partner within tolerance and reports a sea full
of dark vessels -- a spectacular result, entirely manufactured.

So the module refuses to hand back a transform it has not measured, and these
tests check that the measurement is real -- which, for an interpolator, means
checking that the measurement is not CIRCULAR. A transform built to pass
through every control point scores zero against those same control points. It
cannot fail. A check that cannot fail proves nothing, and a number that proves
nothing is worse than no number, because it gets quoted.
"""

from __future__ import annotations

import pytest

from angels.adapters.maritime.geolocate import (
    Accuracy, GCPGrid, GeolocationError, Geolocator, _haversine_m,
)


class FakeGCP:
    def __init__(self, col, row, x, y):
        self.col, self.row, self.x, self.y = col, row, x, y


def grid(n=5, step=500):
    """A perfectly planar lattice -- bilinear reproduces it exactly."""
    return [FakeGCP(c, r, -76.0 + c * 1e-4, 37.0 + r * 1e-4)
            for r in range(0, n * step, step)
            for c in range(0, n * step, step)]


def curved_grid(n=5, step=500, amp=1e-3):
    """A lattice with curvature in range, as a real SAR swath has.

    Bilinear still passes through every node exactly, but no longer predicts
    the points BETWEEN nodes exactly -- which is what makes holdout a real
    test rather than a restatement of the construction.
    """
    span = (n - 1) * step
    return [FakeGCP(c, r,
                    -76.0 + c * 1e-4 + amp * (r / span) ** 2,
                    37.0 + r * 1e-4)
            for r in range(0, n * step, step)
            for c in range(0, n * step, step)]


class Displaced(Geolocator):
    """A locator whose transform is wrong by a known amount.

    Sets every backend to None so accuracy() takes the RESIDUAL path -- the
    one used for fitted transforms, where comparing against the control points
    is a genuine test because the fit does not pass through them.
    """

    def __init__(self, gcps, dlon=0.0, dlat=0.0):
        self.gcps, self.crs = list(gcps), None
        self._grid = self._gdal = self._affine = None
        self._field = self._acc = None
        self.method, self._d = "synthetic", (dlon, dlat)

    def lonlat(self, col, row):
        for g in self.gcps:
            if g.col == col and g.row == row:
                return g.x + self._d[0], g.y + self._d[1]
        raise KeyError((col, row))


# -- the measurement is real -----------------------------------------------

# One degree of latitude on the sphere haversine assumes: 2*pi*R/360 with
# R = 6371008.8 m. NOT 110,574 m -- that is the WGS84 meridional arc near the
# equator, a different quantity, and using it here made a correct
# implementation look 6 m wrong at the top of the range.
DEG_M = 111194.93


@pytest.mark.parametrize("dlat", [0.0, 0.0001, 0.001, 0.01])
def test_accuracy_measures_a_known_displacement(dlat) -> None:
    acc = Displaced(grid(), dlat=dlat).accuracy()
    assert acc.rms_m == pytest.approx(dlat * DEG_M, abs=0.1)


def test_a_perfect_transform_measures_zero() -> None:
    acc = Displaced(grid()).accuracy()
    assert acc.rms_m == 0.0
    assert acc.worst_m == 0.0
    assert acc.n == 25


def test_error_is_reported_in_pixels_too() -> None:
    """Metres are the honest unit; pixels are the one that tells you whether
    a centroid computed to half a pixel was worth computing."""
    assert Accuracy(10, 50.0, 60.0, "x", "residual").pixels == pytest.approx(5.0)


def test_accuracy_says_how_it_was_validated() -> None:
    """The number alone is not enough. '2 m RMS' from a residual against the
    points the transform was built from means something entirely different
    from '2 m RMS' against points it has never seen, and a reader of the
    write-up cannot tell them apart unless the string says which."""
    acc = Displaced(grid(), dlat=0.0001).accuracy()
    assert acc.validation == "residual"
    assert "residual" in str(acc)


# -- and it is enforced ----------------------------------------------------

def test_check_refuses_a_bad_transform() -> None:
    """THE POINT OF THE FILE. Continuing here produces detections that are
    uniformly, invisibly wrong."""
    with pytest.raises(GeolocationError) as e:
        Displaced(grid(), dlat=0.001).check(max_rms_m=50.0)
    msg = str(e.value)
    assert "dark" in msg, "the error must say what the consequence is"
    assert "111" in msg or "110" in msg, "it must state the measured error"


def test_the_refusal_names_the_likely_cause() -> None:
    """An error that says only 'too big' sends you to the limit to raise it.
    This one has to point at the lattice, because a fallback to a polynomial
    across a 250 km swath is the actual reason it would ever trip."""
    with pytest.raises(GeolocationError) as e:
        Displaced(grid(), dlat=0.001).check(max_rms_m=50.0)
    msg = str(e.value)
    assert "geolocation grid" in msg
    assert "synthetic" in msg, "it must name the method that produced it"


def test_check_passes_a_good_transform() -> None:
    acc = Displaced(grid(), dlat=0.00001).check(max_rms_m=50.0)
    assert acc.rms_m < 50


def test_the_limit_is_a_few_pixels_not_a_few_hundred() -> None:
    """50 m is five GRD pixels -- inside any sensible AIS match tolerance and
    far outside what grid interpolation produces. A limit loose enough to
    admit a fitted affine would defeat the check."""
    import inspect
    sig = inspect.signature(Geolocator.check)
    assert sig.parameters["max_rms_m"].default <= 100


# -- the geolocation grid --------------------------------------------------

def test_a_lattice_is_interpolated_not_fitted() -> None:
    """Sentinel-1 publishes a regular 10x21 grid for exactly this. Preferring
    a global polynomial over it cost 54 m on a real Chesapeake scene."""
    loc = Geolocator(grid())
    assert loc._grid is not None
    assert loc.method == "geolocation grid (bilinear)"


def test_the_grid_is_exact_at_every_node() -> None:
    g = grid()
    gg = GCPGrid(g)
    for p in g:
        lon, lat = gg.lonlat(p.col, p.row)
        assert _haversine_m(p.y, p.x, lat, lon) < 1e-6


def test_the_grid_interpolates_between_nodes() -> None:
    """Halfway between two nodes of a planar grid is halfway in position.
    If this returned a node value instead, the transform would be a
    nearest-neighbour staircase with 500-pixel treads."""
    gg = GCPGrid(grid())
    lon, lat = gg.lonlat(250, 0)
    assert lon == pytest.approx(-76.0 + 250 * 1e-4)
    assert lat == pytest.approx(37.0)


def test_the_grid_clamps_rather_than_extrapolating() -> None:
    """A detection cannot be outside the raster, so a query outside the grid
    is a bug upstream. Extrapolating a bilinear surface from it puts a vessel
    hundreds of kilometres away with no indication anything went wrong;
    clamping puts it on the edge, where it is at least adjacent to the truth.
    """
    gg = GCPGrid(grid())
    corner = gg.lonlat(0, 0)
    assert gg.lonlat(-10_000, -10_000) == corner
    far = gg.lonlat(1_000_000, 1_000_000)
    assert far == gg.lonlat(2000, 2000)


def test_scattered_gcps_are_refused_not_interpolated() -> None:
    """Bilinear needs a complete lattice. Handed a cloud of points it would
    silently index a grid full of zeros -- every unfilled node reading
    (0, 0), the Gulf of Guinea."""
    with pytest.raises(ValueError, match="lattice"):
        GCPGrid(grid()[:-1])


def test_a_degenerate_grid_is_refused() -> None:
    line = [FakeGCP(c, 0, -76.0 + c * 1e-4, 37.0) for c in range(5)]
    with pytest.raises(ValueError, match="2-D grid"):
        GCPGrid(line)


def test_a_partial_lattice_is_not_silently_zero_filled() -> None:
    """The specific failure the lattice check exists to prevent: a missing
    node leaves (0.0, 0.0) in the arrays, and nothing downstream can tell a
    zero-filled node from a real one."""
    holed = [g for g in grid() if not (g.row == 1000 and g.col == 1000)]
    with pytest.raises(ValueError):
        GCPGrid(holed)


# -- the validation is not circular ----------------------------------------

def test_holdout_is_used_for_the_grid() -> None:
    acc = Geolocator(curved_grid()).accuracy()
    assert acc.validation.startswith("holdout")


def test_holdout_excludes_the_points_it_was_built_from() -> None:
    """25 nodes, 9 kept to build the coarse grid, 16 held out. If the count
    came back 25 the 'holdout' would include its own control points and the
    error would be diluted toward zero by construction."""
    acc = Geolocator(curved_grid()).accuracy()
    assert acc.n == 16


def test_holdout_reports_error_where_a_self_check_would_report_zero() -> None:
    """THE CIRCULARITY TEST.

    On a curved grid, bilinear still passes through every node exactly, so
    residual-against-own-GCPs is identically zero -- a perfect score for a
    transform that is genuinely wrong between the nodes. Holdout sees the
    error the self-check cannot.
    """
    gcps = curved_grid(amp=1e-3)
    gg = GCPGrid(gcps)

    self_check = max(_haversine_m(g.y, g.x, *reversed(gg.lonlat(g.col, g.row)))
                     for g in gcps)
    assert self_check < 1e-6, "by construction -- which is the problem"

    acc = Geolocator(gcps).accuracy()
    assert acc.rms_m > 1.0, "holdout must see what the self-check cannot"


def test_holdout_is_pessimistic_not_optimistic() -> None:
    """Holdout rebuilds from every other grid line, so it measures the error
    at TWICE the real node spacing. For a smooth surface that overstates the
    true error, which is the right direction for a number you are going to
    stake results on -- an optimistic accuracy figure is how a systematic
    offset survives review.
    """
    gcps = curved_grid(n=5, step=500, amp=1e-3)
    reported = Geolocator(gcps).accuracy().rms_m

    gg = GCPGrid(gcps)
    span = 4 * 500
    truth = []
    for r in range(125, span, 313):
        for c in range(125, span, 313):
            lon, lat = gg.lonlat(c, r)
            want_lon = -76.0 + c * 1e-4 + 1e-3 * (r / span) ** 2
            truth.append(_haversine_m(37.0 + r * 1e-4, want_lon, lat, lon))

    actual = max(truth)
    assert reported > actual, (
        f"holdout {reported:.1f} m should overstate the true {actual:.1f} m")


def test_a_grid_too_coarse_to_hold_out_says_so() -> None:
    """Three grid lines cannot spare one and still be a grid. Reporting 0.0 m
    here would be a lie; 'not validated' is the honest answer, and check()
    passes it because there is nothing to object to -- the caller has been
    told the measurement did not happen."""
    acc = Geolocator(grid(n=3)).accuracy()
    assert acc.validation == "not validated"
    assert acc.n == 0


# -- refusing to guess -----------------------------------------------------

def test_no_gcps_is_an_error_not_a_default() -> None:
    """Falling back to an identity transform would put every vessel at
    latitude 0 -- which at least looks wrong. Falling back to anything
    plausible would not."""
    with pytest.raises(ValueError) as e:
        Geolocator([])
    assert "truncated" in str(e.value) or "wrong file" in str(e.value)


# -- the argument-order trap -----------------------------------------------

def test_lonlat_takes_col_then_row() -> None:
    """Rasterio transforms are x-then-y, which is COLUMN then ROW -- the
    reverse of numpy indexing and of how a Detection stores its centroid.
    Swapping them raises nothing and transposes the entire scene.
    """
    g = grid()
    loc = Displaced(g)
    lon, lat = loc.lonlat(g[1].col, g[1].row)
    assert lon == pytest.approx(g[1].x)
    assert lat == pytest.approx(g[1].y)


def test_the_grid_also_takes_col_then_row() -> None:
    """Same trap, now inside GCPGrid, where rows and columns have different
    lengths in a real scene and the swap would raise nothing either."""
    gcps = [FakeGCP(c, r, -76.0 + c * 1e-4, 37.0 + r * 1e-3)
            for r in (0, 100, 200, 300)
            for c in (0, 1000, 2000, 3000)]
    gg = GCPGrid(gcps)
    lon, lat = gg.lonlat(3000, 0)          # far in column, zero in row
    assert lon == pytest.approx(-76.0 + 0.3)
    assert lat == pytest.approx(37.0)


def test_a_transposed_transform_is_caught_by_accuracy() -> None:
    """Even if the swap happened, the residual against the GCPs would be
    enormous -- which is the whole reason accuracy() exists."""
    class Swapped(Displaced):
        def lonlat(self, col, row):
            for g in self.gcps:
                if g.col == col and g.row == row:
                    return g.y, g.x          # deliberately wrong
            raise KeyError

    assert Swapped(grid()).accuracy().rms_m > 1e6


# -- distance helper -------------------------------------------------------

def test_haversine_against_a_known_separation() -> None:
    """One degree of latitude is about 111 km anywhere on the globe."""
    assert _haversine_m(37.0, -76.0, 38.0, -76.0) == pytest.approx(111195, rel=0.01)


def test_haversine_shrinks_with_latitude_for_longitude() -> None:
    at37 = _haversine_m(37.0, -76.0, 37.0, -75.0)
    at60 = _haversine_m(60.0, -76.0, 60.0, -75.0)
    assert at60 < at37 * 0.7


# -- convergence, and the right to extrapolate -----------------------------

def curved_lattice(rows=9, cols=21, amp=0.08, h=16699, w=25468):
    """A Sentinel-1-shaped grid with smooth range curvature."""
    rs = [int(i * (h - 1) / (rows - 1)) for i in range(rows)]
    cs = [int(j * (w - 1) / (cols - 1)) for j in range(cols)]
    out = []
    for r in rs:
        for c in cs:
            u, v = c / (w - 1), r / (h - 1)
            out.append(FakeGCP(c, r,
                               -76.0 + u * 2.4 + amp * u * u + 0.15 * amp * u * v,
                               37.0 + v * 2.2 - 0.6 * amp * v * v + 0.1 * amp * u * v))
    return out


def test_convergence_is_measured_and_near_four() -> None:
    """Bilinear error scales as h^2, so doubling the node spacing should
    multiply the error by 4. Measuring it rather than assuming it is what
    turns the extrapolation below into a measurement."""
    acc = Geolocator(curved_lattice()).accuracy()
    assert acc.ratio is not None
    assert 2.5 <= acc.ratio <= 6.0


def test_the_holdout_figure_is_extrapolated_to_the_real_spacing() -> None:
    """THE FIX FOR A REAL FAILURE.

    A Chesapeake slice reported 69 m by holdout and was refused against a 50 m
    limit -- but the limit was reasoned about the error that actually applies,
    and holdout measures the error at TWICE the node spacing. Those are not
    the same quantity, and comparing them rejected a perfectly good scene.
    """
    acc = Geolocator(curved_lattice()).accuracy()
    assert acc.implied_m is not None
    assert acc.implied_m < acc.rms_m / 2, "extrapolation must actually bite"
    # best_m carries BOTH corrections: the robust centre rather than the RMS,
    # and the convergence scaling. It is the median at the real node spacing.
    assert acc.best_m == pytest.approx(
        acc.median_m * acc.implied_m / acc.rms_m)
    assert acc.best_m < acc.median_m, "convergence scaling must apply"


def test_the_extrapolation_is_close_to_the_truth() -> None:
    """Checked against error at random points, which holdout never sees."""
    import random
    amp, h, w = 0.08, 16699, 25468
    grid = GCPGrid(curved_lattice(amp=amp, h=h, w=w))
    rnd = random.Random(0)
    errs = []
    for _ in range(2000):
        c, r = rnd.uniform(0, w - 1), rnd.uniform(0, h - 1)
        u, v = c / (w - 1), r / (h - 1)
        want = (-76.0 + u * 2.4 + amp * u * u + 0.15 * amp * u * v,
                37.0 + v * 2.2 - 0.6 * amp * v * v + 0.1 * amp * u * v)
        got = grid.lonlat(c, r)
        errs.append(_haversine_m(want[1], want[0], got[1], got[0]))
    true_rms = (sum(e * e for e in errs) / len(errs)) ** 0.5

    implied = Geolocator(curved_lattice(amp=amp, h=h, w=w)).accuracy().implied_m
    assert implied >= true_rms * 0.8, "must not flatter the transform"
    assert implied < true_rms * 2.0, "and must not be uselessly pessimistic"


def test_a_planar_grid_does_not_claim_a_convergence_it_cannot_show() -> None:
    """Bilinear reproduces a plane exactly, so both holdout errors are zero
    and there is no ratio to take. Reporting one would be division by noise."""
    acc = Geolocator(grid(n=9, step=500)).accuracy()
    assert acc.rms_m == pytest.approx(0.0, abs=1e-6)
    assert acc.implied_m is None


def test_structure_finer_than_the_grid_refuses_extrapolation() -> None:
    """If the error does not shrink as the spacing halves, the surface has
    detail the published grid cannot resolve. Extrapolating then would invent
    an accuracy nobody measured, so the pessimistic figure has to stand."""
    import math
    rows, cols, h, w = 9, 21, 16699, 25468
    rs = [int(i * (h - 1) / (rows - 1)) for i in range(rows)]
    cs = [int(j * (w - 1) / (cols - 1)) for j in range(cols)]
    # Oscillation at the node spacing itself -- halving the spacing does not
    # help, because the wiggle lives between every pair of nodes.
    gcps = [FakeGCP(c, r,
                    -76.0 + c / (w - 1) * 2.4 + 0.02 * math.sin(c * 0.004),
                    37.0 + r / (h - 1) * 2.2 + 0.02 * math.cos(r * 0.006))
            for r in rs for c in cs]
    acc = Geolocator(gcps).accuracy()
    assert acc.implied_m is None, f"ratio was {acc.ratio}"
    assert acc.best_m == acc.median_m, "no extrapolation applied"


def test_check_uses_the_error_that_applies_not_the_holdout_one() -> None:
    """The regression in one line: a scene whose holdout reads above the limit
    but whose real error is well under it must pass."""
    loc = Geolocator(curved_lattice())
    acc = loc.accuracy()
    assert acc.rms_m > 40, "premise: holdout is high"
    assert loc.check(max_rms_m=40.0).best_m < 40.0


def test_both_numbers_are_printed() -> None:
    """A reader must be able to see the measured figure and the extrapolated
    one, and the convergence that connects them. Quoting only the flattering
    number is how a rescale gets mistaken for a measurement."""
    s = str(Geolocator(curved_lattice()).accuracy())
    assert "holdout" in s and "real node spacing" in s and "convergence" in s


# -- error is not uniform across a swath -----------------------------------

def spotty_lattice(rows=9, cols=21, h=16699, w=25468, bump=0.004):
    """A smooth grid with a defect at ONE node.

    This is what ten real slices actually looked like: a well-behaved surface
    with a handful of nodes bilinear cannot predict from their neighbours. The
    scene is not unusable -- it is unevenly usable.
    """
    rs = [int(i * (h - 1) / (rows - 1)) for i in range(rows)]
    cs = [int(j * (w - 1) / (cols - 1)) for j in range(cols)]
    out = []
    for i, r in enumerate(rs):
        for j, c in enumerate(cs):
            u, v = c / (w - 1), r / (h - 1)
            # Curvature in BOTH axes plus a cross-term. Without the azimuth
            # terms, lat is linear in v and lon does not depend on v at all,
            # so every held-out row is predicted exactly and half the error
            # distribution is identically zero -- which makes the median zero
            # and the test below vacuous. Real SAR geometry curves both ways.
            lon = -76.0 + u * 2.4 + 0.02 * u * u + 0.006 * u * v
            lat = 37.0 + v * 2.2 - 0.015 * v * v + 0.004 * u * v
            if (i, j) == (3, 9):
                lon += bump                 # the defect
            out.append(FakeGCP(c, r, lon, lat))
    return out, rs, cs


def test_the_error_field_says_where_the_error_is() -> None:
    gcps, rs, cs = spotty_lattice()
    field = Geolocator(gcps).error_field()
    assert field, "holdout produced no measurements"
    worst = max(field, key=field.get)
    assert worst == (rs[3], cs[9]), f"defect not located; worst at {worst}"


def test_a_local_defect_does_not_move_the_median() -> None:
    """RMS squares its errors, so a dozen bad nodes out of 144 drag it far
    above what a detection anywhere else experiences -- and a scene-wide
    figure is what every detection would otherwise inherit."""
    clean = Geolocator(spotty_lattice(bump=0.0)[0]).accuracy()
    spotty = Geolocator(spotty_lattice()[0]).accuracy()
    assert spotty.worst_m > clean.worst_m * 5, "premise: a real defect"
    assert spotty.median_m == pytest.approx(clean.median_m, rel=0.25)
    assert spotty.rms_m > clean.rms_m * 1.5, "RMS is the one that moves"


def test_a_concentrated_error_is_flagged_as_uneven() -> None:
    assert Geolocator(spotty_lattice()[0]).accuracy().uneven
    assert not Geolocator(spotty_lattice(bump=0.0)[0]).accuracy().uneven


def test_local_error_is_large_at_the_defect_and_small_away_from_it() -> None:
    """THE POINT OF THE FILE.

    Handing every detection the scene's RMS makes the good majority look worse
    than they are and the few bad ones look better. The second half is how a
    displaced detection reaches matching.py wearing a small uncertainty and
    gets counted as a dark vessel.
    """
    gcps, rs, cs = spotty_lattice()
    loc = Geolocator(gcps)
    at_defect = loc.local_error_m(cs[9], rs[3])
    far_away = loc.local_error_m(cs[18], rs[7])
    assert at_defect > far_away * 5, f"{at_defect:.1f} vs {far_away:.1f}"


def test_local_error_is_conservative_between_nodes() -> None:
    """The worst bracketing node, not an average. Between two nodes the
    interpolation error can exceed either endpoint's, so averaging would
    understate it -- and understating uncertainty manufactures findings."""
    gcps, rs, cs = spotty_lattice()
    loc = Geolocator(gcps)
    midway = loc.local_error_m((cs[9] + cs[10]) / 2, rs[3])
    assert midway >= loc.local_error_m(cs[10], rs[3])


def test_local_error_never_returns_zero_for_a_real_scene() -> None:
    """Zero uncertainty is a claim of perfection. Every query must land on
    some measured node, or fall back to the scene figure."""
    gcps, rs, cs = spotty_lattice()
    loc = Geolocator(gcps)
    for col, row in ((0, 0), (cs[-1], rs[-1]), (12345, 6789)):
        assert loc.local_error_m(col, row) > 0


def test_local_error_falls_back_rather_than_raising_off_grid() -> None:
    loc = Geolocator(spotty_lattice()[0])
    assert loc.local_error_m(-500, -500) > 0
    assert loc.local_error_m(10**7, 10**7) > 0


def test_accuracy_is_computed_once() -> None:
    """local_error_m calls it per detection, and a scene has thousands."""
    loc = Geolocator(spotty_lattice()[0])
    assert loc.accuracy() is loc.accuracy()


def test_every_field_survives_the_convergence_branch() -> None:
    """THE BUG THIS CATCHES.

    accuracy() builds an Accuracy, then rebuilds it when convergence is
    confirmed. Listing the fields by hand in that second construction dropped
    median_m and p90_m when they were added: they took their zero defaults,
    only on scenes that converged, and nothing raised. The well-behaved half
    of the archive reported a median error of 0.0 m.
    """
    acc = Geolocator(curved_lattice()).accuracy()
    assert acc.implied_m is not None, "premise: this scene converges"
    assert acc.median_m > 0, "median lost in the convergence branch"
    assert acc.p90_m > 0, "p90 lost in the convergence branch"
    assert acc.p90_m >= acc.median_m


def test_no_reported_field_is_left_at_its_default() -> None:
    """A structural version of the same check: if a field is added later and
    the rebuild forgets it, this fails without anyone remembering to look."""
    import dataclasses
    acc = Geolocator(curved_lattice()).accuracy()
    defaults = {f.name: f.default for f in dataclasses.fields(Accuracy)
                if f.default is not dataclasses.MISSING and f.default == 0.0}
    for name in defaults:
        assert getattr(acc, name) != 0.0, f"{name} is still at its default"
