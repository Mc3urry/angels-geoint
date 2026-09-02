"""Identifier collisions and reuse.

PHASE 2.

An identifier in two places at once is not suspicious, it is impossible. That
makes this the highest-confidence detector in the set: no coverage model, no
threshold tuning, no judgement call. Either one object was in two places or
something is wrong with the data.

Two distinct findings live here:

    collisions  two reports of one identifier, close in time, too far apart
                for any single platform to have covered. Concurrent.

    reuse       one identifier appearing as clearly different platforms at
                different times. Sequential.

The first is a spoof or a decoding fault. The second is routine at sea, where
MMSIs are reassigned constantly, and much rarer in the air, where an ICAO24 is
burned into the airframe. That asymmetry is itself worth reporting: identity
in one domain means something quite different from identity in the other, and
a system that treats them the same is lying to you somewhere.

Domain-blind. Only PlausibilityModel.max_speed_mps decides what "too far
apart" means.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from angels.core.models import DiscrepancyEvent, Report, Track
from angels.core.plausibility import PlausibilityModel

# Reports closer together in time than this are treated as simultaneous.
# Below it, position uncertainty dominates and every platform looks like it
# is in two places.
SIMULTANEITY_S = 60.0


def _all_reports(tracks: list[Track]) -> dict[str, list[Report]]:
    by_id: dict[str, list[Report]] = defaultdict(list)
    for t in tracks:
        for r in t.reports:
            by_id[r.platform_id].append(r)
    for reports in by_id.values():
        reports.sort(key=lambda r: r.position.t)
    return by_id


def collisions(tracks: list[Track], plausibility: PlausibilityModel,
               *, window_s: float = SIMULTANEITY_S) -> list[DiscrepancyEvent]:
    """One identifier reporting from two places at effectively the same time.

    For each pair of reports within `window_s`, ask whether the platform could
    have covered the distance between them at its maximum speed. If not, the
    two reports cannot both be true.

    Only the worst pair per identifier is returned. A spoofed aircraft
    produces hundreds of impossible pairs, and reporting each one buries the
    finding in its own evidence.
    """
    events: list[DiscrepancyEvent] = []
    domain = tracks[0].domain if tracks else "air"

    for pid, reports in _all_reports(tracks).items():
        worst: tuple[float, Report, Report] | None = None

        for i, a in enumerate(reports):
            for b in reports[i + 1:]:
                dt = (b.position.t - a.position.t).total_seconds()
                if dt > window_s:
                    break        # sorted by time, so nothing later is closer
                reachable = plausibility.max_speed_mps * max(dt, 1.0)
                slack = a.position.combined_uncertainty(b.position)
                dist = a.position.distance_to(b.position)
                excess = dist - reachable - slack
                if excess > 0 and (worst is None or excess > worst[0]):
                    worst = (excess, a, b)

        if worst is None:
            continue

        excess, a, b = worst
        dist = a.position.distance_to(b.position)
        dt = (b.position.t - a.position.t).total_seconds()

        events.append(DiscrepancyEvent(
            kind="identity",
            domain=domain,
            t_start=a.position.t,
            t_end=b.position.t,
            lat=b.position.lat,
            lon=b.position.lon,
            # Physically impossible, so confidence is high by construction.
            # It scales only with how absurd the separation is.
            confidence=min(0.98, 0.75 + excess / 1_000_000),
            platform_ids=[pid],
            evidence={
                "reason": "identifier reported from two places at once",
                "separation_m": round(dist, 1),
                "interval_s": round(dt, 1),
                "max_reachable_m": round(plausibility.max_speed_mps * max(dt, 1.0), 1),
                "excess_m": round(excess, 1),
                "position_a": [a.position.lat, a.position.lon],
                "position_b": [b.position.lat, b.position.lon],
                "t_a": a.position.t.isoformat(),
                "t_b": b.position.t.isoformat(),
            },
        ))

    return events


def reuse(tracks: list[Track], plausibility: PlausibilityModel,
          *, min_gap_s: float = 3600.0) -> list[DiscrepancyEvent]:
    """One identifier resuming somewhere it could not have travelled to.

    Distinct from a collision: the reports are separated by a long silence,
    so nothing is simultaneous. The question is whether the platform could
    plausibly have made the journey during the gap. If not, the identifier
    is being used by more than one object.

    Expect this constantly in maritime data and almost never in aviation.
    A flood of these on the air side means something is wrong with your
    ingest, not with the sky.
    """
    events: list[DiscrepancyEvent] = []
    domain = tracks[0].domain if tracks else "air"

    for pid, reports in _all_reports(tracks).items():
        for a, b in zip(reports, reports[1:]):
            dt = (b.position.t - a.position.t).total_seconds()
            if dt < min_gap_s:
                continue

            dist = a.position.distance_to(b.position)
            reachable = plausibility.max_speed_mps * dt
            if dist <= reachable:
                continue          # it could have got there; not our business

            events.append(DiscrepancyEvent(
                kind="identity",
                domain=domain,
                t_start=a.position.t,
                t_end=b.position.t,
                lat=b.position.lat,
                lon=b.position.lon,
                confidence=min(0.9, 0.6 + (dist - reachable) / 5_000_000),
                platform_ids=[pid],
                evidence={
                    "reason": "identifier resumed beyond reachable range",
                    "separation_m": round(dist, 1),
                    "gap_s": round(dt, 1),
                    "gap_hours": round(dt / 3600, 1),
                    "max_reachable_m": round(reachable, 1),
                    "implied_speed_mps": round(dist / dt, 1),
                    "max_speed_mps": plausibility.max_speed_mps,
                },
            ))

    return events


def duplicates(tracks: list[Track]) -> list[DiscrepancyEvent]:
    """The same identifier at the same instant, twice.

    Not a physics question at all -- an exact timestamp collision is either a
    genuine duplicate broadcast or, more usually, the same message ingested
    twice. Worth surfacing because it is a data-quality problem that will
    otherwise quietly inflate every count you compute later.
    """
    events: list[DiscrepancyEvent] = []
    domain = tracks[0].domain if tracks else "air"

    for pid, reports in _all_reports(tracks).items():
        seen: dict[str, Report] = {}
        for r in reports:
            key = r.position.t.isoformat()
            prev = seen.get(key)
            if prev is not None and prev.position.distance_to(r.position) > 1.0:
                events.append(DiscrepancyEvent(
                    kind="identity",
                    domain=domain,
                    t_start=r.position.t,
                    t_end=r.position.t,
                    lat=r.position.lat,
                    lon=r.position.lon,
                    confidence=0.6,
                    platform_ids=[pid],
                    evidence={
                        "reason": "two differing reports share one timestamp",
                        "separation_m": round(
                            prev.position.distance_to(r.position), 1),
                        "t": key,
                    },
                ))
            seen[key] = r

    return events
