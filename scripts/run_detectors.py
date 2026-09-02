"""Run the detectors over the archive and write events.

    python scripts/run_detectors.py                 # last 24h
    python scripts/run_detectors.py --hours 6
    python scripts/run_detectors.py --dry-run       # report, write nothing
    python scripts/run_detectors.py -v              # show every event

Output: data/events/aviation-YYYY-MM-DDTHH.jsonl

WHAT RUNS TODAY, AND WHAT DOES NOT

Five of the seven detectors analyse what platforms SAY about themselves, and
those need nothing but the archive. Two do not run yet:

    matching    needs MLAT observations, which come only from OpenSky's Trino
                historical access. Without an independent sensing channel
                there is nothing to subtract, so this is not a detector that
                degrades gracefully -- it simply cannot run.

    gaps        needs coverage.py. A silence is only interesting once you can
                say a report would have been received had it been sent, and
                the naive version would report your own downtime as a finding.
                core.uptime.blind_intervals is the first half of that answer;
                the coverage model is the second.

So the honest summary printed at the end says which detectors ran and which
were skipped, rather than presenting a partial sweep as a complete one.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from angels.adapters.aviation.opensky import AviationAdapter
from angels.config import AOI_AIR, EVENTS, RAW
from angels.core.detectors import identity, kinematics
from angels.core.models import DiscrepancyEvent
from angels.core.uptime import blind_intervals


def run(tracks, plausibility, domain: str) -> dict[str, list[DiscrepancyEvent]]:
    """Every detector that can run without an observation channel."""
    out: dict[str, list[DiscrepancyEvent]] = {}

    out["kinematic"] = []
    for t in tracks:
        out["kinematic"] += kinematics.validate(t, plausibility)

    out["kinematic_disagreement"] = []
    for t in tracks:
        out["kinematic_disagreement"] += kinematics.speed_disagreement(
            t, plausibility)

    out["identity_collision"] = identity.collisions(tracks, plausibility)
    out["identity_reuse"] = identity.reuse(tracks, plausibility)
    out["identity_duplicate"] = identity.duplicates(tracks)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--domain", choices=["air"], default="air")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=args.hours)

    adapter = AviationAdapter()
    tracks = adapter.tracks(start, end, AOI_AIR)

    print(f"\n  {args.domain}  {start:%Y-%m-%d %H:%M} to {end:%H:%M} UTC")

    if not tracks:
        print("\n  No tracks in the archive for that window.")
        print("  Is the collector running?  .\\collector.ps1 status\n")
        return 1

    reports = sum(len(t) for t in tracks)
    print(f"  {len(tracks)} tracks, {reports:,} reports\n")

    # Say plainly how much of the window we were actually watching. A sweep
    # over a window we were mostly asleep for is not a sweep.
    blind = blind_intervals(RAW, start, end, collector="aviation")
    if blind:
        lost = sum((b - a).total_seconds() for a, b in blind) / 3600
        pct = 100 * lost / args.hours
        print(f"  NOT COLLECTING for {lost:.1f}h of {args.hours:.0f}h ({pct:.0f}%)")
        for a, b in blind[:5]:
            print(f"    {a:%m-%d %H:%M} to {b:%m-%d %H:%M}")
        print()

    results = run(tracks, adapter.plausibility, args.domain)

    total = 0
    print("  detector                  events")
    print("  " + "-" * 34)
    for name, events in results.items():
        total += len(events)
        mark = " " if events else " "
        print(f"  {name:<24}{mark}{len(events):>5}")
    print(f"  {'gaps':<24} {'--':>5}   (needs coverage.py)")
    print(f"  {'matching':<24} {'--':>5}   (needs MLAT)")
    print(f"  {'loiter, orbits, rendezvous':<24}{'':>2}--   (not written yet)")
    print("  " + "-" * 34)
    print(f"  {'total':<24} {total:>5}\n")

    flat = [e for evs in results.values() for e in evs]

    if args.verbose and flat:
        for e in sorted(flat, key=lambda e: -e.confidence)[:25]:
            print(f"  {e.confidence:.2f}  {e.kind:<10} {','.join(e.platform_ids):<10} "
                  f"{e.evidence.get('reason','')}")
        print()

    if flat and not args.dry_run:
        EVENTS.mkdir(parents=True, exist_ok=True)
        path = EVENTS / f"{args.domain}-{end:%Y-%m-%dT%H}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for e in flat:
                fh.write(json.dumps(e.to_geojson()) + "\n")
        print(f"  wrote {len(flat)} events -> {path}\n")
    elif args.dry_run:
        print("  dry run, nothing written\n")

    if total == 0:
        print("  Nothing found. On a few hours of ordinary traffic over a")
        print("  well-regulated metro that is the expected result, not a")
        print("  broken detector -- the tests prove they fire on injected")
        print("  faults. Let the archive grow.\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
