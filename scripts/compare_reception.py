"""Compare two reception grids: what one receiver network hears that another does not.

    python scripts/compare_reception.py                       # live vs bulk
    python scripts/compare_reception.py --a reception.json \
                                        --b reception-maritime-live.json

WHY THIS EXISTS

`ais_coverage.py` measures how attentive a feed is, cell by cell, from the
feed's own reporting cadence. Run it twice -- once on MarineCadastre's bulk
archive, once on what this project's collectors hear from aisstream -- and
the two grids are not interchangeable. They are different receiver networks
looking at the same water.

That matters because the searched-water denominator is the thing that
licenses every dark-vessel claim in this project, and it is a property of
THE RECEIVERS, not of the water. A real-time claim cannot borrow the
retrospective denominator unless the two feeds are shown to hear alike.

THE COMPARISON THAT IS FAIR, AND THE ONE THAT IS NOT

A straight grid-against-grid diff is worthless while one feed has been
collecting for a day and the other covers twelve. `thin` in this project
means the cell held fewer than `min_vessels` vessels -- UNDER-SAMPLED, not
badly received -- and a young archive is thin nearly everywhere. Compared
raw, on 2026-09-24, a 25-hour live archive scored 41% class agreement with
the bulk one and appeared to be blind 250 km short of it. Both numbers were
about elapsed time.

So this restricts to cells where BOTH grids cleared their own min_vessels
bar, and reports two things over that common, adequately-sampled ground:

    class agreement        do they call the same water heard?
    median gap ratio       how much less often does B hear a vessel than A?

The second is the one that travels. It is per-cell, so it does not care that
the two archives cover different spans or different years, and it is a
direct statement about receiver density.

THE EDGE

Also printed: the furthest `heard` cell per latitude band, for both grids.
This one IS duration-sensitive and stays untrustworthy until the younger
archive has roughly a week behind it -- the script says so rather than
letting the table be read straight.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics as st
import sys
from collections import Counter
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import INTERIM

COVERAGE = INTERIM / "coverage"


def load(name: str) -> dict:
    p = COVERAGE / name
    if not p.exists():
        raise SystemExit(f"\n  No grid at {p}.\n  Run: python scripts/"
                         f"ais_coverage.py --store <store>\n")
    return json.loads(p.read_text(encoding="utf-8"))


def cell_km2(row: int, cell_deg: float) -> float:
    lat = (row + 0.5) * cell_deg
    return (cell_deg * 111.32) * (cell_deg * 111.32 * math.cos(math.radians(lat)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--a", default="reception.json",
                    help="the reference grid (default: the bulk archive)")
    ap.add_argument("--b", default="reception-maritime-live.json",
                    help="the grid being compared against it")
    args = ap.parse_args()

    A, B = load(args.a), load(args.b)
    if A["cell_deg"] != B["cell_deg"]:
        raise SystemExit(f"\n  Different cell sizes ({A['cell_deg']} vs "
                         f"{B['cell_deg']}); the grids do not line up.\n")
    cd = A["cell_deg"]
    a, b = A["cells"], B["cells"]
    min_a = A.get("min_vessels", 3)
    min_b = B.get("min_vessels", 3)

    print(f"\n  A  {args.a:38} {len(a):7,} cells")
    print(f"  B  {args.b:38} {len(b):7,} cells")

    common = set(a) & set(b)
    well = [k for k in common
            if a[k]["n_vessels"] >= min_a and b[k]["n_vessels"] >= min_b]
    print(f"\n  cells in both                    {len(common):7,}")
    print(f"  adequately sampled in both       {len(well):7,}"
          f"   (>= {min_a}/{min_b} vessels)")
    thin_b = sum(1 for c in b.values() if c["n_vessels"] < min_b)
    print(f"  B cells under its own bar        {thin_b:7,} of {len(b):,}"
          f"   -- under-sampled, NOT unheard")

    if not well:
        print("\n  Nothing sampled well enough in both to compare. Let the "
              "younger\n  archive run longer.\n")
        return 0

    # -- do they call the same water heard --------------------------------
    m = Counter((a[k]["reception"], b[k]["reception"]) for k in well)
    agree = sum(n for (x, y), n in m.items() if x == y)
    print(f"\n  class agreement over that ground  {agree}/{len(well)} "
          f"= {100 * agree / len(well):.0f}%")
    print(f"    {'A':>13} -> {'B':13}{'cells':>7}")
    for (x, y), n in m.most_common():
        mark = "" if x == y else "   differs"
        print(f"    {x:>13} -> {y:13}{n:7,}{mark}")

    # -- how much less often does B hear a vessel --------------------------
    ga = [a[k]["median_gap_s"] for k in well]
    gb = [b[k]["median_gap_s"] for k in well]
    ratio = [b[k]["median_gap_s"] / a[k]["median_gap_s"]
             for k in well if a[k]["median_gap_s"] > 0]
    print(f"\n  median inter-report gap over the same cells")
    print(f"    A   {st.median(ga):6.0f}s")
    print(f"    B   {st.median(gb):6.0f}s")
    if ratio:
        print(f"    per-cell B/A ratio, median {st.median(ratio):.2f}"
              f"   ({len(ratio)} cells)")
        print(f"\n  B hears a given vessel about {st.median(ratio):.2f}x less "
              f"often than A in the same water.")

    # -- the edge, with its own warning ------------------------------------
    print(f"\n  furthest HEARD cell per latitude band  (degrees longitude)")
    print(f"    {'band':>13}{'A':>10}{'B':>10}")
    rows = [int(k.split(",")[1]) for k in set(a) | set(b)]
    lo = math.floor(min(rows) * cd * 2) / 2
    hi = math.ceil((max(rows) + 1) * cd * 2) / 2
    y = lo
    while y < hi:
        edges = {}
        for tag, d in (("A", a), ("B", b)):
            cols = [int(k.split(",")[0]) for k, c in d.items()
                    if c["reception"] == "heard"
                    and y <= (int(k.split(",")[1]) + 0.5) * cd < y + 0.5]
            edges[tag] = max(cols) * cd if cols else None
        if edges["A"] is not None or edges["B"] is not None:
            f = lambda v: f"{v:10.2f}" if v is not None else f"{'-':>10}"
            print(f"    {y:5.1f}-{y + 0.5:4.1f}{f(edges['A'])}{f(edges['B'])}")
        y += 0.5
    print("\n  The edge is duration-sensitive. Until the younger archive has "
          "about a\n  week behind it, a short B edge means it has not been "
          "there yet.\n")

    heard_b = sum(cell_km2(int(k.split(",")[1]), cd)
                  for k, c in b.items() if c["reception"] == "heard")
    print(f"  B heard area: {heard_b:,.0f} km2\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
