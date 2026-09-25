"""Draw a stratified sample of candidates for a human to read.

    python scripts/sample_candidates.py                 # ~250 chips
    python scripts/sample_candidates.py --per 20 --seed 7

WHY A SAMPLE, AND WHY STRATIFIED

There are 1,097 gated candidates and 21 chips on disk. Reading all of them is
not going to happen; reading the first twenty is how you learn about the
first twenty. Neither produces a clutter rate.

What the project needs from this is a rate it can DEFEND -- and the rate is
not one number. Clutter at the gate is not clutter well above it, clutter in
a bay is not clutter offshore, and clutter on a rough pass is not clutter on
a calm one. A single pooled figure would average all of that and then be
applied to bands where it does not hold.

So the sample is drawn per stratum, and each stratum gets enough draws to
carry its own interval:

    distance      near a limit / mid / far      the bands the result is about
    strength      at the gate / clear of it     where false positives live
    reception     heard / intermittent          the water the claim covers
    pass          spread across all twelve      sea state drives density 16x

Strata are drawn independently with a fixed seed, so the sample is
reproducible and can be extended later without re-reading what is done.

WHAT THIS IS FOR, DOWNSTREAM

Two things, and the second is the one that is blocked:

  1. a measured clutter rate per stratum, to replace the assumption sitting
     under the boundary result;
  2. LABELS. inversion/ cannot be trained before they exist -- that is
     written into the checklist and it is still true.

OUTPUT

    data/events/label-sample.json   the draw, with the context a reader needs
                                    to judge each chip: strength, size,
                                    reception, and how far it sits from the
                                    nearest maritime limit.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import EVENTS

# The gate the published result used. Read from boundary-bands.json when it
# is there, so the sample can never be drawn from a different population than
# the analysis it is meant to validate.
GATE = {"min_snr": 15.0, "min_pixels": 6}
KEEP_RECEPTION = ("heard",)

M_PER_NM = 1852.0

# Distance strata, in nautical miles from the nearest limit. These are the
# bands the boundary question is actually about; a clutter rate that differs
# between them changes the result rather than decorating it.
NEAR, MID = 2.0, 10.0


def gate_from_bands() -> tuple[dict, tuple[str, ...]]:
    p = EVENTS / "boundary-bands.json"
    if not p.exists():
        return GATE, KEEP_RECEPTION
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        return (doc.get("gate", GATE),
                tuple(doc.get("reception_kept", KEEP_RECEPTION)))
    except (OSError, ValueError):
        return GATE, KEEP_RECEPTION


def distance_nm(lon: float, lat: float, lines) -> float | None:
    d = lines.distance_m(lon, lat)
    return None if d is None else d / M_PER_NM


def stratum(props: dict, nm: float | None, gate: dict) -> tuple[str, str, str]:
    if nm is None:
        band = "unknown"
    elif nm <= NEAR:
        band = f"<={NEAR:g}nm"
    elif nm <= MID:
        band = f"{NEAR:g}-{MID:g}nm"
    else:
        band = f">{MID:g}nm"

    snr = float(props.get("snr") or 0)
    # "At the gate" is where a detector's false positives live. Anything the
    # gate only just admitted is the population most worth a human eye.
    edge = float(gate.get("min_snr", 15.0))
    strength = "at-gate" if snr < edge * 1.35 else "clear"

    return band, strength, str(props.get("reception") or "unknown")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--per", type=int, default=28,
                    help="draws per stratum (default 28)")
    ap.add_argument("--seed", type=int, default=20260925)
    ap.add_argument("--src", default="candidates-scored.geojson")
    ap.add_argument("--out", default="label-sample.json")
    args = ap.parse_args()

    src = EVENTS / args.src
    if not src.exists():
        raise SystemExit(f"\n  No {src}. Run scripts/ais_coverage.py first.\n")
    doc = json.loads(src.read_text(encoding="utf-8"))
    feats = doc.get("features", [])

    gate, keep = gate_from_bands()
    print(f"\n  {len(feats):,} scored candidates")
    print(f"  gate: SNR >= {gate.get('min_snr')}, pixels >= "
          f"{gate.get('min_pixels')}, reception in {list(keep)}")

    # The same gate as the analysis, applied here rather than assumed.
    kept = [f for f in feats
            if float(f["properties"].get("snr") or 0) >= gate.get("min_snr", 0)
            and int(f["properties"].get("pixels") or 0) >= gate.get("min_pixels", 0)
            and str(f["properties"].get("reception")) in keep]
    print(f"  {len(kept):,} pass the gate -- this is the population the "
          f"result rests on")

    # Distance to the nearest limit, computed once here and carried on each
    # sampled record, because a reader judging a chip should know whether it
    # sits on a line and Phase C will want it as a feature.
    from scripts.boundary_analysis import ANY_NAME, limit_sets
    sets = limit_sets()
    lines = sets.get(ANY_NAME) or next(iter(sets.values()))
    print(f"  limits loaded: {', '.join(sorted(sets))}")

    buckets: dict[tuple, list] = defaultdict(list)
    for f in kept:
        lon, lat = f["geometry"]["coordinates"]
        nm = distance_nm(lon, lat, lines)
        f["properties"]["_nm_to_limit"] = None if nm is None else round(nm, 2)
        buckets[stratum(f["properties"], nm, gate)].append(f)

    rng = random.Random(args.seed)
    sample, rows = [], []
    for key in sorted(buckets):
        pool = buckets[key]
        # Spread within a stratum across passes before truncating, so one
        # rough date cannot supply a whole cell of the design.
        by_date: dict[str, list] = defaultdict(list)
        for f in pool:
            by_date[str(f["properties"].get("date"))].append(f)
        for d in by_date:
            rng.shuffle(by_date[d])
        picked, dates = [], sorted(by_date)
        i = 0
        while len(picked) < min(args.per, len(pool)):
            d = dates[i % len(dates)]
            if by_date[d]:
                picked.append(by_date[d].pop())
            elif all(not by_date[x] for x in dates):
                break
            i += 1
        rows.append((key, len(pool), len(picked)))
        for f in picked:
            p = f["properties"]
            lon, lat = f["geometry"]["coordinates"]
            sample.append({
                "id": f"{p.get('date')}_{lat:.4f}_{lon:.4f}".replace(".", "p"),
                "lon": lon, "lat": lat,
                "date": p.get("date"), "t": p.get("t"),
                "scene": p.get("scene"),
                "snr": p.get("snr"), "pixels": p.get("pixels"),
                "length_m_approx": p.get("length_m_approx"),
                "uncertainty_m": p.get("uncertainty_m"),
                "reception": p.get("reception"),
                "ais_cell_vessels": p.get("ais_cell_vessels"),
                "nm_to_limit": p.get("_nm_to_limit"),
                "stratum": {"band": key[0], "strength": key[1],
                            "reception": key[2]},
            })

    print(f"\n  {'band':>12}{'strength':>10}{'reception':>14}"
          f"{'pool':>8}{'drawn':>7}")
    for (band, strength, rec), n_pool, n_drawn in rows:
        short = "" if n_drawn >= args.per else "   (all there were)"
        print(f"  {band:>12}{strength:>10}{rec:>14}{n_pool:>8,}{n_drawn:>7}{short}")

    out = EVENTS / args.out
    out.write_text(json.dumps({
        "drawn": len(sample),
        "population": len(kept),
        "per_stratum": args.per,
        "seed": args.seed,
        "gate": gate,
        "reception_kept": list(keep),
        "bands_nm": {"near": NEAR, "mid": MID},
        "what": ("a stratified sample of gated candidates for human reading. "
                 "Verdicts go to labels.jsonl; this file is the draw, and it "
                 "is reproducible from its seed."),
        "sample": sample,
    }, indent=1), encoding="utf-8")
    print(f"\n  {len(sample)} drawn from {len(kept):,}  ->  {out}")
    print(f"  next: python scripts/inspect_candidates.py --sample {args.out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
