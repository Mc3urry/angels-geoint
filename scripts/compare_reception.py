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
    silence ratio          how much more of the time is B quiet than A?

The second and third are the ones that travel. They are per-cell, so they do
not care that the two archives cover different spans or different years, and
both are direct statements about receiver density. Prefer the THIRD: a median
describes the ordinary case, and a dark-vessel claim is never about the
ordinary case.

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

    # -- how often does B fall SILENT, which is the dark-vessel question ---
    #
    # THE MEDIAN IS THE WRONG STATISTIC FOR WHAT THIS PROJECT CLAIMS.
    #
    # A dark-vessel reading is a claim about silence: not "did this vessel
    # report a little less often than usual" but "did it go quiet long
    # enough to mean something, and would the reference network have gone
    # quiet too". The median answers only the first. The deficit between
    # these two feeds is in the TAIL and grows with the length of the
    # silence -- the figures, which date, are in the WORKLOG entry for
    # 2026-10-16 rather than hardcoded here where nothing would recompute
    # them.
    #
    # p_long is P(gap > LONG_GAP_S), and every grid has carried it since the
    # first one was written. Nothing ever printed it, so the shape of the
    # distribution sat on disk unread through four measurements while the
    # median was quoted as the headline.
    pa  = [a[k]["p_long"] for k in well if a[k].get("p_long") is not None]
    pb  = [b[k]["p_long"] for k in well if b[k].get("p_long") is not None]
    # CELLS WHERE A WAS NEVER SILENT ARE EXCLUDED FROM THE RATIO, AND SAID SO.
    #
    # A p_long of exactly 0 makes B/A infinite, so those cells cannot enter a
    # median. They are not missing data -- they are the cells where B looks
    # WORST, and dropping them silently would make the ratio look precise
    # while quietly discarding its most extreme evidence. Counted and
    # reported instead, so the printed ratio is visibly a lower bound.
    finite = [k for k in well
              if a[k].get("p_long") and b[k].get("p_long") is not None]
    nosil = [k for k in well
             if a[k].get("p_long") == 0 and b[k].get("p_long") is not None]
    nosil_b_worse = sum(1 for k in nosil if b[k]["p_long"] > 0)
    plr = [b[k]["p_long"] / a[k]["p_long"] for k in finite]
    if pa and pb:
        worse = sum(1 for k in well
                    if a[k].get("p_long") is not None
                    and b[k].get("p_long") is not None
                    and b[k]["p_long"] > a[k]["p_long"])
        cut = A.get("long_gap_s", 180)
        if B.get("long_gap_s", cut) != cut:
            print(f"    NOTE: grids disagree on long_gap_s "
                  f"({cut} vs {B.get('long_gap_s')}); not comparable")
        print(f"\n  P(gap > {cut:.0f}s) -- how much of the time each feed "
              f"is silent")
        print(f"    A   {st.median(pa):6.1%}")
        print(f"    B   {st.median(pb):6.1%}")
        if plr:
            print(f"    per-cell B/A ratio, median {st.median(plr):.2f}x"
                  f"   ({len(plr):,} of {len(well):,} cells)")
        if nosil:
            print(f"    excluded {len(nosil):,} cells where A was never "
                  f"silent; B was quieter in")
            print(f"      {nosil_b_worse:,} of them, so the ratio above is a "
                  f"LOWER BOUND")
        print(f"    B is the quieter feed in {worse:,} of {len(well):,} cells")
        print(f"\n  Read this before any dark-vessel rate, not the median. A"
              f" detector tuned\n  on A's silence distribution will fire "
              f"constantly on B.")

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
    # NOT duration any more. Measured 2026-10-16: between 429 h and 529 h
    # six of eight latitude bands did not move at all, so the edge has
    # stopped walking east and waiting will not change it. What blocks it is
    # that `heard` is not reliably decidable in B at the sample sizes most of
    # its cells have: drawing 3 vessels from a B cell with 20+ reproduces the
    # cell's own class only 80% of the time, against 97% in A. The same
    # min_vessels means a different thing on each side of this comparison.
    print("\n  The edge is NOT duration-sensitive any more and waiting will not "
          "fix it.\n  What blocks it is that `heard` is not reliably decidable "
          "in B at the\n  sample sizes most of its cells have -- 3 vessels "
          "recover a B cell's own\n  class 80% of the time, against 97% in A. "
          "Read the table as a lower\n  bound on B's reach, never as its "
          "reception envelope.\n")

    heard_b = sum(cell_km2(int(k.split(",")[1]), cd)
                  for k, c in b.items() if c["reception"] == "heard")
    print(f"  B heard area: {heard_b:,.0f} km2\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
