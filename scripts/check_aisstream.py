"""Confirm the live AIS stream works, and MEASURE ITS WARM-UP.

    python scripts/check_aisstream.py              # listen 120 s over AOI_SEA
    python scripts/check_aisstream.py --seconds 300
    python scripts/check_aisstream.py --bbox region

RUN THIS BEFORE TRUSTING THE SEA LAYER.

Two separate questions, and the second is the one nobody thinks to ask.

    1. Does the key work and is the bounding box right? A wrong key closes
       the socket. A bounding box with latitude and longitude the wrong way
       round does NOT fail -- it subscribes to a patch of the Indian Ocean
       and delivers a perfectly healthy, permanently empty stream, which
       looks exactly like a quiet sea.

    2. HOW LONG MUST IT LISTEN BEFORE THE TABLE MEANS ANYTHING? A Class A
       vessel under way transmits every 2-10 s, but one at anchor only every
       3 minutes, and Class B every 30 s to 3 minutes. So the vessel count
       climbs for minutes after connecting, and a count taken too early is
       not a small underestimate -- it systematically omits the moored and
       anchored traffic, which is most of a harbour and most of what any
       dark-vessel question is about.

       The first version of aisstream.py set a 120 s constant on that
       reasoning. This script measured the curve and found it was still a
       straight line at 120 s; on a ten-minute run of the real box the
       discovery rate peaked at 143 new vessels/min around t=100 s and fell
       below 30% of that only at ~400 s. So the decision now lives in the
       stream itself, as a threshold on the measured discovery rate, and
       this script is how you check the threshold against a new box, hour
       or season.

WHAT IT DELIBERATELY DOES NOT DO

It does not print the API key, or any part of it. It reports length only.
This output is exactly the sort of thing that gets pasted into a chat.

It also does not call the final number "the vessels in the study area". It is
the vessels that TRANSMITTED during the listening window and were received by
a shore station. Both of those are conditions, and neither is "present".
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections import Counter

try:
    import _bootstrap  # noqa: F401  (must precede the angels imports)
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.adapters.maritime import aisstream
from angels.config import AOI_SEA, REGION


async def listen(bbox, seconds: float, quiet: bool) -> int:
    key = os.getenv("AISSTREAM_API_KEY", "")
    if not key:
        print("\n  AISSTREAM_API_KEY is not set.")
        print("  Register free at https://aisstream.io -- email only, no")
        print("  review queue -- and put the key in .env.\n")
        return 2
    print(f"\n  key          present, {len(key)} chars")

    w, s, e, n = bbox
    print(f"  bbox         {w} {s} -> {e} {n}")
    print(f"  endpoint     {aisstream.ENDPOINT}")
    try:
        import websockets
        print(f"  websockets   {websockets.__version__}")
    except ImportError:
        print("\n  The 'websockets' package is missing. It ships with")
        print("  uvicorn[standard]; install it with:")
        print("      pip install websockets\n")
        return 2

    stream = aisstream.Stream(bbox, api_key=key)
    await stream.start()

    print(f"\n  listening for {seconds:.0f} s. Vessel counts as they arrive:\n")
    print(f"    {'t':>6}{'msgs':>9}{'vessels':>9}{'typed':>7}{'new':>6}"
          f"{'new/min':>9}")

    t0 = time.time()
    curve: list[tuple[float, int]] = []
    last = 0
    step = max(5.0, seconds / 24)
    next_at = step

    while time.time() - t0 < seconds:
        await asyncio.sleep(0.25)
        el = time.time() - t0
        if el < next_at:
            continue
        next_at += step
        snap = stream.snapshot()
        n_v = len(snap.features)
        curve.append((el, n_v))
        flag = "" if not snap.warming else "  warming"
        print(f"    {el:5.0f}s{snap.n_messages:9,}{n_v:9,}{snap.n_typed:7,}"
              f"{n_v - last:6,}{snap.discovery_per_min:9.0f}{flag}")
        last = n_v
        if snap.error and not quiet:
            print(f"           stream error: {snap.error}")

    snap = stream.snapshot()
    await stream.stop()

    print(f"\n  {snap.n_messages:,} messages, {len(snap.features):,} distinct "
          f"vessels, in {seconds:.0f} s")
    if not snap.features:
        print("\n  NOTHING RECEIVED. Three different faults look identical")
        print("  from here, and only one of them is about the sea:")
        print("    - the key is wrong or expired    -> the socket would also")
        print("      have closed; check for an error line above")
        print("    - the bounding box is inverted   -> a healthy socket over")
        print("      empty water. Check the numbers printed above are the")
        print("      ones you meant.")
        print("    - the service is down            -> no SLA is offered")
        print("  It is NOT evidence that the study area is empty.\n")
        return 1

    # -- is the warm-up constant right? ------------------------------------
    #
    # Compare how fast vessels were still arriving in the last quarter of the
    # window against the first. A stream that has converged is finding almost
    # nothing new; one still climbing has not heard the anchored traffic.
    print()
    print(f"  discovery     {snap.discovery_per_min:.0f}/min now, "
          f"peak {snap.peak_discovery_per_min:.0f}/min")
    if snap.warming:
        frac = (snap.discovery_per_min / snap.peak_discovery_per_min
                if snap.peak_discovery_per_min else 1.0)
        print(f"\n  STILL CLIMBING at {curve[-1][0]:.0f} s -- new vessels are "
              f"arriving at {100 * frac:.0f}% of")
        print(f"  the peak rate, and the stream calls itself settled below "
              f"{100 * aisstream.SETTLED_FRACTION:.0f}%.")
        print("  The table is a fraction of the resident population, and a")
        print("  count taken now omits the SLOW reporters -- the anchored and")
        print("  moored vessels, which is most of a harbour and most of what")
        print("  any dark-vessel question is about.")
        print(f"\n  Re-run with --seconds {max(600, int(seconds * 5))} to find "
              f"where it flattens.")
    else:
        print(f"\n  SETTLED. Arrivals have decayed below "
              f"{100 * aisstream.SETTLED_FRACTION:.0f}% of peak, so the")
        print("  table is worth reading and the front end will say so.")

    # -- what is out there -------------------------------------------------
    counts = Counter(snap.by_subtype)
    print("\n  by type:")
    for name, n in counts.most_common():
        print(f"    {name:>12}  {n:4,}")

    # WHAT "UNKNOWN" IS MADE OF. Twice this block has reported a flattering
    # number: first counting names (which aisstream supplies free from its own
    # database) as static data, then counting any static message as a type,
    # when a Class B vessel's first half-message carries only its name. The
    # three rows below cannot be conflated, and the message counts underneath
    # say whether a large 'unknown' is the sea or the parser.
    total = len(snap.features)
    typed = snap.n_typed
    heard = snap.n_heard_static
    classb = sum(1 for f in snap.features
                 if f["properties"]["ais_class"] == "B")
    parts = snap.static_parts or {}
    print(f"\n  of {total:,} vessels:")
    print(f"    {typed:5,}  declared a type")
    print(f"    {heard - typed:5,}  sent static data but no type "
          f"(name-only half, or type 0)")
    print(f"    {total - heard:5,}  sent no static data at all this window")
    print(f"  {classb:,} were Class B")

    print("\n  static messages received:")
    print(f"    {parts.get('msg5', 0):6,}  Class A, message 5 (name + type)")
    print(f"    {parts.get('A', 0):6,}  Class B, message 24 part A (name only)")
    print(f"    {parts.get('B', 0):6,}  Class B, message 24 part B (type)")
    if parts.get("neither"):
        print(f"    {parts['neither']:6,}  message 24 with NEITHER half valid")

    if parts.get("A", 0) and not parts.get("B", 0):
        print("\n  Part A arriving and part B never: either every Class B set")
        print("  in range skips part B, which is implausible, or the parser")
        print("  is not reading it. Treat 'unknown' as a parser suspect.")
    elif parts.get("B", 0):
        print("\n  Both halves are arriving, so a large 'unknown' is mostly")
        print("  vessels that have not yet sent part B in this window, or")
        print("  sets left at the factory default of type 0 -- the sea, not")
        print("  the parser.")

    print("\n  These are the vessels that TRANSMITTED during the window and")
    print("  were received by a shore station. Not the vessels present. The")
    print("  difference between those two is the entire project.\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seconds", type=float, default=120.0,
                    help="how long to listen (default 120, = WARMUP_S)")
    ap.add_argument("--bbox", choices=["sea", "region"], default="sea")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()
    bbox = AOI_SEA if args.bbox == "sea" else REGION
    return asyncio.run(listen(bbox, args.seconds, args.quiet))


if __name__ == "__main__":
    sys.exit(main())
