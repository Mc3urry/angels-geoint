"""How much Sentinel-1 radar is there over our water, and how often?

RUN THIS BEFORE WRITING ANY SAR CODE.

The aviation observation channel was chased through six sources before it
turned out not to exist for civilians. Every one of those was discovered after
writing code against it. The cheap lesson: establish that a channel is real,
and measure how much of it there is, BEFORE building an adapter on top.

So this asks the European Space Agency's catalogue three questions:

    1. How many Sentinel-1 scenes cover AOI_SEA?
    2. How far apart are they -- what is the real revisit interval here?
    3. Are they the right product? Ship detection needs GRD (ground range
       detected, already geocoded). SLC is raw complex data and is a much
       larger job for no benefit here.

The answer decides the sample size of the entire maritime experiment. Thirty
scenes over nine months is enough to do statistics on. Four is a case study.

WHY SAR IS THE RIGHT OBSERVATION CHANNEL

A radar satellite illuminates the sea and measures what comes back. Water is
flat and scatters the pulse away, so it returns almost nothing and images as
near-black. A steel hull is a corner reflector and throws the pulse straight
back, so it images as a bright point. This works through cloud, and at night,
because the satellite supplies its own illumination.

Crucially, none of it requires the vessel's cooperation. A ship with its AIS
transponder switched off is exactly as bright as one broadcasting normally.
That is what makes it an OBSERVATION in this project's strict sense, and why
the comparison against AIS is the whole experiment:

    bright spot + matching AIS track   ->  a vessel behaving normally
    bright spot + NO AIS track         ->  a dark vessel. The finding.
    AIS track   + no bright spot       ->  a spoof, or a boat too small
                                           or too wooden to reflect

THE HARD PART, WHICH THE ARCHITECTURE ALREADY ANTICIPATED

A scene is one instant. The satellite passes over at, say, 23:14:07 and that
is the only moment it saw. AIS is a continuous dribble of reports at varying
intervals. Comparing them means asking "where does AIS say this vessel was at
23:14:07 exactly", which is interpolation between two reports -- precisely
what core.models.Track.position_at() does, written in Phase 1 and predicted in
docs/core-changelog.md to be needed for exactly this.

    python scripts/check_sar_coverage.py                 # last 12 months
    python scripts/check_sar_coverage.py --months 24
    python scripts/check_sar_coverage.py --start 2024-09-09 --end 2025-06-01

No credentials needed. Searching the catalogue is anonymous; only DOWNLOADING
a scene needs a free Copernicus account, and that comes later.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter
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

# Third-party imports come AFTER the bootstrap, never before: _bootstrap
# re-executes this script under the project interpreter, and an import
# placed above it runs first -- under whatever Python the user typed --
# and dies with ModuleNotFoundError before the switch can happen.
import httpx

from angels.adapters.maritime.cdse import (
    ODATA, footprint_box, overlap_fraction, wkt,
)
from angels.config import AOI_SEA, REGION_NAME



def search(box, t0: datetime, t1: datetime, *, page: int = 1000) -> list[dict]:
    """Raw catalogue records, ALL product types.

    Deliberately not cdse.search(), which returns GRD Scenes only. This script
    reports the full type breakdown, so it needs the unfiltered response.
    """
    flt = (
        f"Collection/Name eq 'SENTINEL-1' and "
        f"OData.CSC.Intersects(area=geography'SRID=4326;{wkt(box)}') and "
        f"ContentDate/Start gt {t0:%Y-%m-%dT%H:%M:%S}.000Z and "
        f"ContentDate/Start lt {t1:%Y-%m-%dT%H:%M:%S}.000Z"
    )
    out: list[dict] = []
    skip = 0
    with httpx.Client(timeout=90.0, follow_redirects=True) as client:
        while True:
            r = client.get(ODATA, params={
                "$filter": flt, "$top": page, "$skip": skip,
                "$orderby": "ContentDate/Start asc",
            })
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            batch = r.json().get("value", [])
            out += batch
            if len(batch) < page or skip > 20000:
                return out
            skip += page


def product_type(name: str) -> str:
    """GRD / SLC / OCN / RAW, read from the filename.

    S1A_IW_GRDH_1SDV_20240909T231407_... -- the third underscore-separated
    field. Reading it from the name rather than requesting Attributes keeps
    the query small and fast.
    """
    parts = name.split("_")
    return parts[2][:3] if len(parts) > 2 else "?"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--months", type=float, default=12.0)
    ap.add_argument("--start", type=str, help="YYYY-MM-DD, overrides --months")
    ap.add_argument("--end", type=str, help="YYYY-MM-DD")
    args = ap.parse_args()

    t1 = (datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
          if args.end else datetime.now(timezone.utc))
    t0 = (datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
          if args.start else t1 - timedelta(days=30.44 * args.months))

    days = (t1 - t0).total_seconds() / 86400

    print(f"\n  Sentinel-1 over {REGION_NAME} (sea box)")
    print(f"  {AOI_SEA}")
    print(f"  {t0:%Y-%m-%d} to {t1:%Y-%m-%d}  ({days:.0f} days)\n")
    print("  querying the Copernicus catalogue...")

    try:
        products = search(AOI_SEA, t0, t1)
    except Exception as exc:
        print(f"\n  SEARCH FAILED: {exc}\n")
        print("  This does NOT mean there is no coverage -- it means the")
        print("  catalogue could not be reached. A blocked network, a proxy,")
        print("  or an ESA outage all look like this. Try again before")
        print("  concluding anything.\n")
        return 2

    if not products:
        print("\n  No Sentinel-1 products found over this box. That would be")
        print("  extraordinary for a populated coast -- check the box and the")
        print("  dates before believing it.\n")
        return 1

    by_type: Counter = Counter()
    grd: list[tuple[datetime, str, str, float]] = []
    seen: set[str] = set()
    dupes = 0

    for p in products:
        name = p.get("Name", "")
        ptype = product_type(name)
        by_type[ptype] += 1
        if ptype != "GRD":
            continue

        # DEDUPE BY NAME. The catalogue holds several records for one
        # acquisition -- different processing baselines, online and archived
        # copies. They are the same photograph of the same water at the same
        # second, and counting them separately would inflate the sample size
        # of the experiment by roughly threefold.
        if name in seen:
            dupes += 1
            continue
        seen.add(name)

        try:
            t = datetime.fromisoformat(
                p["ContentDate"]["Start"].replace("Z", "+00:00"))
        except Exception:
            continue

        fp = footprint_box(p)
        cov = overlap_fraction(fp, AOI_SEA) if fp else 0.0
        grd.append((t, name, p.get("Id", ""), cov))

    grd.sort()

    print(f"\n  {len(products):,} products total\n")
    print(f"  {'type':<8}{'count':>8}   what it is")
    print("  " + "-" * 64)
    MEAN = {
        "GRD": "ground range detected, geocoded  <-- WHAT YOU WANT",
        "SLC": "single look complex, raw phase. Bigger job, no benefit here",
        "OCN": "ocean wind/wave products, already processed",
        "RAW": "unprocessed downlink",
        "ETA": "ETAD timing corrections -- metadata, not imagery",
    }
    for t, n in by_type.most_common():
        print(f"  {t:<8}{n:>8}   {MEAN.get(t, '?')}")

    if not grd:
        print("\n  No GRD products. Ship detection would mean processing SLC,")
        print("  which is a substantially larger job. Reconsider the box.\n")
        return 1

    # -- revisit ----------------------------------------------------------

    MIN_COVER = 0.15          # a scene worth downloading
    good = [g for g in grd if g[3] >= MIN_COVER]

    times = [t for t, _, _, _ in grd]
    gaps = [(b - a).total_seconds() / 86400
            for a, b in zip(times, times[1:]) if (b - a).total_seconds() > 3600]

    print("\n  " + "=" * 64)
    print(f"  GRD ACQUISITIONS: {len(grd)}")
    print("  " + "=" * 64)
    if dupes:
        print(f"    ({dupes} duplicate catalogue records dropped -- the same")
        print(f"     acquisition stored under several processing baselines)")
    print(f"    first        {times[0]:%Y-%m-%d %H:%M} UTC")
    print(f"    last         {times[-1]:%Y-%m-%d %H:%M} UTC")
    print(f"    rate         {len(grd) / days * 30.44:.1f} per month")
    if gaps:
        print(f"    revisit      {statistics.median(gaps):.1f} days median, "
              f"{min(gaps):.1f} best, {max(gaps):.1f} worst")
    sats = Counter(n[:3] for _, n, _, _ in grd)
    print(f"    satellites   {', '.join(f'{k} x{v}' for k, v in sats.most_common())}")

    # Coverage. "Intersects the box" is a very low bar -- a scene clipping one
    # corner counts as a hit while showing you almost none of the Chesapeake.
    # The number that matters is how many scenes actually SEE the study area.
    print()
    print(f"    coverage of the study box (upper bound, bbox approximation):")
    buckets = [(0.80, "80-100%"), (0.50, "50-80%"), (0.30, "30-50%"),
               (0.15, "15-30%"), (0.0, "under 15%")]
    for i, (lo, label) in enumerate(buckets):
        hi = buckets[i - 1][0] if i else 1.01
        n_b = sum(1 for g in grd if lo <= g[3] < hi)
        if n_b:
            print(f"      {label:<10}{n_b:>6} scenes")

    covs = [g[3] for g in grd]
    print(f"      median {100 * statistics.median(covs):.0f}% of the box per scene")
    print()
    print(f"    USABLE (>={int(MIN_COVER * 100)}% of the box): {len(good)}")

    if good:
        gt = [t for t, _, _, _ in good]
        ggaps = [(b - a).total_seconds() / 86400
                 for a, b in zip(gt, gt[1:]) if (b - a).total_seconds() > 3600]
        if ggaps:
            print(f"    effective revisit on those: "
                  f"{statistics.median(ggaps):.1f} days median")

    # -- verdict ----------------------------------------------------------

    print("\n  " + "=" * 64)
    print("  VERDICT")
    print("  " + "=" * 64 + "\n")

    n = len(good)
    if n >= 30:
        print(f"  THE OBSERVATION CHANNEL IS REAL AND LARGE ENOUGH.")
        print(f"  {n} usable looks at the Chesapeake, free, already")
        print("  acquired, requiring nobody's permission.\n")
        print("  That is a sample you can do statistics on, not a case study.")
        print("  Each scene is one instant of ground truth about every vessel")
        print("  in the box, whether or not it was broadcasting.\n")
        print("  Next: a free Copernicus account (registration only, no")
        print("  review queue) to download, then adapters/maritime/sentinel1.py")
        print("  implements observations() against these scenes.\n")
        rc = 0
    elif n >= 8:
        print(f"  USABLE, BUT THIN: {n} scenes.")
        print("  Enough to build and validate the pipeline, not enough for a")
        print("  strong statistical claim. Widen the window with --months 24,")
        print("  or widen AOI_SEA seaward -- more open water per scene, and")
        print("  the interesting jurisdictional limits are out there anyway.\n")
        rc = 0
    else:
        print(f"  TOO FEW: {n} scenes.")
        print("  Widen the window and the box before committing to this.\n")
        rc = 1

    print("  Most recent usable scenes:\n")
    for t, name, _, cov in (good or grd)[-5:]:
        print(f"    {t:%Y-%m-%d %H:%M}  {100 * cov:>3.0f}% box  {name[:52]}")
    print()

    return rc


if __name__ == "__main__":
    sys.exit(main())
