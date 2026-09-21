"""Pixel to latitude and longitude, and proof that it worked.

PHASE 3.

THE PROBLEM

A Sentinel-1 GRD raster is in radar geometry. It is not north-up, it has no
affine geotransform, and its rows and columns are range and azimuth rather
than anything on a map. Position is carried instead as a GEOLOCATION GRID --
210 ground control points laid out in a regular lattice across the scene, each
tying one pixel to one latitude and longitude. Everything in between is
interpolation.

Get that interpolation wrong and nothing errors. The detections still land in
the Atlantic, still look like vessels, still line up roughly with shipping
lanes. They are simply in the wrong place, by an amount nobody can see. Then
matching.py compares them to AIS, finds no partner within the tolerance, and
reports a sea full of dark vessels.

WHY NOT GDAL'S GCP TRANSFORMER

Because it is a POLYNOMIAL FIT, not an interpolator. Given 210 control points
it solves a third-order polynomial in least squares -- one smooth surface
across a 250 km swath. Measured against the scene's own GCPs on a real
Chesapeake acquisition it came back at 54 m RMS, 5.4 pixels. That is not a
bug in GDAL; a global polynomial simply cannot follow the geometry of a
sensor's range-azimuth grid.

The grid is not a cloud of scattered control points. It is a regular lattice,
published by ESA for exactly this purpose, and bilinear interpolation across
it passes through every node exactly and stays sub-pixel between them.

VALIDATING AN INTERPOLATOR HONESTLY

A transform that passes through every GCP by construction scores zero error
against those same GCPs. That check is vacuous -- it cannot fail, which means
it proves nothing.

So the accuracy of an interpolator is measured by HOLDOUT: rebuild it from
half the grid lines and predict the ones left out. That reports the error at
twice the real node spacing, so it is pessimistic, which is the right
direction for a number you are going to trust your results to.

A fitted transform, having no such circularity, is still measured directly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

EARTH_R = 6371008.8

# Bilinear error scales as h^2, so doubling the node spacing should multiply
# the error by 4. Measured on real Sentinel-1 grids it lands near but not on
# that -- 3.65 on a Chesapeake slice -- because the geometry is smooth rather
# than perfectly quadratic. This band accepts "close enough to h^2 to
# extrapolate one step" and refuses anything else, including a ratio near 1
# (error not shrinking at all: the surface has structure finer than the grid,
# and no amount of interpolation will find it).
MIN_CONVERGENCE = 2.5
MAX_CONVERGENCE = 6.0


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def _percentile(vals: list[float], pct: float) -> float:
    """Linear-interpolated percentile. No numpy: this module has no array work
    in it and one import for one statistic is a poor trade."""
    if not vals:
        return 0.0
    xs = sorted(vals)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * pct / 100.0
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


class GeolocationError(RuntimeError):
    """The pixel-to-position transform is not accurate enough to use."""


@dataclass(frozen=True)
class Accuracy:
    """How far the transform puts known points from where they actually are.

    TWO NUMBERS, AND THEY ARE NOT THE SAME QUANTITY.

    rms_m is what holdout measured -- the error at TWICE the real node
    spacing, because half the grid lines were withheld to have something
    honest to test against.

    implied_m is the error at the spacing actually used, obtained by measuring
    how the error shrinks as the spacing halves and extrapolating one step
    further. It is None when that convergence could not be confirmed, and then
    there is only the pessimistic figure, which is the right thing to have
    when the optimistic one cannot be justified.
    """

    n: int
    rms_m: float
    worst_m: float
    method: str
    validation: str        # "holdout" or "residual"
    ratio: float | None = None       # err(4h)/err(2h); 4.0 means h^2
    implied_m: float | None = None   # extrapolated to the true node spacing
    median_m: float = 0.0            # robust centre, unmoved by a few bad nodes
    p90_m: float = 0.0

    @property
    def uneven(self) -> bool:
        """Is the error concentrated in a few places rather than spread?

        Measured across ten real Sentinel-1 slices, a well-behaved grid has a
        worst case under twice its RMS. The slices that failed had three to
        five times, and their error stopped shrinking as the grid refined --
        the signature of a handful of nodes bilinear cannot predict from their
        neighbours at any spacing, not of a uniformly coarser surface.

        A scene like that is not unusable. It is unevenly usable, which is a
        different thing and has to be handled per detection rather than by
        one number for 250 km of swath.
        """
        return self.median_m > 0 and self.worst_m > 3.0 * self.median_m

    @property
    def best_m(self) -> float:
        """The most defensible estimate of the error that actually applies.

        The MEDIAN, not the RMS, once there is one. RMS squares its errors, so
        a dozen bad nodes out of 144 drag it far above what a detection
        anywhere else in the scene experiences -- and a scene-wide figure is
        what every detection would otherwise inherit. The outliers are not
        discarded: they are handled where they actually are, by
        Geolocator.local_error_m.
        """
        base = self.median_m or self.rms_m
        if self.implied_m is None or self.rms_m <= 0:
            return base
        return base * (self.implied_m / self.rms_m)

    @property
    def pixels(self) -> float:
        """best_m in 10 m GRD pixels."""
        return self.best_m / 10.0

    def __str__(self) -> str:
        head = (f"{self.method}: median {self.median_m:.1f} m, "
                f"p90 {self.p90_m:.1f} m, worst {self.worst_m:.1f} m "
                f"({self.rms_m:.1f} RMS), {self.n} pts, {self.validation}")
        tail = ""
        if self.implied_m is not None:
            tail = (f"\n               -> {self.best_m:.1f} m "
                    f"({self.pixels:.2f} px) at the real node spacing, "
                    f"convergence {self.ratio:.2f}x")
        if self.uneven:
            tail += ("\n               UNEVEN: error is concentrated, not "
                     "spread -- per-detection uncertainty applies")
        return head + (tail or f" ({self.pixels:.2f} px)")


# --------------------------------------------------------------------------
# the geolocation grid
# --------------------------------------------------------------------------

class GCPGrid:
    """Bilinear interpolation across Sentinel-1's regular geolocation grid.

    Exact at every node, sub-pixel between them, and cheap -- a bisection and
    four multiplies per query.

    Requires the GCPs to actually form a lattice. They do for Sentinel-1 GRD;
    if a future product ships scattered points this raises rather than
    interpolating nonsense across a grid that is not there.
    """

    def __init__(self, gcps) -> None:
        rows = sorted({g.row for g in gcps})
        cols = sorted({g.col for g in gcps})
        if len(rows) < 2 or len(cols) < 2:
            raise ValueError("GCPs do not form a 2-D grid")
        if len(rows) * len(cols) != len(gcps):
            raise ValueError(
                f"GCPs are not a complete lattice: {len(rows)} distinct rows x "
                f"{len(cols)} distinct cols = {len(rows) * len(cols)}, but "
                f"{len(gcps)} points. Bilinear interpolation needs a full grid."
            )

        self.rows, self.cols = rows, cols
        self._ri = {v: i for i, v in enumerate(rows)}
        self._ci = {v: i for i, v in enumerate(cols)}
        self.lon = [[0.0] * len(cols) for _ in rows]
        self.lat = [[0.0] * len(cols) for _ in rows]
        for g in gcps:
            i, j = self._ri[g.row], self._ci[g.col]
            self.lon[i][j] = g.x
            self.lat[i][j] = g.y

    @staticmethod
    def _bracket(vals: list[float], v: float) -> tuple[int, int, float]:
        """Indices either side of v, and the fraction between them.

        Clamps outside the grid rather than extrapolating. A detection cannot
        be outside the raster, so out-of-range here means a bug upstream, and
        extrapolating a bilinear surface is a fast way to put a vessel in
        Greenland.
        """
        if v <= vals[0]:
            return 0, 0, 0.0
        if v >= vals[-1]:
            n = len(vals) - 1
            return n, n, 0.0
        lo, hi = 0, len(vals) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if vals[mid] <= v:
                lo = mid
            else:
                hi = mid
        span = vals[hi] - vals[lo]
        return lo, hi, (v - vals[lo]) / span if span else 0.0

    def lonlat(self, col: float, row: float) -> tuple[float, float]:
        i0, i1, fr = self._bracket(self.rows, row)
        j0, j1, fc = self._bracket(self.cols, col)

        def interp(grid):
            top = grid[i0][j0] * (1 - fc) + grid[i0][j1] * fc
            bot = grid[i1][j0] * (1 - fc) + grid[i1][j1] * fc
            return top * (1 - fr) + bot * fr

        return interp(self.lon), interp(self.lat)


# --------------------------------------------------------------------------
# the locator
# --------------------------------------------------------------------------

class Geolocator:
    """Pixel -> lon/lat, with a measured and non-circular accuracy.

    Prefers the geolocation grid. Falls back to GDAL's polynomial GCP
    transform, then to a single fitted affine -- each worse than the last, and
    accuracy() will say so in metres rather than leaving it to be discovered
    downstream.
    """

    def __init__(self, gcps, crs=None) -> None:
        self.gcps = list(gcps)
        self.crs = crs
        self._grid = None
        self._gdal = None
        self._affine = None
        self._field = None          # lazily built by error_field()
        self._acc = None            # accuracy() is called per detection

        if not self.gcps:
            raise ValueError(
                "this raster carries no GCPs, so pixels cannot be positioned. "
                "A Sentinel-1 GRD always has them; an empty list means the "
                "wrong file or a truncated download."
            )

        try:
            self._grid = GCPGrid(self.gcps)
            self.method = "geolocation grid (bilinear)"
            return
        except ValueError:
            pass

        try:
            from rasterio.transform import GCPTransformer
            self._gdal = GCPTransformer(self.gcps)
            self.method = "GDAL GCP polynomial"
        except Exception:
            from rasterio.transform import from_gcps
            self._affine = from_gcps(self.gcps)
            self.method = "fitted affine (POOR)"

    # -- conversion --------------------------------------------------------

    def lonlat(self, col: float, row: float) -> tuple[float, float]:
        """Pixel (col, row) -> (lon, lat).

        Note the argument order. Rasterio transforms take x then y, which is
        COLUMN then ROW -- the opposite of the (row, col) that indexes a numpy
        array, and of how a Detection stores its centroid. Swapping them does
        not raise; it silently transposes the scene.
        """
        if self._grid is not None:
            return self._grid.lonlat(col, row)
        if self._gdal is not None:
            x, y = self._gdal.xy(row, col)
            return float(x), float(y)
        x, y = self._affine * (col, row)
        return float(x), float(y)

    # -- proof -------------------------------------------------------------

    def accuracy(self) -> Accuracy:
        """Measured error, by holdout for interpolators and residual for fits.

        THE CIRCULARITY THIS AVOIDS. A bilinear grid passes through every GCP
        by construction, so comparing it to those same GCPs returns zero --
        a check that cannot fail and therefore says nothing. Rebuilding from
        half the grid lines and predicting the other half is a real test, and
        it reports the error at twice the true node spacing, so it errs
        pessimistic.
        """
        if self._acc is not None:
            return self._acc
        self._acc = self._accuracy()
        return self._acc

    def _accuracy(self) -> Accuracy:
        if self._grid is None:
            errs = []
            for g in self.gcps:
                lon, lat = self.lonlat(g.col, g.row)
                errs.append(_haversine_m(g.y, g.x, lat, lon))
            return self._score(errs, "residual")

        rows, cols = self._grid.rows, self._grid.cols
        if len(rows) < 4 or len(cols) < 4:
            # Too coarse to hold anything out and still have a grid.
            return Accuracy(0, 0.0, 0.0, self.method, "not validated")

        errs2 = self._holdout(2)
        acc = self._score(errs2, "holdout at 2x spacing")
        if not errs2:
            return acc

        # CONVERGENCE. Holdout can only measure the error at twice the real
        # spacing, and the honest response has been to quote that and accept
        # it is pessimistic. How pessimistic is itself measurable: bilinear
        # error on a smooth surface scales as h^2, so halving the spacing
        # should quarter the error. Measure at 4h as well and the ratio says
        # whether that law holds HERE. If it does, extrapolating one step to
        # the real spacing is a measurement rather than an assumption.
        #
        # This is the difference between rescaling past a failed check and
        # earning the right to. A ratio far from 4 means the surface is not
        # smooth at this scale, the extrapolation is invalid, and the
        # pessimistic number stands.
        errs4 = self._holdout(4)
        if len(errs4) < 4:
            return acc
        rms4 = math.sqrt(sum(e * e for e in errs4) / len(errs4))
        if acc.rms_m <= 0 or rms4 <= 0:
            return acc

        ratio = rms4 / acc.rms_m
        if not (MIN_CONVERGENCE <= ratio <= MAX_CONVERGENCE):
            return acc                        # not h^2 here; do not extrapolate

        # replace(), not a fresh Accuracy(...). Listing fields by hand here
        # silently dropped median_m and p90_m when they were added -- they
        # took their zero defaults, and only on scenes that CONVERGED, so the
        # well-behaved half of the archive reported a median of 0.0 m and
        # nothing raised. A constructor call in a second place is a field
        # nobody will remember twice.
        return replace(acc, ratio=ratio, implied_m=acc.rms_m / ratio)

    def _holdout(self, step: int) -> list[float]:
        """Error at `step` times the real node spacing.

        Rebuilds the grid from every `step`-th line and scores the lines left
        out. The last row and column are always kept so the coarse grid spans
        the full scene -- without them the excluded nodes near two edges would
        be extrapolated rather than interpolated, and clamping would make the
        error look like a property of the surface instead of of the test.
        """
        rows, cols = self._grid.rows, self._grid.cols
        keep_r = set(rows[::step]) | {rows[-1]}
        keep_c = set(cols[::step]) | {cols[-1]}
        if len(keep_r) < 2 or len(keep_c) < 2:
            return []

        coarse = GCPGrid([g for g in self.gcps
                          if g.row in keep_r and g.col in keep_c])
        return [_haversine_m(g.y, g.x, *reversed(coarse.lonlat(g.col, g.row)))
                for g in self.gcps
                if not (g.row in keep_r and g.col in keep_c)]

    def _score(self, errs: list[float], how: str) -> Accuracy:
        if not errs:
            return Accuracy(0, 0.0, 0.0, self.method, "not validated")
        rms = math.sqrt(sum(e * e for e in errs) / len(errs))
        return Accuracy(len(errs), rms, max(errs), self.method, how,
                        median_m=_percentile(errs, 50),
                        p90_m=_percentile(errs, 90))

    # -- where the error is, not just how big --------------------------------

    def error_field(self) -> dict[tuple[float, float], float]:
        """Holdout error at every node the coarse grid did not build from.

        Cached. Keyed (row, col). Nodes the coarse grid DID use have no
        measurement -- bilinear reproduces them exactly by construction, which
        says nothing -- so they are absent here and filled in by neighbours in
        local_error_m.
        """
        if self._field is not None:
            return self._field
        self._field = {}
        if self._grid is None:
            return self._field

        rows, cols = self._grid.rows, self._grid.cols
        keep_r = set(rows[::2]) | {rows[-1]}
        keep_c = set(cols[::2]) | {cols[-1]}
        coarse = GCPGrid([g for g in self.gcps
                          if g.row in keep_r and g.col in keep_c])
        for g in self.gcps:
            if g.row in keep_r and g.col in keep_c:
                continue
            lon, lat = coarse.lonlat(g.col, g.row)
            self._field[(g.row, g.col)] = _haversine_m(g.y, g.x, lat, lon)
        return self._field

    def local_error_m(self, col: float, row: float) -> float:
        """Geolocation error expected AT THIS PIXEL, in metres.

        THE REASON THIS EXISTS. Across ten real slices the error was not
        uniform: a scene whose median node error was ~20 m had individual
        nodes at 240 m, and which of those a detection inherits depends
        entirely on where in the swath it sits. Handing every detection the
        scene's RMS makes the good majority look worse than they are and the
        few bad ones look better -- and the second half of that is how a
        displaced detection reaches matching.py wearing a small uncertainty
        and gets counted as a dark vessel.

        Conservative by construction: the WORST measured node bracketing the
        query, not an average of them. Between two nodes the interpolation
        error can exceed either endpoint's, so averaging would understate it,
        and understating uncertainty is the direction that manufactures
        findings.
        """
        field = self.error_field()
        if not field or self._grid is None:
            acc = self.accuracy()
            return acc.best_m

        rows, cols = self._grid.rows, self._grid.cols
        i0, i1, _ = GCPGrid._bracket(rows, row)
        j0, j1, _ = GCPGrid._bracket(cols, col)

        # Widen by one node each way: a kept node carries no measurement, so a
        # query sitting between two kept nodes would otherwise find nothing.
        near = []
        for i in range(max(0, i0 - 1), min(len(rows), i1 + 2)):
            for j in range(max(0, j0 - 1), min(len(cols), j1 + 2)):
                e = field.get((rows[i], cols[j]))
                if e is not None:
                    near.append(e)

        acc = self.accuracy()
        scale = 1.0
        if acc.implied_m is not None and acc.rms_m > 0:
            scale = acc.implied_m / acc.rms_m
        if not near:
            return acc.best_m
        return max(near) * scale

    def check(self, *, max_rms_m: float = 50.0) -> Accuracy:
        """accuracy(), but refuse to continue if it is bad.

        Fifty metres is five GRD pixels. AIS matching will tolerate a few
        hundred metres -- an AIS fix interpolated to the radar instant, plus
        half a hull length between the transponder and the detection centroid
        -- so 50 m is comfortably inside the noise. But a GEOLOCATION error is
        systematic rather than random, and a systematic offset biases every
        comparison in the same direction, which is how it turns into a finding.
        """
        acc = self.accuracy()
        if acc.best_m > max_rms_m:
            extra = ""
            if acc.implied_m is None and acc.validation.startswith("holdout"):
                extra = (
                    "\n  This is the HOLDOUT figure, at twice the real node "
                    "spacing, and convergence could not be confirmed, so it "
                    "could not be extrapolated. Either the grid is too small "
                    "to test at 4x spacing, or the error is not shrinking as "
                    "h^2 -- which would mean the geometry has structure finer "
                    "than the published grid."
                )
            raise GeolocationError(
                f"geolocation is {acc.best_m:.0f} m ({acc.pixels:.1f} pixels) "
                f"using {acc.method}, validated by {acc.validation}. "
                f"Above the {max_rms_m:.0f} m limit.{extra}\n"
                f"  A systematic offset biases every AIS comparison the same "
                f"way, which matching.py would read as dark vessels.\n"
                f"  If the method above is not 'geolocation grid', the GCPs "
                f"did not form a lattice and the fallback is a polynomial fit "
                f"across a 250 km swath -- inspect the GCP layout rather than "
                f"raising this limit."
            )
        return acc
