"""Where AIS can actually be heard. The last denominator.

THE HOLE THIS FILLS

A detection with no AIS report means one of two things, and until now the
pipeline could not tell them apart:

    the vessel did not report          a dark vessel. The finding.
    nobody was listening out there     an artefact of the receiver network.

MarineCadastre's bulk AIS is the Coast Guard's NATIONWIDE AIS, a network of
SHORE-BASED receivers. VHF at those antenna heights reaches roughly 40-60 nm
before the horizon takes it, and no further. AOI_SEA runs to -71.0, about
300 km off the Delmarva coast -- well beyond that edge. So the outer third of
the study box may contain no AIS at all, and every vessel there would look
dark whether it was broadcasting or not.

That is the single error this project is built to avoid: reporting an
absence of evidence as evidence of absence. So reception is MEASURED, from
the AIS itself, before any candidate is called dark.

HOW IT IS MEASURED

The data measures its own coverage. Vessels report about once a minute in
the bulk files (measured: median gap 72 s in June, 161 s in September), so
in water a receiver covers, consecutive reports from one vessel are about a
minute apart. Where coverage fades, the same vessels still transit, but
their reports arrive in bursts with long silences between.

Per cell, then:

    n_vessels     distinct MMSI heard here at all
    median_gap_s  median time between consecutive reports of one vessel
    p_long        share of gaps longer than LONG_GAP_S

and the classes:

    heard         enough vessels, gaps at the reporting cadence. A vessel
                  here would have been heard, so silence is a finding.
    intermittent  vessels are heard, but with long silences. A vessel here
                  might not have been heard; a candidate is weak evidence.
    thin          one or two vessels ever. Not enough to say either way.
    unheard       no AIS at all, ever, in twelve passes. A candidate here
                  says nothing about the vessel's behaviour.

WHAT THIS CANNOT DO

It cannot separate "no reception" from "no traffic". A cell with no reports
might be out of range or might simply be empty water that nobody crosses.
Both are `unheard`, and both disqualify a candidate for the same reason:
there is no evidence that a broadcast there would have been recorded. Saying
so is the point. Guessing which one it is would be the error.

Nor does it measure the SAR's coverage -- that is the searched grid -- or
whether a particular vessel's transponder was working. It answers one
question: if something had broadcast here, would this dataset show it?
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

# About 5.5 km at this latitude. Small enough to see the offshore edge,
# large enough that an ordinary day puts several vessels in a coastal cell.
CELL_DEG = 0.05

# A gap longer than this is a silence, not a cadence. The bulk files are
# thinned to about one report per vessel per minute, so three minutes is
# already three missed slots.
LONG_GAP_S = 180.0

# Below this, a median is one or two vessels' habits, not the cell's.
MIN_VESSELS = 3

# Longer than this is not a cadence at all: it is the join between two
# separate stretches of listening -- two acquisition windows, or two passes
# twelve days apart. Counting it as a silence would make every cell that a
# vessel visits on two different dates look like the edge of coverage.
MAX_GAP_S = 3600.0

# The class vocabulary and the empty-cell rule are SHARED with the aviation
# grid, deliberately: a cross-domain claim reads "heard" as the same word in
# both domains, and two private copies of it would drift. See core.coverage.
from angels.core.coverage import (  # noqa: E402
    HEARD, INTERMITTENT, THIN, UNHEARD, cell_area_km2, cell_index as _index,
    neighbour_class)


@dataclass
class Cell:
    n_reports: int = 0
    vessels: set[str] = field(default_factory=set)
    gaps: list[float] = field(default_factory=list)

    @property
    def n_vessels(self) -> int:
        return len(self.vessels)

    @property
    def median_gap_s(self) -> float:
        return statistics.median(self.gaps) if self.gaps else float("nan")

    @property
    def p_long(self) -> float:
        if not self.gaps:
            return float("nan")
        return sum(g > LONG_GAP_S for g in self.gaps) / len(self.gaps)

    def reception(self, *, min_vessels: int = MIN_VESSELS,
                  long_gap_s: float = LONG_GAP_S) -> str:
        if self.n_vessels == 0:
            return UNHEARD
        if self.n_vessels < min_vessels or not self.gaps:
            return THIN
        return HEARD if self.median_gap_s <= long_gap_s else INTERMITTENT

    def to_json(self) -> dict:
        med, pl = self.median_gap_s, self.p_long
        return {
            "n_reports": self.n_reports,
            "n_vessels": self.n_vessels,
            "median_gap_s": None if med != med else round(med, 1),
            "p_long": None if pl != pl else round(pl, 3),
            "reception": self.reception(),
        }


class ReceptionGrid:
    """Which water this AIS dataset can hear, cell by cell."""

    def __init__(self, cell_deg: float = CELL_DEG) -> None:
        self.cell_deg = cell_deg
        self.cells: dict[tuple[int, int], Cell] = {}

    # -- building ----------------------------------------------------------

    def key(self, lon: float, lat: float) -> tuple[int, int]:
        return (_index(lon, self.cell_deg), _index(lat, self.cell_deg))

    def add_report(self, mmsi: str, lon: float, lat: float,
                   gap_s: float | None) -> None:
        """One AIS report, with the seconds since that vessel's previous one.

        gap_s is None for a vessel's first report in the window: there is no
        interval to measure yet, and inventing one would either flatter the
        cell (treating it as prompt) or slander it (treating it as silent).
        """
        c = self.cells.setdefault(self.key(lon, lat), Cell())
        c.n_reports += 1
        c.vessels.add(str(mmsi))
        if gap_s is not None:
            c.gaps.append(float(gap_s))

    def feed_sorted(self, rows, *, max_gap_s: float = MAX_GAP_S) -> int:
        """Add reports already ordered by (mmsi, time). Returns the count.

        Streaming, so a year of national AIS never has to be in memory at
        once: the only state is the previous row. A change of vessel, or a
        gap wider than max_gap_s, starts a new stretch with no interval --
        see MAX_GAP_S.
        """
        n = 0
        prev_mmsi, prev_t = None, None
        for mmsi, t, lon, lat in rows:
            mmsi = str(mmsi)
            gap = None
            if mmsi == prev_mmsi and prev_t is not None:
                delta = (t - prev_t).total_seconds()
                if 0 <= delta <= max_gap_s:
                    gap = delta
            self.add_report(mmsi, lon, lat, gap)
            prev_mmsi, prev_t = mmsi, t
            n += 1
        return n

    @classmethod
    def from_reports(cls, reports, cell_deg: float = CELL_DEG
                     ) -> "ReceptionGrid":
        """Build from (mmsi, t, lon, lat) tuples, in any order.

        Gaps are computed per vessel over the whole set, so a vessel crossing
        several cells contributes each interval to the cell it arrived in.
        """
        grid = cls(cell_deg)
        by_vessel: dict[str, list] = {}
        for mmsi, t, lon, lat in reports:
            by_vessel.setdefault(str(mmsi), []).append((t, lon, lat))
        ordered = []
        for mmsi, rows in sorted(by_vessel.items()):
            rows.sort(key=lambda r: r[0])
            ordered += [(mmsi, t, lon, lat) for t, lon, lat in rows]
        grid.feed_sorted(ordered)
        return grid

    # -- asking ------------------------------------------------------------

    def cell_at(self, lon: float, lat: float) -> Cell:
        return self.cells.get(self.key(lon, lat), Cell())

    def reception(self, lon: float, lat: float, *, neighbours: bool = True,
                  **kw) -> str:
        """The reception class at a point.

        With neighbours=True an empty cell surrounded by heard water is read
        as heard: a 5 km cell can be empty on twelve overflights simply
        because no ship happened to cross that particular square, and calling
        that "unheard" would throw away candidates in the middle of a
        well-covered shipping lane. A cell's own evidence always wins when it
        has any.
        """
        own = self.cell_at(lon, lat)
        if own.n_vessels:
            return own.reception(**kw)
        if not neighbours:
            return UNHEARD
        col, row = self.key(lon, lat)
        return neighbour_class(
            [self.cells[(col + dc, row + dr)].reception(**kw)
             for dc in (-1, 0, 1) for dr in (-1, 0, 1)
             if (col + dc, row + dr) in self.cells])

    def summary(self, **kw) -> dict[str, int]:
        out = {HEARD: 0, INTERMITTENT: 0, THIN: 0}
        for c in self.cells.values():
            out[c.reception(**kw)] = out.get(c.reception(**kw), 0) + 1
        return out

    def area_km2(self, klass: str, **kw) -> float:
        """Rough area of the cells in one class."""
        total = 0.0
        for (col, row), c in self.cells.items():
            if c.reception(**kw) != klass:
                continue
            total += cell_area_km2(row, self.cell_deg)
        return total

    # -- storage -----------------------------------------------------------

    def to_json(self) -> dict:
        return {
            "cell_deg": self.cell_deg,
            "long_gap_s": LONG_GAP_S,
            "min_vessels": MIN_VESSELS,
            "cells": {f"{col},{row}": c.to_json()
                      for (col, row), c in sorted(self.cells.items())},
        }

    @classmethod
    def from_json(cls, doc) -> "ReceptionGrid | None":
        """Rebuild from to_json(). None when the document is unusable --
        a missing coverage grid must not read as coverage everywhere."""
        if not isinstance(doc, dict) or not doc.get("cells"):
            return None
        grid = cls(float(doc.get("cell_deg", CELL_DEG)))
        for k, v in doc["cells"].items():
            col, _, row = k.partition(",")
            c = Cell()
            c.n_reports = int(v.get("n_reports", 0))
            # Vessel identities are not stored -- only how many there were.
            c.vessels = {f"{k}#{i}" for i in range(int(v.get("n_vessels", 0)))}
            med = v.get("median_gap_s")
            if med is not None:
                # One synthetic gap at the stored median reproduces every
                # decision this class makes without storing millions of them.
                c.gaps = [float(med)]
            grid.cells[(int(col), int(row))] = c
        return grid
