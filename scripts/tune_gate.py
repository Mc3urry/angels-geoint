"""What the detection gate costs, measured against the labels.

    python scripts/tune_gate.py
    python scripts/tune_gate.py --bins 6,8,12,20,35,70

THE DEFECT THIS EXISTS TO WORK AROUND

`tune_detector.py` chooses a gate by a pre-registered rule: among gates that
keep at least 90% of the AIS-confirmed detection rate, take the one with the
fewest **unmatched detections per 1,000 km2**. That rule was written before
any candidate had been looked at, and at the time "unmatched" was the only
available stand-in for "false".

The 147 labels say it is the wrong stand-in. Of the 1,097 unmatched
detections in AIS-heard water, roughly **64% are real vessels** -- vessels
that were there and did not report. Those are not a cost. They are the
entire output of the project. A rule that minimises unmatched-per-km2
minimises the signal and the noise together, and cannot tell which it is
removing.

This script does not fix `tune_detector.py`; that script re-runs detection
and matching over the raw scenes and cannot be re-run from here, so changing
its cost function untested would be worse than leaving it. It measures the
same trade with the noise term estimated from labels instead of assumed.

WHAT IS FIXED BEFORE THE TABLES ARE READ

  estimator     P(vessel) per cluster-size bin, from the labels, `ambiguous`
                excluded from both numerator and denominator.
  disqualifier  a gate that thins the distance bands unevenly is disqualified
                for any use upstream of the boundary test, whatever its
                purity, because it would substitute one geographic bias for
                another. This is tested and reported regardless of outcome.
  the trade     clutter removed per real vessel lost. A gate is worth raising
                while that number is large and worth stopping when it is not.

THE STRUCTURAL LIMIT, WHICH IS NOT SMALL

`candidates-scored.geojson` is already gated at SNR >= 15 and >= 6 pixels,
and the labels were drawn from it. So nothing here can say anything about
what a *looser* gate would admit -- the evidence for that lives in the raw
detection files and belongs to `tune_detector.py`. This measures upward
only.

TWO JOBS, AND THEY WANT DIFFERENT GATES

  the detection product -- dossiers, the viewer, training data for Phase C --
  wants purity, and can afford to lose real vessels to get it.

  the boundary statistic does NOT want this gate. It wants the detection set
  thinned at exactly the measured contamination rate and no further, which is
  what `correct_clutter.py` does. A gate strict enough to be worth using for
  the product removes real vessels too, and removes them unevenly across
  bands, which would inflate the near-line ratio beyond what the
  contamination justifies. Use the gate for the product. Use the thinning for
  the test. They are not interchangeable and the table below shows where they
  part company.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from collections import Counter
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import EVENTS

VESSEL = "vessel"
NOT_VESSEL = ("clutter", "fixed")

DEFAULT_BINS = (6, 8, 12, 20, 35, 70)
GATES = (8, 10, 12, 15, 20, 25, 35)

# Keep-fractions the measured contamination justifies, per band. These are
# read from the artefact `correct_clutter.py` writes, never transcribed. A
# hand-copied constant lived here until 2026-09-26, and after the pass-3
# re-read it said >10nm 0.62 while the measurement said 0.59 -- so the rule
# that decides whether a gate may go upstream of the boundary test was being
# checked against a number nothing produced any more. If the artefact is
# missing, or was written from a different labels.jsonl than the one being
# read now, this script stops. It does not fall back to a remembered value,
# because a disqualifier that guesses is not a disqualifier.
KEEP_FRACTIONS = EVENTS / "corrected" / "keep-fractions.json"

SPLITS, SEED = 50, 20260925



def load_justified(path: Path, labels: Path) -> dict[str, float]:
    """The measured keep-fractions, or a refusal -- never a remembered value."""
    import hashlib
    if not path.exists():
        raise SystemExit(
            f"\n  {path} is missing.\n"
            "  The justified keep-fractions come from the clutter correction,\n"
            "  not from a constant in this file. Run:\n"
            "      python scripts/correct_clutter.py\n"
            "  and then run this again.\n")
    doc = json.loads(path.read_text(encoding="utf-8"))
    want = hashlib.sha256(labels.read_bytes()).hexdigest()
    got = doc.get("labels_sha256")
    if got != want:
        raise SystemExit(
            f"\n  {path.name} was written from a different {labels.name}.\n"
            f"      it recorded  {got}\n"
            f"      current file {want}\n"
            "  Re-run `python scripts/correct_clutter.py` so the keep-fractions\n"
            "  describe the labels this script is about to read. Comparing a\n"
            "  gate against a stale correction is how the last one went wrong.\n")
    per = doc.get("per_band") or {}
    if not per:
        raise SystemExit(f"\n  {path.name} carries no per_band block.\n")
    return {k: float(v) for k, v in per.items()}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def load_labels(path: Path) -> dict[tuple[float, float], str]:
    out: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("key"):
            out[rec["key"]] = rec           # last write per chip wins
    return {(round(r["lon"], 6), round(r["lat"], 6)): r["verdict"]
            for r in out.values() if r.get("lon") is not None}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--candidates", type=Path,
                    default=EVENTS / "candidates-scored.geojson")
    ap.add_argument("--labels", type=Path, default=EVENTS / "labels.jsonl")
    ap.add_argument("--reception", default="heard")
    ap.add_argument("--bins", default=",".join(str(b) for b in DEFAULT_BINS),
                    help="lower edges of the cluster-size bins")
    args = ap.parse_args()

    from scripts.boundary_analysis import ANY_NAME, limit_sets
    from scripts.sample_candidates import distance_nm, gate_from_bands, stratum

    edges = [int(x) for x in args.bins.split(",") if x.strip()]
    bins = list(zip(edges, list(edges[1:]) + [10 ** 9]))

    def binof(px: int) -> int:
        for i, (lo, hi) in enumerate(bins):
            if lo <= px < hi:
                return i
        return len(bins) - 1

    labels = load_labels(args.labels)
    gate, _ = gate_from_bands()
    sets = limit_sets()
    lines = sets.get(ANY_NAME) or next(iter(sets.values()))
    keep_rec = {s.strip() for s in args.reception.split(",") if s.strip()}

    rows = []
    for f in json.loads(args.candidates.read_text(encoding="utf-8"))["features"]:
        p = f["properties"]
        if keep_rec and p.get("reception", "heard") not in keep_rec:
            continue
        lon, lat = f["geometry"]["coordinates"][:2]
        band, _st, _rc = stratum(p, distance_nm(lon, lat, lines), gate)
        rows.append({"px": p["pixels"], "snr": p["snr"], "band": band,
                     "v": labels.get((round(lon, 6), round(lat, 6)))})

    justified = load_justified(KEEP_FRACTIONS, args.labels)

    usable = [r for r in rows if r["v"] in (VESSEL,) + NOT_VESSEL]
    # Every excluded verdict is named from the data. Spelling `ambiguous`
    # here and nothing else is how `no-data` would have left the count
    # without appearing in the sentence that reports the count.
    excluded = Counter(r["v"] for r in rows
                       if r["v"] is not None
                       and r["v"] not in (VESSEL,) + NOT_VESSEL)
    print(f"\n  {len(rows):,} candidates, {len(usable)} usable labels "
          f"({sum(1 for r in usable if r['v'] == VESSEL)} vessel); "
          + (", ".join(f"{k} {v}" for v, k in sorted(excluded.items()))
             + " excluded" if excluded else "nothing excluded"))

    rate: dict[int, float] = {}
    print("\n  P(vessel) per cluster-size bin -- the estimator everything below uses")
    print(f"    {'pixels':12}{'labelled':>10}{'vessel':>8}{'rate':>8}   95% Wilson")
    for i, (lo, hi) in enumerate(bins):
        s = [r for r in usable if binof(r["px"]) == i]
        k = sum(1 for r in s if r["v"] == VESSEL)
        rate[i] = k / len(s) if s else float("nan")
        lo_, hi_ = wilson(k, len(s))
        name = f"{lo}-{hi}" if hi < 10 ** 9 else f"{lo}+"
        flag = "   <- thin" if len(s) < 10 else ""
        print(f"    {name:12}{len(s):>10}{k:>8}{rate[i]:>8.3f}   "
              f"[{lo_:.2f}, {hi_:.2f}]{flag}")

    # Held out, because an estimator scored on its own fitting data is not
    # scored. Half the labels fit the bin rates; the other half is predicted.
    rng = random.Random(SEED)
    errs = []
    pairs = [(r["px"], 1 if r["v"] == VESSEL else 0) for r in usable]
    for _ in range(SPLITS):
        idx = list(range(len(pairs)))
        rng.shuffle(idx)
        a = [pairs[i] for i in idx[:len(idx) // 2]]
        b = [pairs[i] for i in idx[len(idx) // 2:]]
        fit: dict[int, float] = {}
        for i in range(len(bins)):
            s = [y for px, y in a if binof(px) == i]
            fit[i] = sum(s) / len(s) if s else float("nan")
        glob = sum(y for _, y in a) / len(a)
        pred = sum(fit[binof(px)] if fit[binof(px)] == fit[binof(px)] else glob
                   for px, _ in b)
        act = sum(y for _, y in b)
        if act:
            errs.append(abs(pred - act) / act)
    print(f"\n  held out ({SPLITS} random halves): the bin rates fitted on half "
          f"the labels predict\n  the vessel count in the other half to a median "
          f"{statistics.median(errs):.1%} error "
          f"(90th pct {sorted(errs)[int(0.9 * len(errs))]:.1%}).")

    def vessels(sub) -> float:
        return sum(rate[binof(r["px"])] for r in sub)

    print("\n\n  THE TRADE -- what each gate buys and what it costs\n")
    print(f"    {'gate':>9}{'kept':>8}{'est vessels':>13}{'est clutter':>13}"
          f"{'purity':>9}{'vessels lost':>14}{'clutter per':>13}")
    print(f"    {'':>9}{'':>8}{'':>13}{'':>13}{'':>9}{'':>14}{'vessel lost':>13}")
    base_v = vessels(rows)
    base_c = len(rows) - base_v
    print(f"    {'(none)':>9}{len(rows):>8}{base_v:>13.0f}{base_c:>13.0f}"
          f"{base_v / len(rows):>9.3f}{0:>14}{'--':>13}")
    for g in GATES:
        kept = [r for r in rows if r["px"] >= g]
        if not kept:
            continue
        v = vessels(kept)
        lost = base_v - v
        removed = base_c - (len(kept) - v)
        ratio = f"{removed / lost:.2f}" if lost > 0 else "--"
        print(f"    px>={g:<5}{len(kept):>8}{v:>13.0f}{len(kept) - v:>13.0f}"
              f"{v / len(kept):>9.3f}{lost:>14.0f}{ratio:>13}")

    print("\n\n  THE DISQUALIFIER -- does the gate thin the bands unevenly?\n")
    print("    A gate's keep-fraction per band, against the keep-fraction the")
    print("    measured contamination justifies. Where they match, the gate is")
    print("    removing what the labels say is not a vessel. Where the gate is")
    print("    lower, it has started removing vessels.\n")
    bands = [b for b in ("<=2nm", "2-10nm", ">10nm")
             if any(r["band"] == b for r in rows)]
    tot = {b: sum(1 for r in rows if r["band"] == b) for b in bands}
    print(f"    {'gate':>9}" + "".join(f"{b:>20}" for b in bands)
          + f"{'spread':>9}")
    print(f"    {'':>9}" + "".join(f"{'gate / justified':>20}" for _ in bands))
    for g in GATES:
        cells, keeps = [], []
        for b in bands:
            k = sum(1 for r in rows if r["band"] == b and r["px"] >= g)
            frac = k / tot[b] if tot[b] else float("nan")
            keeps.append(frac)
            cells.append(f"{frac:.3f} / {justified.get(b, float('nan')):.2f}")
        spread = max(keeps) / min(keeps) if min(keeps) > 0 else float("inf")
        print(f"    px>={g:<5}" + "".join(f"{c:>20}" for c in cells)
              + f"{spread:>9.2f}")
    print(f"\n    (the clutter correction's own band spread is "
          f"{max(justified.values()) / min(justified.values()):.2f}x)")
    print("\n  Read the disqualifier before the trade. A gate whose spread "
          "exceeds the\n  contamination's own is removing vessels unevenly, and "
          "must not be applied\n  upstream of the boundary test at any purity.\n")
    return 0


if __name__ == "__main__":
    # The report is written by the run, not captured after it.
    from scripts._report import tee
    with tee(EVENTS / "corrected" / "gate-report.txt"):
        _rc = main()
    raise SystemExit(_rc)
