"""How much does the published detection floor throw away, and can we say what?

    python scripts/floor_audit.py
    python scripts/floor_audit.py --snr 15 --pixels 6

THE QUESTION THIS ANSWERS, AND THE ONE IT DOES NOT

`candidates-scored.geojson` is gated at SNR >= 15 and >= 6 pixels. Every
number in this project is computed from what survives that floor, and nothing
downstream can see what it removed -- the labels were drawn from the gated
set, so the 147 hand-read chips say nothing at all about the discarded
population.

FINDINGS has carried the line "whether the current floor is throwing away
real vessels below it is a question for the raw detection files" since
2026-09-25, and `tune_detector.py`'s docstring says its cost function cannot
be fixed "without re-running detection and matching over every raw scene".

That was wrong, or has become wrong. The raw detections are on disk:
`data/events/sar-*.geojson` holds every cluster the detector found at
k = 6 sigma, floor and all. This script measures the discarded population
directly.

WHAT IT CANNOT DO, STATED PLAINLY

It cannot tell you how many of them are vessels. There are no labels below
the floor and none can be had without drawing a sample, chipping it, and
having a person read it -- and the 2026-09-28 reader study puts a number on
what that costs: kappa 0.467 between readers, and a 26-point drop in one
reader's own agreement between two sittings.

So this reports a BOUND, not a correction: this many detections were
discarded, distributed like this. Anyone who wants the vessel fraction has to
go and measure it. A bound honestly stated is worth more than an estimate
extrapolated from bins the labels never covered.
"""

from __future__ import annotations

import argparse
import glob
import json
from collections import Counter

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import EVENTS  # noqa: E402

# The published floor. Read from the artefact where possible so this cannot
# drift from what actually gated the set -- the keep-fraction lesson.
DEFAULT_SNR, DEFAULT_PIXELS = 15.0, 6


def published_gate() -> tuple[float, int] | None:
    p = EVENTS / "boundary-bands.json"
    if not p.exists():
        return None
    g = json.loads(p.read_text(encoding="utf-8")).get("gate") or {}
    if "min_snr" in g and "min_pixels" in g:
        return float(g["min_snr"]), int(g["min_pixels"])
    return None


def raw_detections():
    for path in sorted(glob.glob(str(EVENTS / "sar-*.geojson"))):
        doc = json.loads(open(path, encoding="utf-8").read())
        for f in doc.get("features", []):
            p = f["properties"]
            if p.get("snr") is None or p.get("pixels") is None:
                continue
            yield float(p["snr"]), int(p["pixels"]), p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    pub = published_gate()
    ap.add_argument("--snr", type=float, default=pub[0] if pub else DEFAULT_SNR)
    ap.add_argument("--pixels", type=int,
                    default=pub[1] if pub else DEFAULT_PIXELS)
    args = ap.parse_args()

    rows = list(raw_detections())
    if not rows:
        print("\n  No sar-*.geojson on disk. Run detect_ships.py first.\n")
        return 1

    src = "boundary-bands.json" if pub else "this script's default"
    print(f"\n  floor: SNR >= {args.snr:g} and >= {args.pixels} px  (from {src})")
    print(f"  raw detections on disk: {len(rows):,}")

    kept = [r for r in rows if r[0] >= args.snr and r[1] >= args.pixels]
    cut = [r for r in rows if not (r[0] >= args.snr and r[1] >= args.pixels)]
    print(f"    kept by the floor      {len(kept):>7,}  "
          f"({100 * len(kept) / len(rows):4.1f}%)")
    print(f"    DISCARDED by the floor {len(cut):>7,}  "
          f"({100 * len(cut) / len(rows):4.1f}%)  <- unlabelled, unmeasured")

    only_snr = sum(1 for s, px, _ in cut if px >= args.pixels)
    only_px = sum(1 for s, px, _ in cut if s >= args.snr)
    both = len(cut) - only_snr - only_px
    print(f"\n  why each was discarded")
    print(f"    SNR alone      {only_snr:>7,}")
    print(f"    pixels alone   {only_px:>7,}")
    print(f"    both           {both:>7,}")

    print(f"\n  the discarded population, by size")
    bins = [(2, 3), (3, 4), (4, 6), (6, 8), (8, 12), (12, 10 ** 9)]
    for lo, hi in bins:
        n = sum(1 for _s, px, _ in cut if lo <= px < hi)
        if n:
            lab = f"{lo}-{hi}" if hi < 10 ** 9 else f"{lo}+"
            bar = "#" * max(1, round(40 * n / len(cut)))
            print(f"    {lab:>7} px  {n:>7,}  {bar}")

    print(f"\n  and by strength")
    sb = [(6, 8), (8, 10), (10, 12), (12, 15), (15, 10 ** 9)]
    for lo, hi in sb:
        n = sum(1 for s, _px, _ in cut if lo <= s < hi)
        if n:
            lab = f"{lo}-{hi}" if hi < 10 ** 9 else f"{lo}+"
            bar = "#" * max(1, round(40 * n / len(cut)))
            print(f"    {lab:>7} SNR {n:>7,}  {bar}")

    print(f"""
  WHAT THIS DOES AND DOES NOT SAY

  It says {len(cut):,} detections were discarded before any published number
  was computed. That is the bound on what the floor could be hiding.

  It does NOT say how many are vessels. No chip below the floor has ever been
  read, so the vessel fraction there is not estimated, not extrapolated from
  the labelled bins, and not guessed. The labelled range starts at 6 px and
  SNR 15; below that this project has no measurement and says so.

  To answer it: draw a stratified sample from the discarded set, chip it, and
  read it blind. Budget for the reader study of 2026-09-28 -- kappa 0.467
  between two readers, and 26 points of drift within one reader across two
  sittings.
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
