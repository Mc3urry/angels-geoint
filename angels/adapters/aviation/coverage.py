"""Where ADS-B can actually be heard -- and at what HEIGHT.

THE HOLE THIS FILLS, AND WHY IT ONLY OPENED AT CONUS SCALE

Over a two-degree box around Washington, receiver coverage is near enough
uniform to ignore. Over the continental United States it is emphatically
not: OpenSky is a volunteer network, dense along the Northeast corridor and
thin over the Great Basin. A national map of aircraft that stopped reporting
is, without this module, a map of where nobody is listening.

That is the same error the maritime side spent a month avoiding, and it has
the same shape: an absence of evidence reported as evidence of absence.

WHY THIS IS NOT THE AIS MODULE WITH DIFFERENT NAMES

The maritime grid measures the INTERVAL between one vessel's reports, which
works because bulk AIS is a continuous record. The aviation archive is
POLLED SNAPSHOTS -- every 30 s for the Chesapeake box, every 10 min for
CONUS -- so per-aircraft cadence below the poll interval is not observable
at all, and any "gap" measured from it would be an artefact of how often we
asked.

Two candidate measures were tried on ten days of national archive before
this design was settled. The first one failed, and that failure is worth
recording rather than hiding:

    CONTACT AGE (fetched_at - last_contact) does NOT measure coverage.
    OpenSky reports, per aircraft, when it last heard anything from it. The
    hope was that this would be fresh where receivers are dense and stale
    where they are thin. Measured over twelve five-degree longitude bands
    from -125 to -70, the median contact age is 1 second in EVERY band and
    the share over a minute sits between 5.0% and 7.9% everywhere. It is a
    liveness signal -- how current the fix you are holding is -- and it is
    uniform because OpenSky simply drops a state it has not heard for about
    five minutes. It says nothing about what the network is MISSING, which
    is the entire question. It is still measured here, and reported, but as
    freshness for the viewer, never as coverage.

    ALTITUDE is what carries the signal, because ADS-B is line-of-sight.
    A ground station hears an airliner at 11 km from perhaps 400 km away and
    a light aircraft at 500 m from perhaps 40 km. So coverage is a function
    of CELL AND HEIGHT, and collapsing it to one class per cell throws away
    the dimension that does the work.

The same ten days, two 2-degree cells, distinct aircraft by band:

                       New Jersey    Great Basin
        on ground           1,961              0
        0-1 km              2,285              0
        1-3 km              2,017              0
        3-6 km              1,214              0
        6-9 km                685              4
        9-12 km               568            194
        12+ km                 76             51

Over New Jersey the network hears aircraft all the way to the tarmac. Over
Nevada it has not heard ANYTHING below six kilometres in ten days, while
tracking two hundred airliners overhead. An aircraft going quiet at 3 km
over Nevada is therefore not a finding of any kind; the same event at 10 km
is, because two hundred of its neighbours were heard there.

WHAT THIS CANNOT DO -- the same limit as the maritime grid

It cannot separate "no receiver" from "no traffic". Nothing is heard below
6 km over the Great Basin, and light aircraft are also genuinely rare there;
this module reports both as `unheard` and declines to guess. Both disqualify
a candidate for the same reason, which is why one word is right for them.

Nor does it use terrain. An altitude here is barometric, above mean sea
level, and the Great Basin floor is itself at 1.5 km -- so part of that
column is rock, not missing coverage. This does not affect the measurement,
because a claim is only ever compared against OTHER AIRCRAFT IN THE SAME
CELL AND BAND, which sit above the same ground. It affects the EXPLANATION,
and belongs in the write-up as one.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from angels.core.coverage import (
    HEARD,
    INTERMITTENT,
    THIN,
    UNHEARD,
    cell_area_km2,
    cell_index,
    neighbour_class,
)

# One degree, about 85 km across at 40 N. Chosen against the CONUS poll
# interval rather than for cartographic tidiness: at 10 minutes an airliner
# moves 130 km between snapshots, so a cell much smaller than this would be
# claiming a spatial precision the sampling cannot support.
CELL_DEG = 1.0

# Line-of-sight range grows roughly with the square root of height, so the
# bands are wide low down -- where the range changes fastest and the traffic
# is -- and coarse above the flight levels, where everything is heard.
BAND_EDGES_M = (0.0, 1000.0, 3000.0, 6000.0, 9000.0, 12000.0, float("inf"))
BAND_NAMES = ("0-1 km", "1-3 km", "3-6 km", "6-9 km", "9-12 km", "12+ km")

# Kept apart from the 0-1 km band rather than folded into it. An aircraft
# reporting on_ground is being heard from the surface itself, which is the
# strongest possible evidence that coverage reaches the bottom of that cell,
# and it should not be diluted by traffic a few hundred metres up.
GROUND = "on ground"
BANDS = (GROUND,) + BAND_NAMES

# Below this, the class rests on one or two airframes' habits rather than on
# the cell. Matches MIN_VESSELS on the maritime side.
MIN_AIRCRAFT = 3

# "Routinely present" -- the share of polls in which this cell and band held
# at least one aircraft. Below it, the band is real but sparse: we hear what
# passes, but little passes, so a silence there is weak evidence.
#
# 5% is deliberately low. The question is not whether the band is busy, it is
# whether anything is ever heard there, and a corridor crossed by one
# aircraft every twenty polls is still a corridor the network can hear.
MIN_PRESENCE = 0.05

# Older than this and the fix being held is not current. Used only to report
# freshness -- see the module docstring on why contact age is not coverage.
STALE_S = 60.0


def band_of(altitude_m: float | None, on_ground: bool = False) -> str | None:
    """Which altitude band a state vector belongs to.

    None when there is no altitude and the aircraft is not on the ground:
    about 10% of rows, and they must not be silently dropped into the lowest
    band. An unknown height is unknown, and a coverage floor assembled out of
    guesses would be exactly the wrong artefact to build.
    """
    if on_ground:
        return GROUND
    if altitude_m is None or altitude_m != altitude_m:
        return None
    for name, hi in zip(BAND_NAMES, BAND_EDGES_M[1:]):
        if altitude_m < hi:
            return name
    return BAND_NAMES[-1]


@dataclass
class Band:
    """One cell at one height."""

    n_states: int = 0
    aircraft: set[str] = field(default_factory=set)
    n_polls_present: int = 0
    contact_ages: list[float] = field(default_factory=list)

    @property
    def n_aircraft(self) -> int:
        return len(self.aircraft)

    @property
    def median_contact_age_s(self) -> float:
        return (statistics.median(self.contact_ages)
                if self.contact_ages else float("nan"))

    @property
    def p_stale(self) -> float:
        if not self.contact_ages:
            return float("nan")
        return sum(a > STALE_S for a in self.contact_ages) / len(self.contact_ages)

    def presence(self, n_polls: int) -> float:
        """Share of polls in which something was heard here."""
        return self.n_polls_present / n_polls if n_polls else float("nan")

    def reception(self, n_polls: int, *, min_aircraft: int = MIN_AIRCRAFT,
                  min_presence: float = MIN_PRESENCE) -> str:
        if self.n_aircraft == 0:
            return UNHEARD
        if self.n_aircraft < min_aircraft:
            return THIN
        p = self.presence(n_polls)
        if p != p:                       # no poll count: cannot say routinely
            return INTERMITTENT
        return HEARD if p >= min_presence else INTERMITTENT

    def to_json(self, n_polls: int) -> dict:
        age, stale, p = self.median_contact_age_s, self.p_stale, self.presence(n_polls)
        return {
            "n_states": self.n_states,
            "n_aircraft": self.n_aircraft,
            "presence": None if p != p else round(p, 4),
            # Freshness, NOT coverage. See the module docstring.
            "median_contact_age_s": None if age != age else round(age, 1),
            "p_stale": None if stale != stale else round(stale, 3),
            "reception": self.reception(n_polls),
        }


class AirReceptionGrid:
    """Which sky this ADS-B feed can hear, cell by cell and band by band."""

    def __init__(self, cell_deg: float = CELL_DEG) -> None:
        self.cell_deg = cell_deg
        self.cells: dict[tuple[int, int, str], Band] = {}
        self.n_polls = 0
        self.n_no_altitude = 0

    # -- building ----------------------------------------------------------

    def key(self, lon: float, lat: float, band: str) -> tuple[int, int, str]:
        return (cell_index(lon, self.cell_deg),
                cell_index(lat, self.cell_deg), band)

    def add_poll(self, states) -> int:
        """One snapshot: every aircraft the feed held at one instant.

        `states` yields (icao24, lon, lat, altitude_m, on_ground,
        contact_age_s). contact_age_s may be None.

        Presence is counted ONCE PER POLL per cell and band, not once per
        aircraft, because the question the fraction answers is "was anything
        heard here at that moment", and a cell under a holding stack would
        otherwise look twenty times better covered than one under a single
        overflight.
        """
        self.n_polls += 1
        seen: set[tuple[int, int, str]] = set()
        n = 0
        for icao24, lon, lat, alt_m, on_ground, age_s in states:
            band = band_of(alt_m, on_ground)
            if band is None:
                self.n_no_altitude += 1
                continue
            k = self.key(lon, lat, band)
            c = self.cells.setdefault(k, Band())
            c.n_states += 1
            c.aircraft.add(str(icao24))
            if age_s is not None and age_s == age_s:
                c.contact_ages.append(float(age_s))
            seen.add(k)
            n += 1
        for k in seen:
            self.cells[k].n_polls_present += 1
        return n

    # -- asking ------------------------------------------------------------

    def band_at(self, lon: float, lat: float, band: str) -> Band:
        return self.cells.get(self.key(lon, lat, band), Band())

    def reception(self, lon: float, lat: float, altitude_m: float | None, *,
                  on_ground: bool = False, neighbours: bool = True,
                  **kw) -> str:
        """Would an aircraft here, at this height, have been heard?

        The one question this module exists to answer, and the one that has
        to be asked before any silence in the aviation archive is called a
        finding.
        """
        band = band_of(altitude_m, on_ground)
        if band is None:
            # No height, no answer. The alternative -- assuming a band --
            # would let an unknown altitude inherit the reception of traffic
            # it may be nowhere near.
            return UNHEARD
        own = self.band_at(lon, lat, band)
        if own.n_aircraft:
            return own.reception(self.n_polls, **kw)
        if not neighbours:
            return UNHEARD
        col, row, _ = self.key(lon, lat, band)
        # Neighbours in SPACE ONLY, never across bands. Borrowing a class
        # from the band above would be the whole error in miniature: the sky
        # over Nevada is well heard at eleven kilometres and not heard at all
        # at three, and a cell that mixed them would launder the second into
        # the first.
        return neighbour_class(
            [self.cells[(col + dc, row + dr, band)].reception(self.n_polls, **kw)
             for dc in (-1, 0, 1) for dr in (-1, 0, 1)
             if (col + dc, row + dr, band) in self.cells])

    def profile(self, lon: float, lat: float, **kw) -> dict[str, str]:
        """The whole vertical column at one place, lowest band first.

        This is what a viewer should show when someone clicks: not "covered"
        but the height at which coverage starts.
        """
        return {b: self.band_at(lon, lat, b).reception(self.n_polls, **kw)
                for b in BANDS}

    def floor(self, lon: float, lat: float, **kw) -> str | None:
        """The lowest band that is heard at this place, or None if none is.

        The single most useful number for a national map: below it, this
        dataset has no opinion about anything.
        """
        for b in BANDS:
            if self.band_at(lon, lat, b).reception(self.n_polls, **kw) == HEARD:
                return b
        return None

    def summary(self, **kw) -> dict[str, dict[str, int]]:
        """Counts of cells per class, per band."""
        out = {b: dict.fromkeys((HEARD, INTERMITTENT, THIN, UNHEARD), 0)
               for b in BANDS}
        for (_, _, band), c in self.cells.items():
            out[band][c.reception(self.n_polls, **kw)] += 1
        return out

    def area_km2(self, band: str, klass: str, **kw) -> float:
        return sum(cell_area_km2(row, self.cell_deg)
                   for (_, row, b), c in self.cells.items()
                   if b == band and c.reception(self.n_polls, **kw) == klass)

    def freshness(self) -> dict[str, float]:
        """Contact age over the whole grid -- the viewer's staleness line.

        Reported for the whole feed rather than per cell because it was
        measured to be uniform; see the module docstring. If a future archive
        makes it vary, that is a finding and this should become per-cell.
        """
        ages = [a for c in self.cells.values() for a in c.contact_ages]
        if not ages:
            return {"n": 0}
        ages.sort()
        return {
            "n": len(ages),
            "median_s": round(statistics.median(ages), 1),
            "p90_s": round(ages[int(0.90 * (len(ages) - 1))], 1),
            "p_stale": round(sum(a > STALE_S for a in ages) / len(ages), 4),
            "stale_s": STALE_S,
        }

    # -- storage -----------------------------------------------------------

    def to_json(self) -> dict:
        return {
            "cell_deg": self.cell_deg,
            "bands": list(BANDS),
            "band_edges_m": [e for e in BAND_EDGES_M if e != float("inf")],
            "n_polls": self.n_polls,
            "n_no_altitude": self.n_no_altitude,
            "min_aircraft": MIN_AIRCRAFT,
            "min_presence": MIN_PRESENCE,
            "freshness": self.freshness(),
            "cells": {f"{col},{row},{band}": c.to_json(self.n_polls)
                      for (col, row, band), c in sorted(self.cells.items())},
        }

    @classmethod
    def from_json(cls, doc: dict | None) -> AirReceptionGrid | None:
        if not doc:
            return None
        grid = cls(float(doc.get("cell_deg", CELL_DEG)))
        grid.n_polls = int(doc.get("n_polls", 0))
        grid.n_no_altitude = int(doc.get("n_no_altitude", 0))
        for k, v in (doc.get("cells") or {}).items():
            col, row, band = k.split(",", 2)
            c = Band()
            c.n_states = int(v.get("n_states", 0))
            # The aircraft themselves are not stored -- a year of ICAO
            # addresses is large and nothing downstream asks which they were.
            # The count is restored as anonymous members so n_aircraft, and
            # therefore every class this file assigns, survives the round
            # trip.
            c.aircraft = {f"{k}#{i}" for i in range(int(v.get("n_aircraft", 0)))}
            p = v.get("presence")
            c.n_polls_present = (0 if p is None
                                 else int(round(float(p) * grid.n_polls)))
            age = v.get("median_contact_age_s")
            if age is not None:
                c.contact_ages = [float(age)]
            grid.cells[(int(col), int(row), band)] = c
        return grid
