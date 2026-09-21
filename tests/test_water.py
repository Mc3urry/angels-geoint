"""Tests for the sea/land split.

The mask that this replaces did not crash, did not warn, and returned 4,029
detections from six tiles of a real Chesapeake scene -- the densest clusters
being in the fields outside Upper Marlboro, Maryland and Smyrna, Delaware.
It failed by working: every number downstream was produced, formatted and
plotted exactly as it would have been if the mask were right.

So these tests are mostly about the ways a threshold can be confidently wrong.
"""

from __future__ import annotations

import numpy as np
import pytest

from angels.adapters.maritime import water
from angels.adapters.maritime.water import MaskError, WaterMask, otsu


class FakeRaster:
    """The two attributes and one method water.from_raster actually uses.

    Decimated reads are done by block-averaging, which is what rasterio's
    overview pyramid amounts to. No file, no rasterio, no gigabyte.
    """

    def __init__(self, arr):
        self.arr = np.asarray(arr, dtype=np.float64)
        self.height, self.width = self.arr.shape

    def read(self, band, *, out_shape, masked=False):
        h, w = out_shape
        rows = np.linspace(0, self.height, h + 1).astype(int)
        cols = np.linspace(0, self.width, w + 1).astype(int)
        out = np.empty((h, w))
        for i in range(h):
            for j in range(w):
                out[i, j] = self.arr[rows[i]:rows[i + 1],
                                     cols[j]:cols[j + 1]].mean()
        return out


def coastal_scene(h=640, w=640, coast=320, sea=120.0, land=2400.0, seed=0):
    """Dark water on the left, bright land on the right, with speckle.

    SAR amplitude is Rayleigh-ish, so multiplicative noise rather than
    additive -- a scene with additive noise would separate far too cleanly and
    the test would pass for the wrong reason.
    """
    rng = np.random.default_rng(seed)
    base = np.where(np.arange(w)[None, :] < coast, sea, land)
    return np.broadcast_to(base, (h, w)) * rng.rayleigh(1.0, (h, w)) / 1.2533


def open_ocean(h=640, w=640, sea=120.0, seed=1):
    rng = np.random.default_rng(seed)
    return sea * rng.rayleigh(1.0, (h, w)) / 1.2533


# -- otsu itself -----------------------------------------------------------

def test_otsu_splits_two_separated_modes() -> None:
    v = np.concatenate([np.full(1000, 1.0), np.full(1000, 9.0)])
    t, _ = otsu(v)
    assert 1.0 < t < 9.0


def test_otsu_reports_the_variance_it_achieved() -> None:
    """The threshold on its own is not evidence. Otsu minimises, and a
    minimisation over one mode still returns a minimum -- the between-class
    variance is what distinguishes a real split from a bisected blob."""
    two = np.concatenate([np.zeros(1000), np.full(1000, 10.0)])
    one = np.random.default_rng(0).normal(5.0, 0.3, 2000)
    assert otsu(two)[1] > otsu(one)[1] * 10


def test_otsu_on_nothing_is_an_error() -> None:
    with pytest.raises(MaskError):
        otsu(np.array([np.nan, np.inf]))


# -- the ordinary case -----------------------------------------------------

def test_a_coastal_scene_is_split() -> None:
    m = water.from_raster(FakeRaster(coastal_scene()), decimation=8,
                          buffer_m=0)
    assert m.bimodal
    assert m.contrast > water.MIN_CONTRAST
    assert 0.4 < m.water_fraction < 0.6


def test_the_water_is_on_the_water_side() -> None:
    """A mask that is correct in extent and inverted in sense passes every
    fraction-based check and masks precisely the sea."""
    m = water.from_raster(FakeRaster(coastal_scene()), decimation=8,
                          buffer_m=0)
    left = m.tile(0, 0, 640, 320)
    right = m.tile(0, 320, 640, 320)
    assert left.mean() > 0.95, "open water must be testable"
    assert right.mean() < 0.05, "land must not be"


# -- the failure that has to be caught -------------------------------------

def test_open_ocean_is_not_cut_in_half() -> None:
    """THE ORIGINAL POINT OF THE FILE.

    Otsu handed one mode returns a threshold through the middle of it and
    would declare half the Atlantic to be land. Nothing errors, the mask looks
    plausible in a plot, and half of every scene silently stops being searched
    -- an absence of detections that means nothing at all.

    That is still caught. What CHANGED is the response: it used to return "all
    water", and that fallback is what searched inland Pennsylvania. One mode
    is now refused outright, because the mode could be either one.
    """
    with pytest.raises(MaskError, match="ONE mode"):
        water.from_raster(FakeRaster(open_ocean()), decimation=8)


def test_a_scene_that_is_all_land_is_refused() -> None:
    """Not a maritime scene. Returning an empty sea would be read downstream
    as 'no vessels present', which is a finding rather than a mistake."""
    rng = np.random.default_rng(2)
    arr = 2400.0 * rng.rayleigh(1.0, (640, 640))
    arr[:, :20] = 60.0                       # a river, ~3% of the scene
    with pytest.raises(MaskError, match="fields"):
        water.from_raster(FakeRaster(arr), decimation=8, buffer_m=2000)


def test_an_empty_raster_is_refused() -> None:
    with pytest.raises(MaskError, match="truncated"):
        water.from_raster(FakeRaster(np.zeros((64, 64))), decimation=8)


def test_unilluminated_corners_do_not_drag_the_threshold() -> None:
    """The swath is a parallelogram inside a rectangle, so the corners are
    exact zeros -- darker than water. Left in the histogram they pull the
    water mode down and the threshold with it, and the sea starts to look
    like land."""
    arr = coastal_scene()
    arr[:200, :200] = 0.0                    # unilluminated corner
    m = water.from_raster(FakeRaster(arr), decimation=8, buffer_m=0)
    assert m.bimodal
    assert m.tile(400, 0, 200, 200).mean() > 0.9, "real sea still testable"
    assert m.tile(0, 0, 200, 200).mean() < 0.05, "zeros are not sea"


def test_the_per_tile_threshold_this_replaced_inverts_on_land() -> None:
    """The regression, kept because the number is the argument.

    The old mask compared each pixel's local mean against three times the
    MEDIAN OF ITS OWN TILE. On a tile that is mostly water the median is the
    sea and land stands above it. On a tile that is mostly land the median is
    land, the threshold rises with it, and the mask protects exactly what it
    was built to exclude -- while still trimming a fifth of genuine sea on the
    tiles where it appears to work. Both errors point the same way: more
    detections, in the wrong places, with nothing in the output to say so.
    """
    from angels.adapters.maritime import cfar

    def old_mask(tile, window=201, factor=3.0):
        mu, _ = cfar.local_stats(tile, guard=1, background=window)
        return mu < float(np.median(mu)) * factor

    mostly_land = coastal_scene(h=512, w=512, coast=40)     # 8% sea
    mostly_sea = coastal_scene(h=512, w=512, coast=472)     # 92% sea

    assert old_mask(mostly_land).mean() > 0.99, (
        "a mostly-land tile was passed through whole -- this is the bug")
    assert old_mask(mostly_sea).mean() < 0.85, (
        "and a mostly-sea tile lost sea it should have kept")

    # The replacement sees the same coast the same way in both cases, because
    # the threshold does not come from the tile.
    for arr, want_sea in ((mostly_land, 40 / 512), (mostly_sea, 472 / 512)):
        m = water.from_raster(FakeRaster(arr), decimation=8, buffer_m=0)
        got = m.tile(0, 0, 512, 512).mean()
        assert abs(got - want_sea) < 0.05, f"{got:.3f} vs {want_sea:.3f}"


# -- the buffer ------------------------------------------------------------

def test_land_is_buffered_seaward() -> None:
    """A coastline inside a CFAR background ring inflates sigma and suppresses
    genuine detections offshore. The buffer trades a known blind band for an
    unknown suppression."""
    raster = FakeRaster(coastal_scene())
    raw = water.from_raster(raster, decimation=8, buffer_m=0)
    buffered = water.from_raster(raster, decimation=8, buffer_m=800)
    assert buffered.water_fraction < raw.water_fraction
    assert buffered.water.sum() < raw.water.sum()


def test_the_buffer_eats_the_coast_not_the_open_sea() -> None:
    raster = FakeRaster(coastal_scene())
    b = water.from_raster(raster, decimation=8, buffer_m=800)
    assert b.tile(0, 0, 640, 100).mean() > 0.95, "far offshore is untouched"
    assert b.tile(0, 240, 640, 80).mean() < 0.5, "the coastal band is not"


def test_the_default_buffer_covers_the_cfar_background_window() -> None:
    """Half the background window is the distance at which a coastline stops
    contaminating a ring. Anything less leaves the contamination in, and the
    detector goes quiet near shore for a reason nobody would look for."""
    from angels.adapters.maritime import cfar
    raster = FakeRaster(coastal_scene())
    default = water.from_raster(raster, decimation=8)
    explicit = water.from_raster(raster, decimation=8,
                                 buffer_m=cfar.BACKGROUND / 2 * 10.0)
    assert default.water_fraction == explicit.water_fraction


# -- upsampling ------------------------------------------------------------

def test_tile_returns_full_resolution_for_the_window_asked_for() -> None:
    m = water.from_raster(FakeRaster(coastal_scene()), decimation=8,
                          buffer_m=0)
    assert m.tile(100, 200, 300, 400).shape == (300, 400)


def test_tile_clamps_past_the_edge() -> None:
    """The last tile of a scene runs past the decimated grid by up to one
    cell. Indexing off the end would raise; wrapping would put the far side of
    the scene at the near edge."""
    m = water.from_raster(FakeRaster(coastal_scene()), decimation=8,
                          buffer_m=0)
    t = m.tile(600, 600, 100, 100)
    assert t.shape == (100, 100)


def test_tile_offsets_are_row_then_col() -> None:
    """Asymmetric on purpose: a scene is 25,468 x 16,699, and swapping the
    offsets on a square tile would go unnoticed for as long as the tiles are
    square -- which is until the last row and column of every scene."""
    arr = coastal_scene(h=320, w=640, coast=320)
    m = water.from_raster(FakeRaster(arr), decimation=8, buffer_m=0)
    assert m.tile(0, 0, 320, 320).mean() > 0.95        # sea half
    assert m.tile(0, 320, 320, 320).mean() < 0.05      # land half


# -- what it says for itself -----------------------------------------------


def test_the_mask_is_small_enough_to_keep() -> None:
    """A full-resolution boolean for a 425 MP scene is 425 MB. Holding the
    mask decimated is what makes a scene-wide threshold affordable at all."""
    m = water.from_raster(FakeRaster(coastal_scene()), decimation=8)
    assert m.water.size == (640 // 8) ** 2
    assert isinstance(m, WaterMask)


# -- which mode is it? -----------------------------------------------------

def textured_land(h=640, w=640, level=2400.0, seed=7, spread=2.0):
    """Land with real structure at the decimated scale.

    Fields, towns and ridges survive averaging 256 pixels; sea speckle does
    not. That difference is the only thing separating the two cases when Otsu
    finds a single mode.
    """
    rng = np.random.default_rng(seed)
    blocks = rng.uniform(1 / spread, spread, (16, 16))
    base = np.kron(blocks, np.ones((h // 16, w // 16))) * level
    return base * rng.rayleigh(1.0, (h, w)) / 1.2533


def test_a_single_mode_is_refused_whichever_mode_it_is() -> None:
    """THE 112,954 BUG.

    Otsu finds one mode in a scene that is all land, exactly as it does in one
    that is all ocean. The first version assumed ocean and returned "all
    water", so a slice covering inland Pennsylvania was searched for ships and
    produced 112,954 detections of towns, roads and silos -- none inside the
    study area, all formatted exactly like vessels.

    Both cases now refuse. Failing to tell land from sea is not a reason to
    call it sea: that is the more dangerous of the two defaults, because
    unmasked land does not look like an error downstream, it looks like
    traffic.
    """
    for arr in (open_ocean(), textured_land(spread=1.4)):
        with pytest.raises(MaskError, match="ONE mode"):
            water.from_raster(FakeRaster(arr), decimation=8)


def test_texture_is_reported_but_not_decided_on() -> None:
    """Measured on synthetic land the two populations overlap between 0.15 and
    0.30, and real ocean carrying wind streaks and rain cells sits inside that
    band. A threshold placed there is a coin toss wearing a decimal point, so
    the number is evidence for a human and nothing branches on it."""
    import inspect
    src = inspect.getsource(water.from_raster)
    assert "TYPICAL_OCEAN_CV" in src
    assert "cv >" not in src and "cv <" not in src, "texture must not decide"


def test_the_refusal_offers_the_way_forward() -> None:
    with pytest.raises(MaskError) as e:
        water.from_raster(FakeRaster(open_ocean()), decimation=8)
    msg = str(e.value)
    assert "--no-land-mask" in msg, "a real open-water slice must have a route"
    assert "slice" in msg, "the outer slices of a pass often miss the coast"






def test_texture_is_reported_on_a_normal_scene_too() -> None:
    """Carried even when bimodality succeeded, so the number can be checked
    against real scenes rather than trusted from one synthetic."""
    m = water.from_raster(FakeRaster(coastal_scene()), decimation=8)
    assert m.bimodal and m.texture > 0


def test_str_describes_a_scene_that_was_split() -> None:
    m = water.from_raster(FakeRaster(coastal_scene()), decimation=8)
    assert "contrast" in str(m) and "water" in str(m)


def test_the_two_single_mode_cases_genuinely_overlap() -> None:
    """The measurement that killed the texture threshold, kept as the reason.

    Synthetic land with modest relief sits at a decimated CV of 0.16 to 0.26.
    Calm synthetic ocean sits at 0.07 -- but real ocean carrying wind streaks,
    swell and rain cells runs well into that range. Any constant between them
    separates these two fixtures and nothing else, which is why from_raster
    refuses instead of choosing.
    """
    def cv(arr):
        small = FakeRaster(arr).read(1, out_shape=(80, 80))
        return float(small.std() / small.mean())

    ocean = cv(open_ocean())
    lands = [cv(textured_land(spread=s, seed=s_i))
             for s_i, s in enumerate((1.3, 1.5, 1.7), start=1)]
    assert ocean < min(lands), "the fixtures do differ"
    assert min(lands) < 0.30, (
        f"but land reaches down to {min(lands):.2f}, inside the range a "
        f"weathered sea occupies -- so no threshold here is safe")
