"""List and download Sentinel-1 scenes over the sea box.

    python scripts/fetch_sar.py --list                  # what is available
    python scripts/fetch_sar.py --download 1            # ONE scene, first
    python scripts/fetch_sar.py --list --from 2024-04-01 --to 2024-12-31
    python scripts/fetch_sar.py --check                 # credentials only

THE WINDOW DEFAULTS TO THE OBSERVATION PERIOD, NOT TO "RECENTLY"

AIS is published 624 days behind, a year at a time, so the newest and most
attractive passes are precisely the ones nothing can be compared against.
--from and --to default to config.OBSERVE_START and the measured AIS frontier;
a window past the frontier is refused unless you pass --unmatchable and mean
it.

START WITH ONE.

A GRD product is roughly a gigabyte. Before queueing fifty of them, fetch a
single scene and find out three things you cannot learn any other way: whether
the credentials work, what your actual throughput is, and therefore what fifty
scenes will cost in wall-clock time and disk. Guessing those and starting a
fifty-scene run is how you discover at 2am that the password was wrong.

    --check     authenticates and stops. Two seconds.
    --download 1  one scene end to end.

SELECTION

Scenes are ranked by how much of AOI_SEA they cover, then by recency, and only
those above --min-coverage are offered. "Intersects the box" is a very low bar
-- a scene clipping one corner of the Atlantic is a catalogue hit and shows you
almost none of the Chesapeake. Downloading a gigabyte for that is wasted disk
and a wasted hour.

Files land in data/raw/sar/, which is gitignored.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

# Re-runs this script under the interpreter that has ANGELS installed, if the
# one invoking it does not. See scripts/_bootstrap.py.
#
# The name check matters. `import _bootstrap` only resolves when scripts/ is on
# sys.path, which is true when this file is RUN and false when the test suite
# IMPORTS it as scripts.<name>. Swallowing every ModuleNotFoundError here would
# also swallow the one _bootstrap raises about 'angels' itself -- turning a
# clear "wrong interpreter" message back into a confusing one.
try:
    import _bootstrap  # noqa: F401  (must precede the angels imports)
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.adapters.maritime.cdse import (
    MissingCredentials, TokenManager, download, group_passes, search,
)
from angels.config import (
    AIS_FRONTIER, AOI_SEA, OBSERVE_END, OBSERVE_START, RAW, REGION_NAME,
)

DEST = RAW / "sar"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--list", action="store_true", help="show and stop")
    ap.add_argument("--check", action="store_true",
                    help="verify credentials and stop")
    ap.add_argument("--download", type=int, metavar="N",
                    help="download the best N passes (all slices of each)")
    # The window defaults to the OBSERVATION PERIOD, not to "recently".
    #
    # Searching back from today is the obvious thing and it is wrong here: AIS
    # for the last 624 days does not exist, so the most attractive passes --
    # newest, cleanest, easiest to download -- are exactly the ones nothing
    # can be concluded from. Defaulting to the matchable window makes the
    # right choice the lazy one.
    ap.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD",
                    default=OBSERVE_START,
                    help=f"window start (default {OBSERVE_START})")
    ap.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD",
                    default=OBSERVE_END,
                    help=f"window end (default {OBSERVE_END}, the measured "
                         f"AIS frontier)")
    ap.add_argument("--months", type=float,
                    help="instead of --from, this many months back from --to")
    ap.add_argument("--unmatchable", action="store_true",
                    help="allow a window past the AIS frontier. The scenes "
                         "are real; nothing can be compared to them yet.")
    ap.add_argument("--min-coverage", type=float, default=0.30,
                    help="fraction of the sea box a PASS must cover (0.30). "
                         "A single slice can never exceed ~20%% of AOI_SEA, "
                         "so this is a threshold on the overflight, not the "
                         "scene.")
    ap.add_argument("--slices", action="store_true",
                    help="list individual slices instead of passes")
    ap.add_argument("--newest-first", action="store_true",
                    help="prefer recent passes over well-placed ones")
    ap.add_argument("--rank", choices=["limits", "coverage"], default="limits",
                    help="what makes a pass worth a gigabyte. 'limits' (the "
                         "default) prefers passes that actually CONTAIN a "
                         "jurisdictional boundary -- the independent variable "
                         "of the experiment. 'coverage' prefers area, which "
                         "over this box means open Atlantic.")
    ap.add_argument("--min-limits", type=int, default=0,
                    help="skip passes crossing fewer than this many limits")
    args = ap.parse_args()

    # -- credentials ------------------------------------------------------

    if args.check or args.download:
        tokens = TokenManager()
        try:
            tok = tokens.token()
        except MissingCredentials as exc:
            print(f"\n  {exc}\n")
            return 2
        except Exception as exc:
            print(f"\n  {exc}\n")
            return 2
        print(f"\n  credentials OK ({len(tok)} char token, "
              f"user {tokens.username})")
        if args.check:
            print()
            return 0

    # -- search -----------------------------------------------------------

    def _day(s: str) -> datetime:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    try:
        t1 = _day(args.date_to)
        t0 = (t1 - timedelta(days=int(args.months * 30.44)) if args.months
              else _day(args.date_from))
    except ValueError as exc:
        print(f"\n  bad date: {exc}\n")
        return 2

    if t0 >= t1:
        print(f"\n  empty window: {t0:%Y-%m-%d} is not before {t1:%Y-%m-%d}\n")
        return 2

    frontier = _day(AIS_FRONTIER)
    if t1 > frontier and not args.unmatchable:
        print(f"\n  The window ends {t1:%Y-%m-%d}, past the AIS frontier of "
              f"{AIS_FRONTIER}.")
        print("  Scenes after that date can be detected in but not compared")
        print("  against anything, because their AIS has not been published.")
        print("  Re-measure with:  python scripts/check_ais_lag.py")
        print("  Or pass --unmatchable to collect the forward half anyway.\n")
        return 2

    print(f"\n  searching {REGION_NAME} sea box, "
          f"{t0:%Y-%m-%d} to {t1:%Y-%m-%d}...")
    if t1 <= frontier:
        print("  (inside the AIS window -- every pass here is matchable)")

    try:
        # No per-slice coverage floor: a slice that clips the box is still
        # part of a pass, and dropping it here would punch a hole in the
        # mosaic and understate that pass's coverage.
        scenes = search(AOI_SEA, t0, t1, product_type="GRD")
    except Exception as exc:
        print(f"\n  catalogue search failed: {exc}")
        print("  (a network or proxy problem, not an absence of scenes)\n")
        return 2

    if not scenes:
        print("\n  no GRD scenes over this box in that window at all.\n")
        return 1

    passes = group_passes(scenes, AOI_SEA, min_coverage=args.min_coverage)

    if not passes:
        best = max(group_passes(scenes, AOI_SEA), key=lambda p: p.coverage,
                   default=None)
        print(f"\n  {len(scenes)} slices found, but no PASS covers "
              f">={args.min_coverage:.0%} of the box.")
        if best:
            print(f"  The best overflight covers {best.coverage:.0%} "
                  f"({len(best.scenes)} slices, {best.t:%Y-%m-%d}).")
        print()
        print("  AOI_SEA is 545 x 398 km. A Sentinel-1 swath is 250 km wide,")
        print("  so even a full overflight only crosses part of it. Lower")
        print("  --min-coverage, or accept that the bay is bigger than one")
        print("  pass and analyse per-pass footprints.\n")
        return 1

    # Best first: coverage dominates, because a pass that sees more of the bay
    # is worth more per gigabyte than a recent one that clips the corner.
    if args.min_limits:
        passes = [p for p in passes if len(p.limits) >= args.min_limits]
        if not passes:
            print(f"\n  No pass contains {args.min_limits}+ limits. The inner")
            print("  3/12/24 nm cluster sits within about half a degree of the")
            print("  Virginia coast, so only passes reaching west of -75.9")
            print("  contain all three.\n")
            return 1

    if args.newest_first:
        passes.sort(key=lambda p: p.t, reverse=True)
    elif args.rank == "limits":
        # Boundaries first, then area. A pass with no limit in it answers no
        # question this project asks, however much water it covers.
        passes.sort(key=lambda p: (-len(p.limits), -round(p.coverage, 2),
                                   -p.t.timestamp()))
    else:
        passes.sort(key=lambda p: (-round(p.coverage, 2), -p.t.timestamp()))

    total_gb = sum(p.size_bytes for p in passes) / 1e9
    print(f"  {len(scenes)} slices -> {len(passes)} usable passes, "
          f"{total_gb:.1f} GB in total\n")

    # -- list -------------------------------------------------------------

    if args.list or not args.download:
        for p in passes[:30]:
            offline = sum(1 for s in p.scenes if not s.online)
            flag = f"   ({offline} ARCHIVED -- slow)" if offline else ""
            print(f"    {p}{flag}")
            if args.slices:
                for s in p.scenes:
                    print(f"        {s}")
        if len(passes) > 30:
            print(f"    ... and {len(passes) - 30} more")
        print()
        if not args.download:
            print("  Next: python scripts/fetch_sar.py --download 1")
            print("  One pass first. Measure the throughput before queueing "
                  "the rest.\n")
        return 0

    # -- download ---------------------------------------------------------

    picked = passes[:args.download]
    todo = [s for p in picked for s in p.scenes]
    gb = sum(s.size_bytes for s in todo) / 1e9
    print(f"  downloading {len(picked)} pass(es) = {len(todo)} slices, "
          f"{gb:.1f} GB -> {DEST}\n")
    for p in picked:
        print(f"    {p}")
    print()

    done, failed = [], []
    t_start = datetime.now(timezone.utc)

    for i, s in enumerate(todo, 1):
        print(f"  [{i}/{len(todo)}] {s.t:%Y-%m-%d %H:%M}  "
              f"{100 * s.coverage:.0f}% box")
        try:
            done.append(download(s, DEST, tokens))
        except KeyboardInterrupt:
            print("\n\n  interrupted. Partial files are kept as .part and")
            print("  will resume where they stopped on the next run.\n")
            return 130
        except Exception as exc:
            print(f"    FAILED: {exc}")
            failed.append((s.name, str(exc)))

    elapsed = (datetime.now(timezone.utc) - t_start).total_seconds()
    got_gb = sum(p.stat().st_size for p in done) / 1e9

    print(f"\n  {len(done)} downloaded, {len(failed)} failed, "
          f"{got_gb:.1f} GB in {elapsed / 60:.1f} min")
    if done and elapsed > 0:
        per = elapsed / len(done)
        print(f"  {per / 60:.1f} min/slice -- so 30 passes is about "
              f"{30 * len(todo) * per / 3600:.1f} hours and "
              f"{30 * len(todo) * got_gb / len(done):.0f} GB")
    for name, err in failed[:5]:
        print(f"    {name[:50]}: {err}")

    if done:
        print(f"\n  Next: ship detection against {done[0].name}\n")
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
