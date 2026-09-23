"""How well did the sensor do? A detection rate that can be defended.

The first two passes produced detection rates of 29%, 17% and 54%, and every
one of them was below the 60% line under which an unmatched detection says
more about the detector than about the sea. Read closely, two different
things were dragging those numbers down, and neither of them was really the
detector.

1. SMALL CRAFT. 15 of the 20 misses on 2024-06-21 slice 3 were under 25 m.
   Sentinel-1 IW GRD has 10 m pixels and roughly 20 x 22 m resolution, so a
   15 m boat is one or two pixels. Missing it is a property of the sensor,
   not a failure of the method -- and averaging it into one rate makes a
   sound pipeline look broken while hiding how it does on the ships it
   CAN see. So the rate is split by reported length, and the class boundary
   is fixed here, before any result is looked at, and is not to be tuned
   to one.

2. MATCHES THAT COULD BE COINCIDENCE. "TOW BOAT U.S BAILOUT" was matched at
   2.9 km. Its AIS fix at the acquisition instant was so uncertain that the
   admissible radius was about 5 km, and with 375 detections over 16,300 km2
   a circle that size contains a detection by chance most of the time. A
   pair like that neither shows the radar found the vessel nor that it
   missed it. So before any outcome is looked at, every track gets the
   probability that a RANDOM detection would fall inside its radius, and a
   track where that exceeds MAX_CHANCE_MATCH is not scored at all -- matched
   or missed.

   Deciding eligibility BEFORE looking at the outcome is the whole point.
   Excluding only the doubtful MATCHES would push the rate down; excluding
   only the doubtful MISSES would push it up. Excluding the tracks, both ways,
   by a rule that never sees whether they were found, does neither.

This affects the rate only. Poorly located tracks still stay in the matching
itself and can still explain a detection -- the conservative choice for dark
vessels, where an explanation that is merely possible must still be allowed
to explain.

THE CHANCE MODEL

Detections treated as a Poisson process over the searched water at the
observed density rho. The probability that at least one falls within r of a
point is 1 - exp(-rho * pi * r^2). Clutter raises rho and so, correctly,
makes every wide radius less trustworthy. Summed over the scored tracks it
also estimates how many of the "found" could be coincidences.

THE UNCERTAINTY

Each rate carries a 95% Wilson interval, because at these sample sizes the
point estimate is close to meaningless alone: 8 of 28 is 29%, and the
interval runs from about 15% to 47%. Wilson rather than the normal
approximation because the normal interval goes below zero and above one at
exactly the small counts this project has.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime

from ..models import Observation, Track
from .matching import (K_SIGMA, MAX_FIX_UNCERTAINTY_M, Association,
                       vessel_length_m)

# Fixed in advance and NOT to be tuned to a result. 25 m is the smallest
# class edge the miss summary already uses, and about the size of one
# Sentinel-1 IW GRD resolution cell, so anything below it is at most a pixel
# or two.
DETECTABLE_LENGTH_M = 25.0

# A track whose radius would hold a random detection more often than this is
# not scored. 5% is the conventional line, chosen before looking at any pass.
MAX_CHANCE_MATCH = 0.05

STRATA = (">=25 m", "<25 m", "unknown length", "all scored")

# The same evidence as a CURVE rather than a line in the sand. Two classes
# answer "is the detector good enough to trust"; the curve answers "what can
# this sensor see", which is the question a reviewer asks and the one a
# later study reuses. Edges fixed here, before any result.
CURVE_EDGES_M = (0.0, 15.0, 25.0, 50.0, 100.0, float("inf"))
CURVE_NAMES = tuple(
    f"{lo:g}-{hi:g} m" if hi != float("inf") else f">{lo:g} m"
    for lo, hi in zip(CURVE_EDGES_M, CURVE_EDGES_M[1:])) + ("unknown",)


def length_bin(track: Track) -> str:
    length = reported_length(track)
    if length is None:
        return "unknown"
    for lo, hi in zip(CURVE_EDGES_M, CURVE_EDGES_M[1:]):
        if lo <= length < hi:
            return (f"{lo:g}-{hi:g} m" if hi != float("inf")
                    else f">{lo:g} m")
    return CURVE_NAMES[-2]


def reported_length(track: Track) -> float | None:
    """The first positive length any report carries; None if none does.

    NOT matching.vessel_length_m, which substitutes a 50 m default for the
    match radius. For deciding which class a vessel belongs to, a default is
    a guess, and a vessel that really is 50 m long must not be mistaken for
    one whose length was never sent.
    """
    for r in track.reports:
        length = getattr(r, "length_m", None)
        if length and length > 0:
            return float(length)
    return None


def length_class(track: Track) -> str:
    length = reported_length(track)
    if length is None:
        return "unknown length"
    return ">=25 m" if length >= DETECTABLE_LENGTH_M else "<25 m"


def wilson(found: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for found/n. (nan, nan) when n is 0."""
    if n <= 0:
        return float("nan"), float("nan")
    p = found / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def chance_of_coincidence(radius_m: float, density_per_km2: float) -> float:
    """P(at least one random detection within radius_m)."""
    r_km = radius_m / 1000.0
    return 1.0 - math.exp(-density_per_km2 * math.pi * r_km * r_km)


@dataclass
class Rate:
    found: int = 0
    scored: int = 0

    @property
    def rate(self) -> float:
        return self.found / self.scored if self.scored else float("nan")

    @property
    def interval(self) -> tuple[float, float]:
        return wilson(self.found, self.scored)

    def __add__(self, other: "Rate") -> "Rate":
        return Rate(self.found + other.found, self.scored + other.scored)

    def to_json(self) -> dict:
        lo, hi = self.interval
        r = self.rate

        def rnd(v: float) -> float | None:
            return None if v != v else round(v, 4)

        return {"found": self.found, "scored": self.scored,
                "rate": rnd(r), "ci95": [rnd(lo), rnd(hi)]}


@dataclass
class Calibration:
    """The detection rate, split the way it has to be read."""

    strata: dict[str, Rate] = field(
        default_factory=lambda: {s: Rate() for s in STRATA})
    # The detectability curve: the same scored vessels, binned by length.
    curve: dict[str, Rate] = field(
        default_factory=lambda: {s: Rate() for s in CURVE_NAMES})
    # Located, but too loosely for a match to mean anything at this density.
    n_unscorable: int = 0
    n_unscorable_matched: int = 0
    unscorable: set[int] = field(default_factory=set)   # track indices
    # Sum over scored tracks of their chance-coincidence probability.
    expected_chance_matches: float = 0.0
    density_per_km2: float | None = None
    # The widest radius that still passes MAX_CHANCE_MATCH at this density.
    max_scorable_radius_m: float | None = None

    @property
    def headline(self) -> Rate:
        return self.strata[">=25 m"]

    def __add__(self, other: "Calibration") -> "Calibration":
        """Pool two passes. Counts add; density and radius are per-pass
        and have no pooled value."""
        return Calibration(
            strata={s: self.strata[s] + other.strata[s] for s in STRATA},
            curve={s: self.curve[s] + other.curve[s] for s in CURVE_NAMES},
            n_unscorable=self.n_unscorable + other.n_unscorable,
            n_unscorable_matched=(self.n_unscorable_matched
                                  + other.n_unscorable_matched),
            expected_chance_matches=(self.expected_chance_matches
                                     + other.expected_chance_matches),
        )

    def to_json(self) -> dict:
        return {
            "detectable_length_m": DETECTABLE_LENGTH_M,
            "max_chance_match": MAX_CHANCE_MATCH,
            "density_per_km2": (None if self.density_per_km2 is None
                                else round(self.density_per_km2, 5)),
            "max_scorable_radius_m": (None if self.max_scorable_radius_m is None
                                      else round(self.max_scorable_radius_m)),
            "n_unscorable": self.n_unscorable,
            "n_unscorable_matched": self.n_unscorable_matched,
            "expected_chance_matches": round(self.expected_chance_matches, 2),
            "strata": {s: r.to_json() for s, r in self.strata.items()},
            "curve_edges_m": [e for e in CURVE_EDGES_M if e != float("inf")],
            "curve": {s: r.to_json() for s, r in self.curve.items()},
        }


def calibrate(result: Association, tracks: list[Track],
              observations: list[Observation], t: datetime, *,
              searched_km2: float | None, k: float = K_SIGMA,
              density_per_km2: float | None = None,
              obs_uncertainty_m: float | None = None) -> Calibration:
    """Score each located track, once, by a rule that ignores its outcome.

    `tracks` must be the same list, in the same order, that `result` was
    computed from: the association refers to tracks by index.

    `density_per_km2` and `obs_uncertainty_m` override the values computed
    from `observations`.
    tune_detector.py passes the UNGATED density so that every gate is scored
    on the same set of vessels -- otherwise a stricter gate, by thinning the
    detections, would also change who counts, and two rates with different
    denominators would be compared as if they had the same one.
    """
    cal = Calibration()
    density = density_per_km2
    if density is None and searched_km2 and searched_km2 > 0:
        density = len(observations) / searched_km2
    if density:
        cal.density_per_km2 = density
        cal.max_scorable_radius_m = 1000.0 * math.sqrt(
            -math.log(1.0 - MAX_CHANCE_MATCH) / (math.pi * density)) \
            if density > 0 else None

    # One detection uncertainty for every track, so a track's eligibility
    # cannot depend on which detection it happened to be paired with.
    obs_unc = obs_uncertainty_m
    if obs_unc is None:
        obs_unc = (statistics.median(o.position.uncertainty_m
                                     for o in observations)
                   if observations else 0.0)
    found = set(c.track_index for c in result.pairs)

    for j, tr in enumerate(tracks):
        fix = tr.position_at(t)
        if fix is None:
            continue          # already outside the rate; see Association
        if fix.uncertainty_m > MAX_FIX_UNCERTAINTY_M:
            continue          # matching treated it as having no fix

        # The same radius formula as matching.pair_radius.
        radius = k * math.sqrt(obs_unc ** 2 + fix.uncertainty_m ** 2
                               + (vessel_length_m(tr) / 2) ** 2)
        p = chance_of_coincidence(radius, density) if density else 0.0
        if p > MAX_CHANCE_MATCH:
            cal.n_unscorable += 1
            cal.n_unscorable_matched += j in found
            cal.unscorable.add(j)
            continue

        hit = int(j in found)
        for s in (length_class(tr), "all scored"):
            cal.strata[s].found += hit
            cal.strata[s].scored += 1
        b = cal.curve[length_bin(tr)]
        b.found += hit
        b.scored += 1
        cal.expected_chance_matches += p
    return cal


def pct(v: float) -> str:
    return "  n/a" if v != v else f"{100 * v:4.0f}%"


def format_curve(cal: Calibration, indent: str = "  ") -> list[str]:
    """The detectability curve, shortest first, unknown last."""
    lines = [f"{indent}{'length':>10}{'found':>7} / {'scored':<7}{'rate':>6}"
             f"   95% range"]
    for s in CURVE_NAMES:
        r = cal.curve.get(s, Rate())
        if r.scored == 0:
            continue
        lo, hi = r.interval
        lines.append(f"{indent}{s:>10}{r.found:>7} / {r.scored:<7}"
                     f"{pct(r.rate):>6}   {pct(lo).strip()}-{pct(hi).strip()}")
    return lines


def format_table(cal: Calibration, indent: str = "  ") -> list[str]:
    """The rate table as printed lines."""
    lines = [f"{indent}{'':16}{'found':>7} / {'scored':<7}{'rate':>6}   95% range"]
    for s in STRATA:
        r = cal.strata[s]
        lo, hi = r.interval
        rng = "" if r.scored == 0 else f"{pct(lo).strip()}-{pct(hi).strip()}"
        lines.append(f"{indent}{s:16}{r.found:>7} / {r.scored:<7}"
                     f"{pct(r.rate):>6}   {rng}")
    return lines
