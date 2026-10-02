"""Things that are always there. The second subtraction.

A detection no AIS report explains is not yet a dark vessel. Some of them are
not vessels at all: wind turbines, met masts, platforms, the artificial
islands of the Chesapeake Bay Bridge-Tunnel, lighthouses, wrecks, buoys.
They are real radar targets, they never carry AIS, and they appear on EVERY
pass in the same spot. Counting them as dark vessels would inflate the
headline and -- worse -- concentrate the inflation at fixed locations, which
is exactly the shape of the finding this project is looking for.

The water mask used to hide them, by calling any bright isolated blob land.
That also hid the largest ships (2026-09-22), so the mask now leaves them in
and they are removed HERE instead, by the one property that separates a
structure from a vessel: a structure does not leave.

THE RULE

A site is a cluster of detections within RADIUS_M of each other across
passes. It is called fixed when

    it was detected on at least MIN_DATES separate dates, AND
    on at least MIN_FRACTION of the dates whose searched water covered it.

Until 2026-09-30 there was a third clause -- no detection there was ever
matched to an AIS report -- and it is now off by default. It survives as
`require_never_matched`, so the old rule is still runnable and the old
result still reproducible. Why it changed is below.

Each clause removes a different mistake:

  MIN_DATES        two passes can put two different ships in the same
                   kilometre by chance; a dozen dates make that unlikely.
  MIN_FRACTION     the denominator is the passes that SEARCHED the spot
                   SINCE IT FIRST APPEARED, not all passes. Two reasons, and
                   the second was found the hard way. A site inside the
                   coastal blind zone on half the passes must not be judged
                   as though it were looked at and found empty -- the same
                   principle as the detection rate's searched-water
                   denominator. And a structure that is BUILT during the
                   study year is absent before it exists: counting those
                   earlier passes against it kept a whole wind farm under
                   construction in the candidate list, where it produced a
                   4x "concentration" 1-5 nm outside the contiguous zone
                   that was really 85 monopiles going in off Virginia Beach
                   between September and December 2024.
  never matched    (VETO REMOVED 2026-09-30 -- reasoning kept, see below)
                   an anchorage is also "always occupied", but by DIFFERENT
                   vessels, and those report themselves. A site where AIS
                   ever explained a detection is traffic, not furniture --
                   and dropping it would mean dropping the one place where a
                   vessel that goes dark at anchor would show up.

WHY THE never-matched VETO CAME OFF (2026-09-30)

The argument above is sound about anchorages and wrong about berths. As a
veto it exempted precisely the fixed structures most likely to have a
reporting vessel alongside: piers, terminals, berths, and lease areas under
construction. Cove Point LNG pier is the proof -- detected on 7 of 7 passes,
hit_fraction 1.00, and classified "traffic" solely because AIS explained a
detection there once, which is what a berth looks like when it is working.

A structure does not stop being a structure because a ship tied up to it.
MIN_DATES and MIN_FRACTION already carry the load the veto was doing: an
anchorage occupied by different vessels on different passes is not detected
in the SAME 300 m across dates unless something is moored there every time,
and a site that IS detected every time is furniture whatever AIS says.

What this costs, stated rather than hidden: a vessel that goes dark while
moored at the same berth on three or more passes is now removed with the
berth. That is the case the veto protected, and it is a real loss. It is
accepted because the veto's price was 6 structures kept in the candidate
list at 3+ dates and hit fractions above 0.5, and structures cluster at
fixed points -- the same shape as the finding this project is looking for.
Keeping them in does not merely inflate the count, it biases it.

Measured on the 2024 study year before the change (147 labelled chips,
post-pass-3 labels): 6 more sites called fixed, 43 of 1,150 candidates
removed -- 35 beyond 10 nm, 8 between 2 and 10 nm, NONE within 2 nm. Of the
labelled chips it removes two read as "fixed", one as "clutter", one as
"ambiguous", and no chip read as a vessel.

NOTE THE DIRECTION. Every removal falls in the outer two bands, so this
change mechanically RAISES the within-2 nm share -- it pushes toward the
finding. That is why it is committed, with this reasoning, before the
corrected nulls are run and not after.

NOT ADOPTED: also accepting >=2 dates at hit_fraction >=0.9. It would call
34 more sites fixed and remove 91 candidates, including 2 within 2 nm that
were both read as fixed structures -- so it agrees with the reader there.
But 28 of those 34 sites rest on two passes only, which is exactly the
coincidence MIN_DATES=3 exists to prevent. Recorded as a sensitivity, not a
rule.

WHAT THIS DELIBERATELY DOES NOT DO

It does not decide what the structure IS. Naming it would need a chart
overlay this project does not have and does not need: the claim is that
something was there on every pass and never reported itself, which is a
statement about the data. A later chart comparison can label them.

A vessel that moors in the same berth every twelve days for a year would
look fixed. That is a real limit, stated rather than hidden: with a 12-day
repeat this method cannot separate "never moved" from "always there when
looked at". Chart labelling, or a finer time series, is what would.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

RADIUS_M = 300.0
MIN_DATES = 3
MIN_FRACTION = 0.5

# 2026-09-30: was an unconditional True, written into is_fixed rather than
# named. Amendment recorded in the module docstring and in DECISIONS.md.
REQUIRE_NEVER_MATCHED = False

M_PER_DEG_LAT = 111_195.0


def metres_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    """(lon, lat) pairs to metres. Equirectangular; fine at 300 m."""
    lat_mid = math.radians((a[1] + b[1]) / 2)
    dx = (a[0] - b[0]) * M_PER_DEG_LAT * math.cos(lat_mid)
    dy = (a[1] - b[1]) * M_PER_DEG_LAT
    return math.hypot(dx, dy)


@dataclass
class Site:
    """One place detections keep coming back to."""

    lon: float
    lat: float
    n_detections: int = 0
    dates: set[str] = field(default_factory=set)
    matched_dates: set[str] = field(default_factory=set)
    # Dates whose searched water covered this site -- the denominator.
    searched_dates: set[str] = field(default_factory=set)
    max_snr: float = 0.0

    @property
    def n_dates(self) -> int:
        return len(self.dates)

    @property
    def first_seen(self) -> str | None:
        return min(self.dates) if self.dates else None

    @property
    def n_searched(self) -> int:
        # A date it was detected on was, by construction, searched -- even if
        # the searched grid disagrees at the cell edge.
        return len(self.searched_dates | self.dates)

    @property
    def n_searched_since_first(self) -> int:
        """Passes that searched here on or after it was first seen.

        The denominator for a thing that may not have existed all year. See
        MIN_FRACTION in the module docstring.
        """
        first = self.first_seen
        if first is None:
            return 0
        return len({d for d in (self.searched_dates | self.dates) if d >= first})

    @property
    def hit_fraction(self) -> float:
        n = self.n_searched_since_first
        return self.n_dates / n if n else float("nan")

    @property
    def hit_fraction_all_passes(self) -> float:
        """The old, stricter reading. Kept because the gap between the two
        is exactly the signature of something that was built mid-year."""
        return self.n_dates / self.n_searched if self.n_searched else float("nan")

    @property
    def ever_matched(self) -> bool:
        return bool(self.matched_dates)

    def is_fixed(self, *, min_dates: int = MIN_DATES,
                 min_fraction: float = MIN_FRACTION,
                 require_never_matched: bool = REQUIRE_NEVER_MATCHED) -> bool:
        if require_never_matched and self.ever_matched:
            return False
        return (self.n_dates >= min_dates
                and self.hit_fraction >= min_fraction)

    def verdict(self, **kw) -> str:
        if self.is_fixed(**kw):
            appeared = (self.n_searched_since_first < self.n_searched
                        and self.hit_fraction_all_passes < 0.5)
            if self.ever_matched:
                # Kept distinct from a never-matched structure: this is the
                # class the removed veto used to exempt, and a reader must be
                # able to count them without re-deriving anything.
                return ("fixed structure (appeared during the year, AIS "
                        "explained it at least once)" if appeared else
                        "fixed structure (AIS explained it at least once)")
            if appeared:
                return "fixed structure (appeared during the year)"
            return "fixed structure"
        if self.ever_matched:
            return "traffic (AIS explained it at least once)"
        if self.n_dates >= 2:
            return "repeat, not yet fixed"
        return "one pass only"

    def to_geojson(self, **kw) -> dict:
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(self.lon, 5),
                                                          round(self.lat, 5)]},
            "properties": {
                "n_detections": self.n_detections,
                "n_dates_seen": self.n_dates,
                "n_dates_searched": self.n_searched,
                "hit_fraction": round(self.hit_fraction, 3),
                "n_searched_since_first": self.n_searched_since_first,
                "first_seen": self.first_seen,
                "dates": sorted(self.dates),
                "ever_matched": self.ever_matched,
                "max_snr": round(self.max_snr, 1),
                "fixed": self.is_fixed(**kw),
                "verdict": self.verdict(**kw),
            },
        }


class SiteIndex:
    """Clusters detections into sites, by proximity, as they are added.

    A grid of RADIUS_M cells keeps this linear: only the nine cells around a
    point can hold a site within the radius. Sites keep the mean position of
    their detections, so a site does not drift toward whichever pass came
    first.
    """

    def __init__(self, radius_m: float = RADIUS_M) -> None:
        self.radius_m = radius_m
        self._sites: list[Site] = []
        self._cells: dict[tuple[int, int], list[int]] = {}
        self._sums: list[tuple[float, float]] = []      # lon, lat running sums

    # -- internals ---------------------------------------------------------

    def _cell(self, lon: float, lat: float) -> tuple[int, int]:
        d_lat = self.radius_m / M_PER_DEG_LAT
        d_lon = d_lat / max(math.cos(math.radians(lat)), 0.01)
        return int(math.floor(lat / d_lat)), int(math.floor(lon / d_lon))

    def _nearest(self, lon: float, lat: float) -> int | None:
        r, c = self._cell(lon, lat)
        best, best_d = None, self.radius_m
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                for i in self._cells.get((r + dr, c + dc), ()):
                    s = self._sites[i]
                    d = metres_between((lon, lat), (s.lon, s.lat))
                    if d <= best_d:
                        best, best_d = i, d
        return best

    def _reindex(self, i: int, old: tuple[float, float]) -> None:
        old_cell, new_cell = self._cell(*old), self._cell(self._sites[i].lon,
                                                          self._sites[i].lat)
        if old_cell != new_cell:
            self._cells[old_cell].remove(i)
            self._cells.setdefault(new_cell, []).append(i)

    # -- use ---------------------------------------------------------------

    def add(self, lon: float, lat: float, date: str, *, matched: bool = False,
            snr: float = 0.0) -> Site:
        i = self._nearest(lon, lat)
        if i is None:
            s = Site(lon=lon, lat=lat)
            self._sites.append(s)
            self._sums.append((lon, lat))
            self._cells.setdefault(self._cell(lon, lat), []).append(
                len(self._sites) - 1)
            i = len(self._sites) - 1
        else:
            s = self._sites[i]
            old = (s.lon, s.lat)
            lon_sum, lat_sum = self._sums[i]
            self._sums[i] = (lon_sum + lon, lat_sum + lat)
            n = s.n_detections + 1
            s.lon, s.lat = self._sums[i][0] / n, self._sums[i][1] / n
            self._reindex(i, old)

        s = self._sites[i]
        s.n_detections += 1
        s.dates.add(date)
        s.max_snr = max(s.max_snr, snr)
        if matched:
            s.matched_dates.add(date)
        return s

    def mark_searched(self, date: str, covers) -> None:
        """Record that `date` searched wherever `covers(lon, lat)` is true.

        Called once per date with that date's searched-water test, AFTER all
        detections are in: the denominator is which passes looked at each
        site, and a site can only be located once it exists.
        """
        for s in self._sites:
            if covers(s.lon, s.lat):
                s.searched_dates.add(date)

    @property
    def sites(self) -> list[Site]:
        return list(self._sites)

    def fixed(self, **kw) -> list[Site]:
        return [s for s in self._sites if s.is_fixed(**kw)]

    def is_near_fixed(self, lon: float, lat: float, **kw) -> bool:
        i = self._nearest(lon, lat)
        return i is not None and self._sites[i].is_fixed(**kw)
