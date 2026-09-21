"""Where the radar actually looked, in ground coordinates.

THE DENOMINATOR, AND IT WAS WRONG A SECOND TIME.

The first version of the denominator scored every slice against every vessel
in AOI_SEA, so a slice covering offshore Virginia was charged with missing
vessels in Delaware Bay. scene_footprint() fixed that: the denominator became
the ground quadrilateral the raster covers, and the track count for one slice
fell from 1,227 to 314.

The detection rate stayed at 2%.

WHY THE FOOTPRINT IS STILL NOT THE ANSWER

The footprint is the area the raster covers. It is not the area the detector
searched. Between them sit two deliberate exclusions, both of which remove
exactly the water that vessels crowd into:

  the land mask       The Chesapeake, the James, the Elizabeth and every
                      terminal on them are inside the footprint. Whether they
                      survive the scene-wide Otsu split is a property of the
                      histogram, not of navigability.

  the coastal blind   A CA-CFAR cell needs an uncontaminated background ring.
                      BACKGROUND is 121 px, so the outer 605 m of every water
                      body is unsearchable by construction. Harbours are
                      narrower than that in places, and a ship at a pier is
                      always inside it.

Measured on slice 2 of the 2024-09-25 pass: of 306 AIS vessels inside the
footprint, 286 had no detection within 5 km, and they were not scattered --
they were a solid block at Norfolk International Terminals, Lambert's Point
and the York River anchorage, with a hard edge where the detection field
began. The radar never looked there. The denominator counted them anyway.

Charging a detector with missing a ship moored at a pier it masked out is the
same error as charging it with missing a ship 300 km outside its swath, one
order of magnitude smaller and far harder to see. An absence means nothing
unless the thing could have appeared.

WHAT THIS MODULE IS

A coarse lon/lat occupancy grid of searched water, built while the tiles are
being read and carried in the detection file, so that match_maritime.py can
ask "was this position searched?" months later without the 900 MB scene.

It stores a FRACTION per cell, not a flag. A cell straddling a shoreline is
partly searched, and the two ways of rounding that off push the detection rate
in opposite directions: count it and the detector is blamed for a vessel it
could not see; drop it and the rate is flattered. Neither is reportable on its
own, so the fraction survives to the point of use and match_maritime.py
reports the marginal cells as their own bucket rather than resolving them.

Resolution is deliberately coarse -- CELL_DEG of 0.01 is about 1.1 km, and the
coastal blind zone is 0.6 km, so a cell is the smallest unit at which the
question is even well posed. Finer would encode the mask's own noise.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

CELL_DEG = 0.01
"""Grid cell size in degrees. ~1.11 km in latitude, ~0.89 km in longitude at
37 N -- commensurate with the 605 m coastal blind zone, not finer than it."""

SAMPLE_STRIDE = 32
"""Sample every Nth pixel of a tile when accumulating coverage: 320 m at 10 m
pixels, comfortably inside one cell, so no searched cell goes unsampled. Full
resolution would mean ~4e8 forward geolocations per scene to answer a question
whose own uncertainty is a kilometre wide."""

LEVELS = 10
"""Fractions are quantised to tenths before encoding. The input is a sampled
estimate of a mask that is itself a threshold on a histogram; storing it to
more precision than this would dress up noise as measurement."""

SEARCHED = 0.5
"""A cell counts as searched when at least this fraction of it was. The value
is a convention, and everything below it is reported separately rather than
silently dropped, so the convention is visible in the output."""


def _index(value: float, cell_deg: float) -> int:
    """Which cell a coordinate falls in, as an absolute lattice index.

    The rounding is not cosmetic. -75.0 / 0.01 is -7500.000000000001 in
    binary floating point, and floor() of that is -7501: a cell boundary
    lands one cell to the left of where the same arithmetic put it a moment
    earlier. Build and query then disagree about the edge cells, and the
    disagreement shows up as a shoreline strip that reports zero coverage
    however much of it was searched. Every index in this module goes through
    here so that build and query cannot drift apart.
    """
    return math.floor(round(value / cell_deg, 9))


@dataclass(frozen=True)
class SearchedArea:
    """Searched-water fraction on a regular lon/lat lattice.

    Row r spans latitudes [lat0 + r*cell, lat0 + (r+1)*cell); column c spans
    longitudes [lon0 + c*cell, lon0 + (c+1)*cell). Cells that were never
    searched at all are absent rather than stored as zero -- a scene is mostly
    not-this-scene, and the sparse form is what makes this small enough to sit
    in every detection file.
    """

    cell_deg: float
    lon0: float
    lat0: float
    cells: dict[tuple[int, int], int]      # (col, row) -> level, 1..LEVELS

    # ---------------------------------------------------------------- build

    @classmethod
    def from_samples(cls, samples, cell_deg: float = CELL_DEG) -> SearchedArea:
        """Build from an iterable of (lon, lat, searched) sample points.

        `searched` is truthy when that sample fell on water the detector
        actually examined. Samples that fell on land, on the zero-filled
        corners outside the illuminated swath, or inside the coastal blind
        zone are passed in as False rather than omitted -- the fraction is
        what distinguishes "not searched" from "not sampled", and dropping
        the negatives would make every touched cell look fully searched.
        """
        tally: dict[tuple[int, int], list[int]] = {}
        lon0 = lat0 = None
        for lon, lat, ok in samples:
            col = _index(lon, cell_deg)
            row = _index(lat, cell_deg)
            lon0 = col if lon0 is None else min(lon0, col)
            lat0 = row if lat0 is None else min(lat0, row)
            slot = tally.setdefault((col, row), [0, 0])
            slot[0] += 1
            if ok:
                slot[1] += 1
        if lon0 is None:
            return cls(cell_deg, 0.0, 0.0, {})

        cells: dict[tuple[int, int], int] = {}
        for (col, row), (n, hit) in tally.items():
            if not hit:
                continue
            level = max(1, min(LEVELS, round(LEVELS * hit / n)))
            cells[(col - lon0, row - lat0)] = level
        return cls(cell_deg, lon0 * cell_deg, lat0 * cell_deg, cells)

    # ---------------------------------------------------------------- query

    def fraction(self, lon: float, lat: float) -> float:
        """How much of this position's cell was searched, 0.0 to 1.0."""
        col = _index(lon, self.cell_deg) - _index(self.lon0, self.cell_deg)
        row = _index(lat, self.cell_deg) - _index(self.lat0, self.cell_deg)
        return self.cells.get((col, row), 0) / LEVELS

    def contains(self, lon: float, lat: float, threshold: float = SEARCHED) -> bool:
        """Was this position searched, by the stated convention?"""
        return self.fraction(lon, lat) >= threshold

    @property
    def area_km2(self) -> float:
        """Searched area implied by the grid.

        Kept so it can be compared against the pixel count detect_ships.py
        arrives at independently. The two are computed from the same mask by
        different routes -- one counts pixels, the other geolocates a sparse
        lattice of them -- so agreement is evidence that the geolocation and
        the sampling are both sound, and a large disagreement is a bug in one
        of them rather than a fact about the sea.
        """
        total = 0.0
        lat_km = self.cell_deg * 111.195
        for (_, row), level in self.cells.items():
            lat = self.lat0 + (row + 0.5) * self.cell_deg
            lon_km = self.cell_deg * 111.195 * math.cos(math.radians(lat))
            total += lat_km * lon_km * level / LEVELS
        return total

    def __str__(self) -> str:
        full = sum(1 for v in self.cells.values() if v / LEVELS >= SEARCHED)
        part = len(self.cells) - full
        return (f"{self.area_km2:,.0f} km2 searched "
                f"({full:,} cells, {part:,} partial) "
                f"at {self.cell_deg:g} deg")

    # --------------------------------------------------------------- codec

    def to_json(self) -> dict:
        """Run-length by equal level along each row.

        A swath is long and smooth, so a row of open ocean is one run. The
        whole grid for a 250 km slice comes to a few kilobytes, which is what
        makes it affordable to carry in every detection file rather than
        keeping it in a sidecar that can go missing while the file that
        depends on it survives.
        """
        rows: dict[str, list[list[int]]] = {}
        by_row: dict[int, dict[int, int]] = {}
        for (col, row), level in self.cells.items():
            by_row.setdefault(row, {})[col] = level
        for row in sorted(by_row):
            runs: list[list[int]] = []
            cols = by_row[row]
            for col in sorted(cols):
                level = cols[col]
                if runs and runs[-1][0] + runs[-1][1] == col \
                        and runs[-1][2] == level:
                    runs[-1][1] += 1
                else:
                    runs.append([col, 1, level])
            rows[str(row)] = runs
        return {
            "cell_deg": self.cell_deg,
            "lon0": round(self.lon0, 6),
            "lat0": round(self.lat0, 6),
            "levels": LEVELS,
            "rows": rows,
        }

    @classmethod
    def from_json(cls, blob) -> SearchedArea | None:
        """Decode, or None if the field is missing or unusable.

        None means "this file cannot answer the question", which is a
        different statement from "nothing was searched" and has to stay
        different all the way to the caller. A detection file written before
        this existed must make match_maritime.py say so, not quietly report a
        detection rate computed against an empty denominator.
        """
        if not isinstance(blob, dict) or "rows" not in blob:
            return None
        try:
            cell = float(blob["cell_deg"])
            lon0 = float(blob["lon0"])
            lat0 = float(blob["lat0"])
        except (KeyError, TypeError, ValueError):
            return None
        if not cell > 0:
            return None
        cells: dict[tuple[int, int], int] = {}
        for row_s, runs in blob["rows"].items():
            row = int(row_s)
            for start, length, level in runs:
                for col in range(int(start), int(start) + int(length)):
                    cells[(col, row)] = int(level)
        return cls(cell, lon0, lat0, cells)
