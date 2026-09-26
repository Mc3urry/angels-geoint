"""Thin the detection set by the contamination the labels actually measured.

THE PROBLEM THIS SOLVES, AND WHY IT IS NOT THE CLASSIFIER'S JOB

`boundary_analysis.py` counts unexplained detections per distance band. The
147-chip read showed those detections are not equally real across bands:
within 2 nm of a limit about 84% are vessels, beyond 10 nm about 60% are.
The published answer -- no concentration at the limits -- is therefore
computed on a set that is contaminated *more* in the far bands, which is the
direction that would hide a near-line concentration rather than invent one.

`classify_candidates.py` learns P(vessel) per detection and could be used to
thin the set. Two things argue against making that the primary correction:

  1. Its features partly encode distance-to-limit (leakage AUC ~0.65 after
     the disguised coordinate was removed, and it cannot go much lower --
     local sea roughness and return size are genuinely properties of where
     the water is). A correction built from it is not fully independent of
     the hypothesis being tested.
  2. It under-states the very asymmetry it is meant to correct. Its mean
     P(vessel) by band came out 0.76 / 0.67 / 0.69 against a labelled
     0.84 / 0.69 / 0.60 -- in the far band it barely corrects at all.

This script takes the other route. It does not model an individual
detection. It uses only the per-stratum rate the blind read measured, and
thins each stratum by that rate at random. That is non-circular by
construction: the rates come from labels made without sight of the band, and
within a stratum every detection is treated identically, so nothing about
*where inside the band* a detection sits can influence whether it survives.

WHAT IT ASSUMES, STATED PLAINLY

That the 147 are representative within their stratum. They were drawn by
`sample_candidates.py` at a fixed seed from the gated, AIS-heard population,
so this is the assumption a stratified sample is entitled to -- but the
smallest cell holds 9 labels, and a rate estimated from 9 is a rate with an
interval about as wide as the range it is trying to distinguish. That is why
this writes replicates rather than one answer: each replicate draws its
stratum rates from a Jeffreys posterior, so the thinness of the sample
propagates into the spread of corrected results instead of vanishing into a
point estimate.

AMBIGUOUS

The primary rate excludes `ambiguous` from both numerator and denominator.
Counting it as "not a vessel" would assert what the reader explicitly
declined to assert. `--ambiguous-as-clutter` runs the other convention, and
both should be reported; if the corrected verdict depends on which, that is
the finding.

    python scripts/correct_clutter.py --replicates 20
    python scripts/boundary_analysis.py \
        --candidates data/events/corrected/candidates-corrected-point.geojson \
        --out data/events/corrected/boundary-bands-point.json
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import EVENTS

M_PER_NM = 1852.0

OUT_DIR = EVENTS / "corrected"

# Jeffreys prior. With n = 9 in the smallest cell, the choice of prior is not
# cosmetic; Beta(1/2, 1/2) is the one that does not pretend to information the
# sample does not have.
PRIOR_A = PRIOR_B = 0.5

SEED = 20260925


def beta_sample(rng: random.Random, a: float, b: float) -> float:
    """Beta(a, b) from two gammas. Avoids a numpy dependency here."""
    x = rng.gammavariate(a, 1.0)
    y = rng.gammavariate(b, 1.0)
    return x / (x + y) if (x + y) else 0.5



def _sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utcnow() -> str:
    import datetime
    return (datetime.datetime.now(datetime.timezone.utc)
            .replace(microsecond=0).isoformat())


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
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--replicates", type=int, default=200,
                    help="posterior draws used for the interval below; these "
                         "are computed in memory and cost nothing on disk")
    ap.add_argument("--write-replicates", type=int, default=5,
                    help="how many replicate files to actually write, for "
                         "anyone who wants to re-run the nulls on them. "
                         "Writing all 200 fills the tree with near-identical "
                         "geojson for no gain.")
    ap.add_argument("--reception", default="heard")
    ap.add_argument("--ambiguous-as-clutter", action="store_true",
                    help="count `ambiguous` in the denominator as not-vessel")
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()

    from angels.config import AOI_SEA
    from scripts.boundary_analysis import ANY_NAME, limit_sets
    from scripts.sample_candidates import distance_nm, gate_from_bands, stratum

    doc = json.loads(args.candidates.read_text(encoding="utf-8"))
    keep = {s.strip() for s in args.reception.split(",") if s.strip()}
    feats = [f for f in doc.get("features", [])
             if not keep or f["properties"].get("reception", "heard") in keep]
    print(f"\n  {len(feats):,} candidates ({', '.join(sorted(keep))})")

    gate, _ = gate_from_bands()

    # TWO line sets, deliberately, because the two jobs here were done with
    # two different ones and silently unifying them would move candidates
    # between bands.
    #
    #   `sample_candidates.py` calls limit_sets() with no bbox. The strata it
    #   wrote -- and therefore the population each measured rate belongs to --
    #   are defined by that unbounded set.
    #
    #   `boundary_analysis.py` calls limit_sets(bbox=AOI_SEA). The published
    #   per-band counts are defined by that trimmed set.
    #
    # They differ: the trim drops segments outside the study area plus a
    # margin, so a candidate near the edge can have a different nearest line.
    # On this data exactly one candidate lands in 0-1 nm under one and 1-2 nm
    # under the other. One point does not change a conclusion, but comparing a
    # corrected numerator binned one way against a denominator binned the
    # other is the kind of quiet mismatch that does.
    sets_full = limit_sets()
    sets_aoi = limit_sets(bbox=AOI_SEA)
    lines_strata = sets_full.get(ANY_NAME) or next(iter(sets_full.values()))
    lines_pub = sets_aoi.get(ANY_NAME) or next(iter(sets_aoi.values()))
    for f in feats:
        lon, lat = f["geometry"]["coordinates"][:2]
        nm = distance_nm(lon, lat, lines_strata)
        band, strength, _rec = stratum(f["properties"], nm, gate)
        f["properties"]["_stratum"] = f"{band} / {strength}"
        f["properties"]["_band"] = band
        f["properties"]["_nm_to_limit"] = None if nm is None else round(nm, 3)
        # Distance under the PUBLISHED line set, in raw metres. Unrounded on
        # purpose: round(nm, 3) * 1852 quantises to ~1.9 m, and a point a hair
        # from the 1 nm line then bins on the wrong side. That is the same
        # double-rounding that silently lost 14 of 147 chips from the label
        # queue; once was enough.
        nm_pub = distance_nm(lon, lat, lines_pub)
        f["properties"]["_d_m"] = None if nm_pub is None else nm_pub * M_PER_NM

    labels = load_labels(args.labels)
    tally: dict[str, Counter] = defaultdict(Counter)
    for f in feats:
        lon, lat = f["geometry"]["coordinates"][:2]
        v = labels.get((round(lon, 6), round(lat, 6)))
        if v:
            tally[f["properties"]["_stratum"]][v] += 1

    print(f"\n  measured vessel rate per stratum "
          f"(ambiguous {'counted as clutter' if args.ambiguous_as_clutter else 'excluded'})")
    print(f"    {'stratum':24}{'labelled':>10}{'vessel':>8}{'rate':>8}"
          f"{'population':>12}")
    pop = Counter(f["properties"]["_stratum"] for f in feats)
    rates: dict[str, tuple[int, int]] = {}
    for s in sorted(pop):
        c = tally.get(s, Counter())
        k = c["vessel"]
        # Named explicitly, not sum(c.values()): a verdict added later --
        # `no-data` was -- would otherwise slide into the denominator and
        # quietly lower every rate without anything saying so.
        n = c["vessel"] + c["clutter"] + c["fixed"] + \
            (c["ambiguous"] if args.ambiguous_as_clutter else 0)
        rates[s] = (k, n)
        r = f"{k / n:.3f}" if n else "  --  "
        print(f"    {s:24}{n:>10}{k:>8}{r:>8}{pop[s]:>12,}")

    excluded: Counter = Counter()
    for c in tally.values():
        for v, k in c.items():
            if v not in ("vessel", "clutter", "fixed") and not (
                    args.ambiguous_as_clutter and v == "ambiguous"):
                excluded[v] += k
    if excluded:
        print("\n  labelled chips held out of every denominator: "
              + ", ".join(f"{k} {v}" for v, k in sorted(excluded.items())))
        print("  `no-data` means the candidate falls in the scene's no-data "
              "border ramp,\n  so there was no ground to read -- a detector "
              "defect, not a hard chip.")

    missing = [s for s, (k, n) in rates.items() if n == 0]
    if missing:
        print(f"\n  {len(missing)} stratum/strata carry no labels: "
              f"{', '.join(missing)}")
        print("  Those detections are kept unthinned -- an unlabelled stratum "
              "is not\n  evidence of a clean one, and dropping them would be a "
              "correction\n  invented rather than measured.")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    written = []

    def write(name: str, chosen: list[dict], note: str) -> None:
        p = args.out_dir / name
        p.write_text(json.dumps({
            "type": "FeatureCollection",
            "properties": {
                "derived_from": args.candidates.name,
                "correction": note,
                "labels": args.labels.name,
                "ambiguous_as_clutter": args.ambiguous_as_clutter,
                "kept": len(chosen), "of": len(feats),
            },
            "features": chosen,
        }, indent=1), encoding="utf-8")
        written.append((name, len(chosen)))

    # Point estimate: keep the expected number per stratum, chosen at random.
    rng = random.Random(args.seed)
    point = []
    for s in sorted(pop):
        k, n = rates[s]
        grp = [f for f in feats if f["properties"]["_stratum"] == s]
        rng.shuffle(grp)
        frac = (k / n) if n else 1.0
        point += grp[:round(frac * len(grp))]
    write("candidates-corrected-point.geojson", point,
          "per-stratum thinning at the measured rate (MLE)")

    replicate_sets: list[list[dict]] = []
    for i in range(args.replicates):
        r = random.Random(args.seed + 1 + i)
        chosen = []
        for s in sorted(pop):
            k, n = rates[s]
            rate = beta_sample(r, k + PRIOR_A, n - k + PRIOR_B) if n else 1.0
            chosen += [f for f in feats
                       if f["properties"]["_stratum"] == s
                       and r.random() < rate]
        if i < args.write_replicates:
            write(f"candidates-corrected-{i:02d}.geojson", chosen,
                  f"replicate {i}: stratum rates drawn from Beta posteriors")
        replicate_sets.append(chosen)

    print(f"\n  wrote {len(written)} file(s) to {args.out_dir}; "
          f"{args.replicates} replicates computed in memory")
    sizes = sorted(len(r) for r in replicate_sets)
    print(f"    kept: point={len(point):,}  "
          f"replicates {sizes[0]:,}-{sizes[-1]:,}" if sizes
          else f"    kept: {len(point):,}")
    print("\n  per-band effect of the point correction")
    before = Counter(f["properties"]["_band"] for f in feats)
    after = Counter(f["properties"]["_band"] for f in point)
    print(f"    {'band':10}{'before':>9}{'after':>9}{'kept':>8}")
    for b in sorted(before):
        print(f"    {b:10}{before[b]:>9,}{after[b]:>9,}"
              f"{after[b] / before[b]:>8.2f}")

    # Publish the per-band keep-fractions instead of leaving them to be
    # transcribed. `tune_gate.py` held a hand-copied constant of these, and
    # after the pass-3 re-read its >10nm value said 0.62 while the
    # measurement said 0.59 -- so the rule that decides whether a gate may
    # be applied upstream of the boundary test was being checked against a
    # number the measurement no longer produced. The digest lets the
    # consumer tell that for itself rather than trust the file's presence.
    keep = {b: round(after[b] / before[b], 4) for b in sorted(before)}
    (args.out_dir / "keep-fractions.json").write_text(json.dumps({
        "what": "keep-fraction per band under the point correction",
        "per_band": keep,
        "labels": args.labels.name,
        "labels_sha256": _sha256(args.labels),
        "seed": args.seed,
        "at": _utcnow(),
    }, indent=1) + "\n", encoding="utf-8")
    print(f"\n  per-band keep-fractions -> {args.out_dir / 'keep-fractions.json'}")

    report(feats, point, replicate_sets, args)
    print()
    return 0


def report(feats: list[dict], point: list[dict],
           replicates: list[list[dict]], args) -> None:
    """What the correction does to observed / expected, without the nulls.

    The nulls are the expensive part of `boundary_analysis.py` and they do not
    need re-running to answer the first question, which is whether the
    correction can move the ratios far enough to matter.

    Thinning changes only the observed counts. The expected counts come from
    searched water, which the correction does not touch -- so the corrected
    ratio for a band is exactly

        ratio_corrected = observed_corrected / (expected * N_corr / N_orig)

    and the whole propagation is arithmetic. If this says the sign cannot
    flip, re-running the nulls will not make it flip either: random thinning
    within a stratum preserves the spatial pattern and lowers n, which costs
    power rather than creating significance.
    """
    from scripts.boundary_analysis import band_of

    bands_path = EVENTS / "boundary-bands.json"
    if not bands_path.exists():
        print("\n  no boundary-bands.json; skipping the ratio propagation")
        return
    pub = json.loads(bands_path.read_text(encoding="utf-8"))
    any_limit = pub.get("limits", {}).get("any limit")
    if not any_limit:
        print("\n  boundary-bands.json has no 'any limit' entry; skipped")
        return
    expected = any_limit["expected_by_band"]
    obs0 = any_limit["candidates_by_band"]
    n0 = sum(obs0.values())

    def binned(fs: list[dict]) -> Counter:
        c = Counter()
        for f in fs:
            d = f["properties"].get("_d_m")
            c[band_of(float("inf") if d is None else d)] += 1
        return c

    def ratios(c: Counter) -> dict[str, float]:
        n = sum(c.values())
        scale = (n / n0) if n0 else 1.0
        return {b: (c[b] / (expected[b] * scale) if expected.get(b) and scale
                    else float("nan")) for b in expected}

    # Before trusting any corrected ratio, check that this script bins
    # candidates the way boundary_analysis did -- otherwise a corrected
    # numerator is being divided by a denominator built for a different
    # partition.
    #
    # It will not match exactly at the fine bands, and the reason is worth
    # knowing: `Bander.band()` caches its answer per 0.01 deg cell, so a
    # candidate's published band is really the band of whichever point in
    # that ~1.1 km cell was asked about first. That is consistent with the
    # denominator, which is computed on the same grid, but it means the
    # published band of an individual point is cell-resolution. Exactly one
    # candidate sits close enough to the 1 nm line for this to matter.
    #
    # So the check is made at the granularity the correction actually works
    # at -- the strata bands -- where cell quantisation cannot move a point,
    # and the fine table below is reported as indicative.
    check = binned(feats)
    coarse = {
        "<=2 nm": ("0-1 nm", "1-2 nm"),
        "2-10 nm": ("2-5 nm", "5-10 nm"),
        ">10 nm": ("10-25 nm", "> 25 nm"),
    }
    drift = {k: sum(check[b] for b in bs) - sum(obs0[b] for b in bs)
             for k, bs in coarse.items()
             if sum(check[b] for b in bs) != sum(obs0[b] for b in bs)}
    if drift:
        print(f"\n  *** band assignment does not match boundary-bands.json "
              f"at the strata granularity: {drift}")
        print("  The corrected ratios below are NOT comparable to the "
              "published ones.")
    else:
        fine = {b: check[b] - obs0[b] for b in obs0 if check[b] != obs0[b]}
        print(f"\n  band assignment reproduces the published counts at the "
              f"strata granularity ({sum(obs0.values()):,} candidates)")
        if fine:
            print(f"  fine bands differ by {fine} -- one point inside "
                  f"boundary_analysis's\n  0.01 deg band cache; see the note "
                  f"in report().")

    r0 = {b: (obs0[b] / expected[b] if expected.get(b) else float("nan"))
          for b in expected}
    rp = ratios(binned(point))
    reps = [ratios(binned(r)) for r in replicates]

    print("\n  observed / expected, 'any limit' -- published vs corrected")
    print("  (>1 = more unexplained returns than searched water predicts)")
    print(f"    {'band':10}{'published':>11}{'corrected':>11}"
          f"{'replicate 5-95%':>22}")
    for b in expected:
        lo_hi = sorted(x[b] for x in reps) if reps else []
        span = (f"{lo_hi[int(0.05 * len(lo_hi))]:.2f} - "
                f"{lo_hi[int(0.95 * (len(lo_hi) - 1))]:.2f}") if lo_hi else "--"
        print(f"    {b:10}{r0[b]:>11.3f}{rp[b]:>11.3f}{span:>22}")

    near = [b for b in expected if b in ("0-1 nm", "1-2 nm")]
    n_pub = sum(obs0[b] for b in near) / sum(expected[b] for b in near)
    cb = binned(point)
    scale = sum(cb.values()) / n0
    n_cor = sum(cb[b] for b in near) / (sum(expected[b] for b in near) * scale)
    # An adversarial bound, so the conclusion does not rest on the point
    # estimate. Give the hypothesis every break the data can be tortured
    # into: assume EVERY detection within 2 nm is a real vessel, and that
    # every at-gate detection beyond 10 nm is clutter. Nothing in the labels
    # supports this -- it is the most extreme reading the strata allow -- and
    # it is here to show what the answer would be even then.
    best_case = _extreme(feats, expected, obs0, n0)
    print(f"\n    within 2 nm:  {n_pub:.3f} published  ->  {n_cor:.3f} "
          f"corrected  ->  {best_case:.3f} under the most favourable "
          f"assumption the strata allow")
    if max(n_cor, best_case) < 1.0:
        print("    Still depleted, in every version. The correction moves the "
              "number in the")
        print("    direction the contamination asymmetry predicts, and does "
              "not come close to")
        print("    reversing the sign -- not at the point estimate, not "
              "across 5-95% of the")
        print("    posterior, and not under an assumption chosen to favour "
              "the hypothesis.")
        print("    The published verdict survives this correction.")
    elif n_cor < 1.0:
        print("    The point estimate stays depleted, but the adversarial "
              "bound crosses 1.")
        print("    Re-run the nulls before saying anything stronger.")
    else:
        print("    The sign flipped. Re-run boundary_analysis.py on the "
              "corrected set;")
        print("    the ratio alone is not a p-value.")


def _extreme(feats: list[dict], expected: dict, obs0: dict, n0: int) -> float:
    """Ratio within 2 nm if the far band were as contaminated as it could be.

    Keeps every candidate within 2 nm, keeps `clear` detections elsewhere at
    their measured rate, and throws away every `at-gate` detection beyond
    2 nm. This is not a defensible correction; it is an upper bound on what
    any clutter correction of this shape could do.
    """
    from scripts.boundary_analysis import band_of
    kept = []
    for f in feats:
        p = f["properties"]
        band, strength = p["_band"], p["_stratum"].split(" / ")[-1]
        if band == "<=2nm":
            kept.append(f)
        elif strength == "clear":
            kept.append(f)
    c = Counter()
    for f in kept:
        d = f["properties"].get("_d_m")
        c[band_of(float("inf") if d is None else d)] += 1
    near = ("0-1 nm", "1-2 nm")
    scale = sum(c.values()) / n0 if n0 else 1.0
    denom = sum(expected[b] for b in near) * scale
    return sum(c[b] for b in near) / denom if denom else float("nan")


if __name__ == "__main__":
    # The report is written by the run, not captured after it.
    from scripts._report import tee
    with tee(EVENTS / "corrected" / "report.txt"):
        _rc = main()
    raise SystemExit(_rc)
