"""Is there an INDEPENDENT aviation channel over the AOI at all?

    python scripts/check_tisb.py
    python scripts/check_tisb.py --lat 38.95 --lon -77.0 --dist 70

WHAT THIS IS FOR

The maritime half of this project compares AIS (self-reported) against
Sentinel-1 (observed without consent). The aviation half is supposed to
compare ADS-B against MLAT and TIS-B -- positions derived from receiver
geometry or uplinked from ground radar, neither of which an aircraft chooses
to emit.

This script asks whether that second channel is actually present overhead. It
is the aviation equivalent of the reception grid: before reading anything
into an absence, find out whether the absence could have been observed.

WHY IT WAS REWRITTEN ON 2026-09-25

The previous version read the aircraft list from `d.get("ac")`. adsb.fi
returns it under `aircraft`. So it printed

    0 aircraft within 70 nm of DC

every time it was ever run, and an empty table under it -- indistinguishable
from a sky with no independent traffic in it. The one probe whose job was to
say whether the aviation channel existed had been answering "no" because it
was looking in the wrong place, which is very likely why nobody noticed that
224 hours of collected ADS-B contained not one MLAT row.

So: accept either key, and if NEITHER is present, say so and exit non-zero.
A probe that cannot tell "nothing there" from "I looked in the wrong place"
is worse than no probe, because it produces a confident negative.

`type` is the position source -- adsb_icao, adsr_icao, tisb_icao, mlat and
so on. Do not confuse it with `t`, which is the airframe type code (B738).
The `mlat` and `tisb` keys are separate: arrays naming the FIELDS on an
otherwise-cooperative aircraft that came from those sources, which is a
weaker thing than an independently-observed target.

Source: adsb.fi open data (https://adsb.fi), public endpoint, 1 request per
second, personal non-commercial use.
"""

from __future__ import annotations

import argparse
import collections
import sys

import httpx

API = "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{dist}"

# `type` values that mean the position was derived, not reported.
INDEPENDENT_PREFIXES = ("tisb", "adsr")
INDEPENDENT_EXACT = ("mlat",)


def is_independent(kind: str) -> bool:
    return kind.startswith(INDEPENDENT_PREFIXES) or kind in INDEPENDENT_EXACT


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--lat", type=float, default=38.95)
    ap.add_argument("--lon", type=float, default=-77.0)
    ap.add_argument("--dist", type=int, default=70, help="nautical miles")
    args = ap.parse_args()

    url = API.format(lat=args.lat, lon=args.lon, dist=args.dist)
    doc = httpx.get(url, timeout=30).raise_for_status().json()

    for key in ("aircraft", "ac"):
        if key in doc:
            craft, used = doc[key] or [], key
            break
    else:
        print(f"\n  NO AIRCRAFT LIST in the response. Keys were: "
              f"{sorted(doc)}\n")
        print("  This is not an empty sky. The feed's shape has changed, or "
              "the URL is\n  wrong. Refusing to report zero.\n")
        return 2

    print(f"\n  {len(craft)} aircraft within {args.dist} nm of "
          f"{args.lat}, {args.lon}   (key: {used!r})\n")
    if not craft:
        print("  The list is genuinely empty.\n")
        return 0

    counts = collections.Counter(a.get("type", "?") for a in craft)
    indep = sum(n for k, n in counts.items() if is_independent(k))
    for kind, n in counts.most_common():
        tag = "INDEPENDENT" if is_independent(kind) else "cooperative"
        print(f"    {n:>4}  {kind:<16} {tag}")

    # A field sourced from MLAT/TIS-B on an otherwise cooperative aircraft is
    # not an independent target, and counting it as one would overstate the
    # channel. Reported separately.
    partial = sum(1 for a in craft if a.get("mlat") or a.get("tisb"))
    print(f"\n    {indep} of {len(craft)} independently positioned "
          f"({indep / len(craft):.1%})")
    print(f"    {partial} more carry at least one MLAT- or TIS-B-sourced "
          f"field on an otherwise cooperative track")
    if indep == 0:
        print("\n  No independent targets in this snapshot. One snapshot is "
              "not a rate --\n  run the collector if you need to know how "
              "often that is true.\n")
    else:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
