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
    on at least MIN_FRACTION of the dates whose searched water covered it, AND
    no detection there was ever matched to an AIS report.

Each clause removes a different mistake:

  MIN_DATES        two passes can put two different ships in the same
                   kilometre by chance; a dozen dates make that unlikely.
  MIN_FRACTION     the denominator is the passes that SEARCHED the spot, not
                   all passes. A site inside the coastal blind zone on half
                   the passes must not be judged as though it were looked at
                   and found empty. Same principle as the detection rate's
                   searched-water denominator.
  never matched    an anchorage is also "always occupied", but by DIFFERENT
                   vessels, and those report themselves. A site where AIS
                   ever explained a detection is traffic, not furniture --
                   and dropping it would mean dropping the one place where a
                   vessel that goes dark at anchor would show up.

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
    def n_searched(self) -> int:
        # A date it was detected on was, by construction, searched -- even if
        # the searched grid disagrees at the cell edge.
        return len(self.searched_dates | self.dates)

    @property
    def hit_fraction(self) -> float:
        return self.n_dates / self.n_searched if self.n_searched else float("nan")

    @property
    def ever_matched(self) -> bool:
        return bool(self.matched_dates)

    def is_fixed(self, *, min_dates: int = MIN_DATES,
                 min_fraction: float = MIN_FRACTION) -> bool:
        return (self.n_dates >= min_dates
                and self.hit_fraction >= min_fraction
                and not self.ever_matched)

    def verdict(self, **kw) -> str:
        if self.ever_matched:
            return "traffic (AIS explained it at least once)"
        if self.is_fixed(**kw):
            return "fixed structure"
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
