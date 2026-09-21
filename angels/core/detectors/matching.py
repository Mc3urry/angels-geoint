"""Observation to report association. THE CORE SUBTRACTION.

PHASE 2. What a sensor saw, minus what anyone reported. The leftovers are the
product -- and so, just as importantly, are the leftovers on the other side.

TWO DIRECTIONS, NOT ONE

    observed, never reported     a candidate dark vessel. The finding.
    reported, never observed     a MISS by the sensor. The calibration.

It is tempting to write only the first. The second is what licenses it.
Without a measured miss rate, "no report accounts for this detection" and
"this detector cannot see things like that" produce identical output, and the
first is a result while the second is an artefact. A project that reports dark
vessels without reporting how often it fails to see reported ones has not
measured anything; it has described its own blind spots in the language of
evidence.

So association returns both, and the miss rate is computed from the same run
that produces the findings -- not estimated separately, not assumed.

WHY MAXIMUM MATCHING AND NOT NEAREST NEIGHBOUR

The obvious implementation gives each observation its closest report inside a
radius. It is wrong in a way that inflates the headline number.

Two vessels 200 m apart, two detections. Nearest-neighbour hands both
detections to the same track and calls the second one dark. Assigning greedily
by distance is better but still fails: take detections A and B, where A is
110 m from track 1 and 130 m from track 2, and B is 120 m from track 1 and out
of range of track 2. Greedy pairs A with track 1 -- the shortest link
available -- and B is left with nothing, reported as a dark vessel. But the
pairing A-track 2, B-track 1 explains BOTH, and every link in it is admissible.

The error only ever runs one way: greedy invents dark vessels, never hides
them. So association maximises the NUMBER of admissible pairings, which makes
"unmatched" mean what it should -- no assignment of reports to detections
explains this one -- rather than merely "the first pass did not get to it".

That is Kuhn's algorithm, written out below, on the bipartite graph of pairs
already inside their own tolerance. It is the conservative reading, and the
conservative reading is the one a reviewer cannot take away from you.

THE RADIUS

Never a constant. Each candidate pair carries its own, from:

  * the detection's own uncertainty -- geolocation residual and cluster extent
  * the interpolated fix's uncertainty, which Track.position_at already
    inflates by how far it had to reach
  * half the vessel's length, because AIS reports the transponder and SAR
    reports the brightest scatterer, and on a 300 m hull those are not the
    same place

added in quadrature and scaled by k. Getting this wrong in either direction is
the difference between a system that flags everything and one that flags
nothing, and neither failure announces itself.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from ..models import DiscrepancyEvent, Observation, Position, Track

# Sigmas. 3.0 keeps a genuine pairing inside the radius about 99% of the time
# under a Gaussian error model, which SAR geolocation error is not -- so treat
# this as a tuning knob validated against a scene with known traffic, not as a
# statistical guarantee.
K_SIGMA = 3.0

# Used when a track carries no length. A mid-size coastal vessel; big enough
# that small craft are not penalised, small enough that it does not quietly
# widen every radius by a ship's length.
DEFAULT_VESSEL_LENGTH_M = 50.0

# Below this, an interpolated fix is not evidence of anything. position_at
# will happily dead-reckon a vessel for hours past its last report and inflate
# the uncertainty as it goes; past a point the honest response is to stop
# calling the result a position. A vessel doing 12 knots covers 13 km in half
# an hour, and a 13 km radius matches most of the bay.
MAX_FIX_UNCERTAINTY_M = 2000.0


@dataclass(frozen=True)
class Candidate:
    """One admissible observation-to-track pairing, and why it is admissible."""

    obs_index: int
    track_index: int
    fix: Position
    distance_m: float
    radius_m: float

    @property
    def slack(self) -> float:
        """How far inside its own tolerance this pairing sits, 0..1.

        1.0 is a perfect coincidence, 0.0 is right on the edge. Used to break
        ties between equally valid assignments, and worth carrying into the
        evidence: a match at 0.02 slack is not the same claim as one at 0.9.
        """
        return 1.0 - self.distance_m / self.radius_m if self.radius_m else 0.0


@dataclass
class Association:
    """The result of one subtraction, in both directions.

    Deliberately not a list of events. Events are a view of this; the miss
    rate is the other view, and discarding it at this stage is how a project
    ends up unable to say whether its findings are findings.
    """

    t: datetime
    pairs: list[Candidate] = field(default_factory=list)
    unmatched_observations: list[int] = field(default_factory=list)
    unmatched_tracks: list[int] = field(default_factory=list)
    n_observations: int = 0
    n_tracks: int = 0
    # Tracks whose fix at t was unusable -- no report to interpolate from, or
    # too far extrapolated to mean anything. NOT counted as misses: the sensor
    # was never given a chance to be wrong about them.
    n_tracks_without_fix: int = 0

    @property
    def detection_rate(self) -> float:
        """Of the vessels that reported themselves HERE and THEN, how many did
        the sensor find?

        This is the number that makes the dark-vessel count mean something. At
        0.9 an unmatched detection is interesting. At 0.4 the sensor is
        missing more than half of what it is pointed at, and 'unmatched' says
        more about the detector than about the sea.
        """
        eligible = self.n_tracks - self.n_tracks_without_fix
        return len(self.pairs) / eligible if eligible else float("nan")

    @property
    def unmatched_fraction(self) -> float:
        """Share of detections no report explains. The headline, and not
        interpretable on its own -- read it beside detection_rate."""
        if not self.n_observations:
            return float("nan")
        return len(self.unmatched_observations) / self.n_observations

    def __str__(self) -> str:
        rate = self.detection_rate
        rate_s = "n/a" if rate != rate else f"{100 * rate:.0f}%"
        return (f"{len(self.pairs)} matched, "
                f"{len(self.unmatched_observations)} unmatched detections, "
                f"{len(self.unmatched_tracks)} missed reports "
                f"(detection rate {rate_s})")


def vessel_length_m(track: Track) -> float:
    """Reported length, if any report carries one.

    AIS carries Length; ADS-B does not, and for aircraft the term is
    meaningless at this scale anyway. Adapters put it in Report-adjacent
    attributes where they have it; absent, the default stands in and the
    radius is slightly optimistic for large vessels -- which costs recall on
    exactly the traffic most worth catching, so it is worth wiring through.
    """
    for r in track.reports:
        length = getattr(r, "length_m", None)
        if length:
            return float(length)
    return DEFAULT_VESSEL_LENGTH_M


def pair_radius(obs: Observation, fix: Position, length_m: float,
                k: float = K_SIGMA) -> float:
    """How far apart these two may be and still be the same vessel."""
    combined = obs.position.combined_uncertainty(fix)
    return k * (combined ** 2 + (length_m / 2) ** 2) ** 0.5


def candidates(observations: list[Observation], tracks: list[Track],
               t: datetime, *, k: float = K_SIGMA,
               max_fix_uncertainty_m: float = MAX_FIX_UNCERTAINTY_M
               ) -> tuple[list[Candidate], int]:
    """Every admissible pairing at instant t, and how many tracks had no fix.

    Both are returned because the second is not a detail: a track with no
    usable fix at t is not a miss. It is a vessel the sensor was never given
    the chance to see, and counting it as a miss would depress the detection
    rate with arithmetic rather than with evidence.
    """
    fixes: list[Position | None] = []
    for tr in tracks:
        fix = tr.position_at(t)
        if fix is not None and fix.uncertainty_m > max_fix_uncertainty_m:
            fix = None
        fixes.append(fix)

    # ONCE per track, not once per pair. vessel_length_m scans a track's
    # reports looking for a length, and a vessel reporting every two seconds
    # for an hour has 1,800 of them. Called inside the inner loop against 845
    # detections that is 1.5 million attribute lookups for a single track, and
    # the whole run stops responding -- which looks like a hang rather than
    # like slow arithmetic.
    lengths = [vessel_length_m(tr) for tr in tracks]

    # SPATIAL INDEX. Every detection against every track is O(n*m), and almost
    # all of those pairs are hundreds of kilometres apart. Bucketing the fixes
    # into cells at least as wide as the largest possible match radius means
    # only the nine cells around a detection can contain a partner, and the
    # comparison count drops by three or four orders of magnitude.
    #
    # The cell size is DERIVED from the widest radius any pair could have, not
    # chosen: too small and a real pairing falls outside the neighbourhood and
    # is reported as a dark vessel, which is exactly the error this module
    # exists to avoid.
    biggest = max(lengths) if lengths else DEFAULT_VESSEL_LENGTH_M
    worst_obs_unc = max((o.position.uncertainty_m for o in observations),
                        default=0.0)
    max_radius_m = k * math.hypot(
        math.hypot(worst_obs_unc, max_fix_uncertainty_m), biggest / 2)

    lat_deg = max_radius_m / 111_195.0
    worst_lat = max((abs(f.lat) for f in fixes if f is not None), default=0.0)
    lon_deg = max_radius_m / (111_195.0 * max(math.cos(math.radians(worst_lat)),
                                              0.01))
    cell_lat = max(lat_deg, 1e-6)
    cell_lon = max(lon_deg, 1e-6)

    buckets: dict[tuple[int, int], list[int]] = {}
    for j, fix in enumerate(fixes):
        if fix is None:
            continue
        buckets.setdefault((int(fix.lat // cell_lat), int(fix.lon // cell_lon)),
                           []).append(j)

    out: list[Candidate] = []
    for i, obs in enumerate(observations):
        p = obs.position
        gr, gc = int(p.lat // cell_lat), int(p.lon // cell_lon)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                for j in buckets.get((gr + dr, gc + dc), ()):
                    fix = fixes[j]
                    d = p.distance_to(fix)
                    r = pair_radius(obs, fix, lengths[j], k=k)
                    if d <= r:
                        out.append(Candidate(i, j, fix, d, r))

    return out, sum(f is None for f in fixes)


def _maximum_matching(pairs: list[Candidate], n_obs: int
                      ) -> dict[int, Candidate]:
    """Kuhn's algorithm: the largest set of pairings using each side once.

    Returns {track_index: Candidate}. Augmenting paths, depth-first, one pass
    per observation -- O(V*E), which for a few hundred detections against a
    few thousand tracks is milliseconds.

    Candidates are considered tightest-first within each observation, so among
    the several maximum matchings that may exist, the one found tends to use
    the closer pairings. Cardinality is what is being maximised; distance only
    breaks ties. Maximising total closeness instead would be the assignment
    problem, and would buy nothing here: it cannot change HOW MANY detections
    end up unexplained, which is the quantity every downstream claim rests on.
    """
    by_obs: dict[int, list[Candidate]] = {}
    for c in pairs:
        by_obs.setdefault(c.obs_index, []).append(c)
    for lst in by_obs.values():
        lst.sort(key=lambda c: c.distance_m)

    taken: dict[int, Candidate] = {}

    def augment(i: int, seen: set[int]) -> bool:
        for c in by_obs.get(i, ()):
            if c.track_index in seen:
                continue
            seen.add(c.track_index)
            held = taken.get(c.track_index)
            if held is None or augment(held.obs_index, seen):
                taken[c.track_index] = c
                return True
        return False

    for i in range(n_obs):
        augment(i, set())
    return taken


def associate(observations: list[Observation], tracks: list[Track],
              t: datetime, *, k: float = K_SIGMA,
              max_fix_uncertainty_m: float = MAX_FIX_UNCERTAINTY_M
              ) -> Association:
    """Pair what was observed against what was reported, at one instant.

    t is the acquisition instant, not "about then". Every track is advanced to
    it by interpolation; a scene whose timestamp is wrong produces confident,
    wrong discrepancies, which is why detect_ships.py parses it from the
    filename rather than from the file's modification time.
    """
    pairs, no_fix = candidates(observations, tracks, t, k=k,
                               max_fix_uncertainty_m=max_fix_uncertainty_m)
    taken = _maximum_matching(pairs, len(observations))

    matched = sorted(taken.values(), key=lambda c: c.obs_index)
    used_obs = {c.obs_index for c in matched}
    used_tracks = set(taken)

    return Association(
        t=t,
        pairs=matched,
        unmatched_observations=[i for i in range(len(observations))
                                if i not in used_obs],
        unmatched_tracks=[j for j in range(len(tracks))
                          if j not in used_tracks],
        n_observations=len(observations),
        n_tracks=len(tracks),
        n_tracks_without_fix=no_fix,
    )


def unmatched(observations: list[Observation], tracks: list[Track],
              t: datetime, *, k: float = K_SIGMA,
              domain: str = "sea") -> list[DiscrepancyEvent]:
    """Detections no report explains, as events.

    CONFIDENCE HERE IS NOT THE DETECTOR'S CONFIDENCE. A bright, unambiguous
    radar return that no AIS accounts for is strong evidence of a vessel and
    says nothing on its own about whether that vessel was hiding -- the sensor
    may simply be in a part of the scene where reports are sparse. So the
    event's confidence combines how good the detection was with how well the
    sensor was doing at that instant: on a pass where only half the reported
    traffic was found, an unmatched detection is much weaker evidence than on
    a pass where nearly all of it was.

    That coupling is the point. It makes a poorly performing run produce
    weaker claims automatically, instead of producing the same claims with no
    indication that anything was wrong.
    """
    result = associate(observations, tracks, t, k=k)
    rate = result.detection_rate
    if rate != rate:                      # NaN -- nothing reported to compare
        rate = 0.0

    events: list[DiscrepancyEvent] = []
    for i in result.unmatched_observations:
        obs = observations[i]
        detector_conf = float(obs.attributes.get("confidence", 0.5))
        events.append(DiscrepancyEvent(
            kind="unmatched",
            domain=domain,          # type: ignore[arg-type]
            t_start=obs.position.t,
            t_end=obs.position.t,
            lat=obs.position.lat,
            lon=obs.position.lon,
            confidence=max(0.0, min(1.0, detector_conf * rate)),
            platform_ids=[],
            evidence={
                "sensor": obs.sensor,
                "detector_confidence": round(detector_conf, 3),
                "detection_rate_this_pass": round(rate, 3),
                "reports_considered": result.n_tracks,
                "reports_with_a_fix": result.n_tracks - result.n_tracks_without_fix,
                "search_radius_m": round(
                    pair_radius(obs, obs.position, DEFAULT_VESSEL_LENGTH_M,
                                k=k), 1),
                **{key: obs.attributes[key] for key in
                   ("snr", "length_m_approx", "pixels", "scene")
                   if key in obs.attributes},
            },
        ))
    return events
