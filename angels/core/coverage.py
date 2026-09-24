"""Reception modelling: what the two domains share.

THE PROBLEM THIS NAMES

A platform that stops reporting may have switched off -- or may have left
receiver coverage. Identical in the data, opposite in meaning. And coverage
is itself geographically patterned, so it directly confounds the boundary
hypothesis the project rests on.

Each adapter measures its own coverage, because the physics and the evidence
differ:

    maritime    AIS is a continuous broadcast from shore-heard VHF. The
                measurable is the INTERVAL between one vessel's reports:
                where a receiver covers, they arrive about a minute apart;
                where it fades, they arrive in bursts.
                -> adapters/maritime/coverage.py

    aviation    the archive is polled snapshots of a national feed, so
                per-aircraft cadence is not observable below the poll
                interval. What is observable is WHERE AND AT WHAT ALTITUDE
                aircraft are heard at all, and ADS-B reception is
                line-of-sight: range grows with altitude, so coverage is a
                function of cell AND height, not of cell alone.
                -> adapters/aviation/coverage.py

WHAT IS ACTUALLY SHARED

Not the measurement. Only the vocabulary it is reported in, and the rule for
reading a cell that has no evidence of its own. Both of those must be
identical across domains or the cross-domain comparison is comparing two
different words spelled the same way, so they live here and both adapters
call them.

The original version of this module promised density surfaces built from
receiver positions and a reception probability from distance and altitude.
Neither was ever written, and neither should be: OpenSky does not publish
receiver positions in a form that would support it, and the Coast Guard does
not publish theirs at all. Measuring coverage from the observations
themselves needs no receiver list and cannot be wrong about one.
"""

from __future__ import annotations

import math

# The four answers, in every domain. Their meanings are fixed here because a
# cross-domain claim reads them as the same scale.
#
#   heard         enough platforms, reported at the expected cadence or
#                 routinely present. Something here would have been
#                 recorded, so a SILENCE IS A FINDING.
#   intermittent  platforms are recorded, but sparsely or with long
#                 silences. Something here might not have been recorded; a
#                 candidate is weak evidence.
#   thin          one or two platforms, ever. Not enough to say either way.
#   unheard       nothing, ever. A candidate here says nothing at all about
#                 the platform's behaviour.
#
# NOTE what "unheard" does NOT distinguish: no receiver from no traffic. A
# cell may be out of range or may simply be somewhere nothing goes. Both
# disqualify a candidate for the same reason -- there is no evidence that a
# transmission there would have been recorded -- and reporting them as one
# class is deliberate. Guessing which one it is would be the error.
HEARD = "heard"
INTERMITTENT = "intermittent"
THIN = "thin"
UNHEARD = "unheard"

CLASSES = (HEARD, INTERMITTENT, THIN, UNHEARD)


def cell_index(v: float, cell: float) -> int:
    """Floor to a cell index, rounding first.

    Same guard as searched.py: floor() of a value that binary floating point
    puts a hair below a cell boundary lands one cell too low, and the cell it
    lands in is the neighbour of the one meant.
    """
    return math.floor(round(v / cell, 9))


def neighbour_class(classes: list[str]) -> str:
    """Read an empty cell from the cells around it.

    WHY AN EMPTY CELL IS NOT AUTOMATICALLY UNHEARD

    Cells are small and platforms are sparse. A 5 km square of well-covered
    shipping lane can be empty across twelve satellite passes simply because
    no ship happened to cross that particular square, and a 1 degree box at
    cruise altitude can be empty for an hour between airliners. Calling
    either "unheard" would throw away candidates in the middle of
    demonstrably covered space.

    So an empty cell inherits, conservatively:

        half or more of the neighbours heard   -> heard
        any neighbour heard or intermittent    -> intermittent
        neighbours exist but none of those     -> thin
        no neighbours at all                   -> unheard

    A cell's OWN evidence always wins when it has any; this is only consulted
    when it has none. The downgrade in the second case matters: one heard
    neighbour out of eight is not enough to promise a transmission here would
    have been recorded, but it is enough to refuse to call the place silent.
    """
    if not classes:
        return UNHEARD
    if classes.count(HEARD) * 2 >= len(classes):
        return HEARD
    if HEARD in classes or INTERMITTENT in classes:
        return INTERMITTENT
    return THIN


def cell_area_km2(row: int, cell_deg: float) -> float:
    """Rough ground area of one cell, by its row index.

    Cosine of the latitude, evaluated at the cell's middle. Good to a percent
    or so at these cell sizes, which is well inside how precisely any of
    these areas are used.
    """
    lat = (row + 0.5) * cell_deg
    return (cell_deg * 111.32 * math.cos(math.radians(lat))
            * cell_deg * 110.57)
