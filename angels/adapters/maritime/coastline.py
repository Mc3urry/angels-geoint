"""A land mask that does not depend on the weather. The fallback shoreline.

WHY THIS EXISTS

water.from_raster splits land from sea by the scene's own histogram, and
refuses when it cannot (one Otsu mode, contrast below 1.8). Refusing is
right -- assuming ocean is what searched inland Pennsylvania. But the
refusals are not random: they happen when wind brightens the sea until it
stops looking different from land. Measured over the 2024 passes, slice 1
failed on five of twelve dates, and those dates searched 40-45k km2 against
60-97k on the others.

Coverage that disappears in high wind is worse than coverage that is merely
smaller, because sea state also changes what is out there and how much
clutter the detector sees. A comparison across dates then has weather on
both sides of it.

THE WAY OUT IS THE ORBIT

Sentinel-1 retraces the same ground every twelve days. The coastline does
not move between passes. So a mask measured on a pass where the split WORKED
describes the same shoreline as the pass where it failed -- as long as it is
held in GROUND coordinates rather than pixels, because the two passes do not
share a pixel grid (their start times differ by a second or so, which is
hundreds of pixels).

So: sample every successful scene's mask through its own geolocation, vote
per 0.005 degree cell (about 500 m, finer than the 605 m coastal blind zone
that gets applied anyway), and keep the majority. A cell nobody sampled
stays unknown, and unknown is NOT water -- an unobserved cell must not be
searched on the strength of never having been looked at.

WHAT IT IS NOT

It is not a chart. It is this project's own mask, generalised across passes,
and it inherits every property of the Otsu split that built it -- including
the 605 m buffer, applied again here so a fallback scene is masked exactly
as a normal one is. If it ever disagrees with a chart, the chart is right.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np

from . import cfar, water

# About 500 m. Finer than the coastal blind zone that is applied on top, and
# coarse enough that a dozen passes agree on nearly every cell.
CELL_DEG = 0.005

# Sample every Nth cell of a scene's decimated mask. At decimation 16 that
# is 640 m per sample, which fills a 500 m grid densely enough while keeping
# the geolocation calls in the tens of thousands rather than the millions.
SAMPLE_STRIDE = 4


def _index(v: float, cell: float) -> int:
    return math.floor(round(v / cell, 9))


@dataclass
class LandGrid:
    """Land/water votes per ground cell, poolable across passes."""

    cell_deg: float = CELL_DEG
    land: dict[tuple[int, int], int] = field(default_factory=dict)
    water: dict[tuple[int, int], int] = field(default_factory=dict)
    scenes: list[str] = field(default_factory=list)

    # -- building ----------------------------------------------------------

    def vote(self, lon: float, lat: float, is_water: bool) -> None:
        key = (_index(lon, self.cell_deg), _index(lat, self.cell_deg))
        book = self.water if is_water else self.land
        book[key] = book.get(key, 0) + 1

    def add_scene(self, loc, mask: "water.WaterMask", name: str = "",
                  stride: int = SAMPLE_STRIDE) -> int:
        """Vote every stride-th cell of one scene's mask. Returns the votes.

        `mask.water` is True where the detector may look; that is water minus
        the coastal buffer. The buffer is re-applied when a mask is built
        from this grid, so what is stored here is the raw side of the split:
        anything the scene called water.
        """
        h, w = mask.water.shape
        n = 0
        for r in range(0, h, stride):
            for c in range(0, w, stride):
                # Centre of that decimated cell, in full-resolution pixels.
                col = (c + 0.5) * mask.decimation
                row = (r + 0.5) * mask.decimation
                if row >= mask.shape[0] or col >= mask.shape[1]:
                    continue
                lon, lat = loc.lonlat(col, row)
                self.vote(lon, lat, bool(mask.water[r, c]))
                n += 1
        if name:
            self.scenes.append(name)
        return n

    # -- asking ------------------------------------------------------------

    def is_water(self, lon: float, lat: float) -> bool | None:
        """True water, False land, None never sampled."""
        key = (_index(lon, self.cell_deg), _index(lat, self.cell_deg))
        wv, lv = self.water.get(key, 0), self.land.get(key, 0)
        if wv == 0 and lv == 0:
            return None
        return wv > lv

    def __len__(self) -> int:
        return len(set(self.land) | set(self.water))

    def __str__(self) -> str:
        return (f"{len(self):,} cells of {self.cell_deg:g} deg from "
                f"{len(self.scenes)} scene(s)")

    # -- using it as a mask ------------------------------------------------

    def mask_for(self, loc, width: int, height: int, *,
                 decimation: int = 16, stride: int = SAMPLE_STRIDE,
                 buffer_m: float | None = None,
                 pixel_m: float = 10.0) -> "water.WaterMask":
        """A WaterMask for a scene this grid did not come from.

        Sampled at `stride` decimated cells and filled in blocks, then
        buffered exactly as water.from_raster buffers: land dilated seaward
        by half the CFAR background window, because a coastline inside a
        background ring suppresses real detections for hundreds of metres.

        A cell this grid never sampled is NOT water. The scene may cover
        ground no other pass covered, and searching it on the strength of
        never having been looked at is the same error as calling an
        unsearched sea empty.
        """
        hh, ww = max(1, height // decimation), max(1, width // decimation)
        out = np.zeros((hh, ww), dtype=bool)
        for r in range(0, hh, stride):
            for c in range(0, ww, stride):
                col = (c + 0.5) * decimation
                row = (r + 0.5) * decimation
                lon, lat = loc.lonlat(min(col, width - 1), min(row, height - 1))
                verdict = self.is_water(lon, lat)
                out[r:r + stride, c:c + stride] = bool(verdict)

        if buffer_m is None:
            buffer_m = cfar.BACKGROUND / 2 * pixel_m
        cells = int(np.ceil(buffer_m / (decimation * pixel_m)))
        if cells > 0:
            near, _ = cfar._window_sum(
                cfar._integral((~out).astype(np.float64)), cells)
            out = out & (near == 0)

        frac = float(out.sum() / out.size) if out.size else 0.0
        return water.WaterMask(water=out, decimation=decimation,
                               shape=(height, width), threshold=float("nan"),
                               contrast=float("nan"), water_fraction=frac,
                               bimodal=True, texture=0.0, released=0,
                               source="pooled shoreline")

    # -- storage -----------------------------------------------------------

    def to_json(self) -> dict:
        return {
            "cell_deg": self.cell_deg,
            "scenes": self.scenes,
            "cells": {f"{c},{r}": [self.land.get((c, r), 0),
                                   self.water.get((c, r), 0)]
                      for c, r in sorted(set(self.land) | set(self.water))},
        }

    @classmethod
    def from_json(cls, doc) -> "LandGrid | None":
        """None when unusable. A missing shoreline must not read as all
        water, which is the failure this whole module exists to avoid."""
        if not isinstance(doc, dict) or not doc.get("cells"):
            return None
        grid = cls(cell_deg=float(doc.get("cell_deg", CELL_DEG)),
                   scenes=list(doc.get("scenes", [])))
        for key, (lv, wv) in doc["cells"].items():
            c, _, r = key.partition(",")
            k = (int(c), int(r))
            if lv:
                grid.land[k] = int(lv)
            if wv:
                grid.water[k] = int(wv)
        return grid
