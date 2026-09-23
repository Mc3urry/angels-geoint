"""Tests for the fallback shoreline.

It exists because mask refusals are weather-correlated: slice 1 failed on
five of twelve 2024 dates, always in high wind, so the coverage that
vanished was the coverage on rough days. The tests are about the two ways a
fallback can be worse than no fallback -- searching water nobody ever
looked at, and quietly replacing a mask that worked.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from angels.adapters.maritime import water
from angels.adapters.maritime.coastline import LandGrid


class FakeLoc:
    """Pixels to lon/lat on a plain grid: 10 m pixels near 37 N."""

    def __init__(self, lon0=-75.0, lat0=37.0, pixel_deg=9e-5):
        self.lon0, self.lat0, self.d = lon0, lat0, pixel_deg

    def lonlat(self, col, row):
        return self.lon0 + col * self.d, self.lat0 - row * self.d


def scene_mask(water_left=True, h=640, w=640, decimation=16):
    """A mask that is water on one side and land on the other."""
    cells = np.zeros((h // decimation, w // decimation), dtype=bool)
    half = cells.shape[1] // 2
    if water_left:
        cells[:, :half] = True
    else:
        cells[:, half:] = True
    return water.WaterMask(water=cells, decimation=decimation, shape=(h, w),
                           threshold=1.0, contrast=3.0,
                           water_fraction=0.5, bimodal=True)


def test_votes_from_one_scene_reproduce_its_split() -> None:
    grid = LandGrid()
    loc = FakeLoc()
    grid.add_scene(loc, scene_mask(), name="A", stride=1)
    assert grid.is_water(*loc.lonlat(80, 320)) is True      # left half
    assert grid.is_water(*loc.lonlat(560, 320)) is False    # right half


def test_water_never_sampled_is_unknown_not_water() -> None:
    """THE ERROR THIS MUST NOT MAKE. A cell no pass has looked at must not
    be searched on the strength of never having been looked at."""
    grid = LandGrid()
    grid.add_scene(FakeLoc(), scene_mask(), name="A", stride=1)
    assert grid.is_water(-60.0, 20.0) is None


def test_a_mask_built_from_the_grid_keeps_the_unknown_out() -> None:
    grid = LandGrid()
    loc = FakeLoc()
    grid.add_scene(loc, scene_mask(), name="A", stride=1)
    # A scene twice as wide: the right half was never sampled.
    m = grid.mask_for(loc, 1280, 640, decimation=16, stride=1, buffer_m=0)
    assert m.water[:, 60:].sum() == 0


def test_passes_vote_and_the_majority_wins() -> None:
    grid = LandGrid()
    loc = FakeLoc()
    for _ in range(3):
        grid.add_scene(loc, scene_mask(water_left=True), stride=1)
    grid.add_scene(loc, scene_mask(water_left=False), stride=1)
    assert grid.is_water(*loc.lonlat(80, 320)) is True
    assert len(grid.scenes) == 0        # unnamed scenes are not listed


def test_the_coastal_buffer_is_applied_again() -> None:
    """A fallback scene has to be masked exactly as a normal one: land
    dilated seaward by half the CFAR background window, or the detector
    goes quiet near shore for a reason nobody would look for."""
    grid = LandGrid()
    loc = FakeLoc()
    grid.add_scene(loc, scene_mask(), name="A", stride=1)
    raw = grid.mask_for(loc, 640, 640, decimation=16, stride=1, buffer_m=0)
    buffered = grid.mask_for(loc, 640, 640, decimation=16, stride=1)
    assert buffered.water.sum() < raw.water.sum()
    assert buffered.water[:, 0].all(), "far from the coast is untouched"


def test_the_default_buffer_matches_the_detector() -> None:
    from angels.adapters.maritime import cfar
    grid = LandGrid()
    loc = FakeLoc()
    grid.add_scene(loc, scene_mask(), name="A", stride=1)
    a = grid.mask_for(loc, 640, 640, decimation=16, stride=1)
    b = grid.mask_for(loc, 640, 640, decimation=16, stride=1,
                      buffer_m=cfar.BACKGROUND / 2 * 10.0)
    assert (a.water == b.water).all()


def test_it_round_trips_and_a_missing_file_is_none() -> None:
    grid = LandGrid()
    grid.add_scene(FakeLoc(), scene_mask(), name="A", stride=2)
    back = LandGrid.from_json(json.loads(json.dumps(grid.to_json())))
    assert back is not None
    assert back.scenes == ["A"]
    assert len(back) == len(grid)
    assert LandGrid.from_json({}) is None
    assert LandGrid.from_json(None) is None


def test_the_cell_is_finer_than_the_blind_zone_it_will_be_buffered_by() -> None:
    from angels.adapters.maritime import cfar
    from angels.adapters.maritime.coastline import CELL_DEG
    assert CELL_DEG * 111_195 < cfar.BACKGROUND / 2 * 10.0


def test_a_scene_the_grid_cannot_cover_is_mostly_unsearchable() -> None:
    """Better an honest hole than invented water."""
    grid = LandGrid()
    grid.add_scene(FakeLoc(), scene_mask(), name="A", stride=1)
    elsewhere = FakeLoc(lon0=-70.0, lat0=33.0)
    m = grid.mask_for(elsewhere, 640, 640, decimation=16, stride=1)
    assert m.water_fraction == 0.0


def test_a_fallback_mask_does_not_claim_a_contrast_it_never_measured() -> None:
    """The pooled shoreline never looked at this scene's histogram. Printing
    "contrast nanx, 0 bright offshore target(s) kept searchable" told the
    reader the release step had run and found nothing; it never ran."""
    grid = LandGrid()
    loc = FakeLoc()
    grid.add_scene(loc, scene_mask(), name="A", stride=1)
    m = grid.mask_for(loc, 640, 640, decimation=16, stride=1, buffer_m=0)

    s = str(m)
    assert "pooled shoreline" in s
    assert "nan" not in s.lower()
    assert "bright offshore target" not in s
    assert m.source != "own histogram"
    # A real mask keeps its own sentence.
    assert "contrast" in str(scene_mask())
