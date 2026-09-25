"""P(vessel) for every gated candidate, learned from the 147 read by hand.

WHY THIS EXISTS

`FINDINGS.md` reports that unexplained radar returns do **not** concentrate at
maritime limits. The 147-chip read then measured something that works against
that answer: contamination is not symmetric across bands. Within 2 nm of a
limit, 23% of sampled detections are not moving vessels; beyond 10 nm, 46%
are not. Removing non-vessels therefore thins the far bands about twice as
hard as the near band, which moves the corrected pattern *toward* a near-line
concentration.

So the uncorrected answer is stated on a detection set known to be unevenly
contaminated. This script is the cheap route to the corrected one: learn
P(vessel) from the 147 labels, apply it to all 1,097, and let
`boundary_analysis.py` re-run on the result.

WHAT THE MODEL IS ALLOWED TO SEE, AND WHY THE LIST IS SHORT

Only radar measurements. Specifically excluded, and each for its own reason:

  lon / lat            position IS distance-to-limit. A classifier that knows
                       where a detection is can learn "returns near the 12 nm
                       line are vessels", and the corrected set would then
                       contain the hypothesis it is about to be tested for.
                       This is the exclusion that makes the re-run mean
                       anything.
  nm_to_limit, stratum the same thing, stated outright.
  ais_cell_vessels     how much AIS traffic the surrounding cell carries. It
                       genuinely predicts "vessel", and that is the problem:
                       it is a proxy for shipping geography, which is the
                       confound the shift null exists to control. Including
                       it would make the corrected set follow traffic density
                       by construction.
  date / scene         12 scenes, 147 labels. Scene identity would be
                       memorised, not learned. Sea state is already carried
                       locally by background_dn and sigma_dn.
  reception            constant ("heard") across the whole draw.

What is left is the shape and brightness of the return and the local sea
around it. That is a weaker feature set than the eye had -- the labels were
made from imagery, and the model never sees an image -- so its ceiling is
whatever these summary statistics can express. The cross-validated error
below is the measurement of that ceiling, not a formality.

WHAT IS FIXED BEFORE ANY SCORE IS READ

  target        vessel = 1; clutter and fixed = 0; `ambiguous` is dropped from
                fitting and scored at inference. Ambiguous exists precisely so
                that hard cases do not get forced into whichever class is
                easier to justify; folding them in either direction here would
                undo that.
  thresholds    0.30, 0.50, 0.70 -- three, declared in advance, reported
                together. A single threshold chosen after seeing the band
                table is a threshold fitted to the answer. If the corrected
                verdict is the same at all three, it is robust to the cut; if
                it is not, that is the finding.
  leakage test  the same features are used to predict the *band*. If they can,
                propagation smuggles geography into the corrected set and the
                re-run is circular. This runs and is reported whether or not
                it is convenient.

Needs the `ml` extra: pip install -e ".[ml]"

    python scripts/classify_candidates.py
    python scripts/classify_candidates.py --out data/events/candidate-pvessel.json
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import EVENTS

# Raw properties the model may use. peak_dn is deliberately absent: snr is
# already (peak_dn - background_dn) / sigma_dn, so including peak_dn adds a
# near-exact linear dependence and nothing else.
FEATURES = (
    "snr",
    "pixels",
    "background_dn",
    "sigma_dn",
    "confidence",
)

# The range-pixel column went in first, as a stand-in for incidence angle,
# on the reasoning that sea clutter statistics vary across the swath and a
# candidate's column is a property of sensor geometry rather than of where the
# water sits relative to a legal line.
#
# That reasoning was wrong, and the leakage test below caught it. On this
# relative orbit the swath runs roughly parallel to the coast, so a detection's
# across-track column IS very nearly its distance from shore, and therefore its
# band. Measured on its own it predicts "<=2 nm vs rest" at AUC 0.732 -- it was
# carrying essentially all of the 0.738 the whole feature set achieved, while
# every other feature sat between 0.45 and 0.63. It is a geographic coordinate
# wearing a sensor-geometry costume.
#
# Dropping it costs little physics: incidence angle acts on the imagery through
# local sea brightness, which background_dn and sigma_dn already measure at the
# candidate itself.
USE_RANGE_PIXEL = False

VESSEL = "vessel"
NOT_VESSEL = ("clutter", "fixed")
DROPPED = ("ambiguous",)

THRESHOLDS = (0.30, 0.50, 0.70)

# Repeated stratified CV. 147 labels is small enough that a single 5-fold
# split is mostly noise; repeating it and reporting the spread is the honest
# version.
N_SPLITS, N_REPEATS, SEED = 5, 20, 20260925


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def load_features(path: Path) -> list[dict]:
    """Every scored candidate, as a flat row. No filtering here."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for f in doc.get("features", []):
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"][:2]
        row = {c: p.get(c) for c in FEATURES}
        if USE_RANGE_PIXEL:
            px = p.get("pixel") or [None, None]
            row["range_px"] = px[0]
        row["lon"], row["lat"] = lon, lat
        row["date"] = p.get("date")
        row["reception"] = p.get("reception")
        rows.append(row)
    return rows


def load_labels(path: Path) -> dict[tuple[float, float], str]:
    """Last verdict per chip, keyed by rounded position.

    The log is append-only and last-write-wins per key, so a verdict Joshua
    makes later silently supersedes the machine read without anything being
    deleted. This reads whatever is current.
    """
    out: dict[str, dict] = {}
    if not path.exists():
        return {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("key"):
            out[rec["key"]] = rec
    return {(round(r["lon"], 6), round(r["lat"], 6)): r["verdict"]
            for r in out.values()
            if r.get("lon") is not None and r.get("lat") is not None}


def feature_matrix(rows: list[dict]) -> tuple[list[list[float]], list[str]]:
    names = list(FEATURES) + (["range_px"] if USE_RANGE_PIXEL else [])
    X = []
    for r in rows:
        X.append([float(r[n]) if r[n] is not None else float("nan")
                  for n in names])
    return X, names


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--candidates", type=Path,
                    default=EVENTS / "candidates-scored.geojson")
    ap.add_argument("--labels", type=Path, default=EVENTS / "labels.jsonl")
    ap.add_argument("--out", type=Path,
                    default=EVENTS / "candidate-pvessel.json")
    ap.add_argument("--reception", default="heard",
                    help="restrict scoring to these reception classes")
    args = ap.parse_args()

    try:
        import numpy as np
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import roc_auc_score
        from sklearn.model_selection import RepeatedStratifiedKFold
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        print("\n  Needs scikit-learn:  pip install -e \".[ml]\"\n")
        return 1

    rows = load_features(args.candidates)
    labels = load_labels(args.labels)
    print(f"\n  {len(rows):,} scored candidates from {args.candidates.name}")
    print(f"  {len(labels):,} labelled chips from {args.labels.name}")
    if not labels:
        print("\n  No labels. Run scripts/sample_candidates.py and label "
              "first.\n")
        return 1

    for r in rows:
        r["verdict"] = labels.get((round(r["lon"], 6), round(r["lat"], 6)))

    train = [r for r in rows
             if r["verdict"] in (VESSEL,) + tuple(NOT_VESSEL)]
    dropped = [r for r in rows if r["verdict"] in DROPPED]
    print(f"  {len(train)} usable for fitting "
          f"({sum(1 for r in train if r['verdict'] == VESSEL)} vessel, "
          f"{sum(1 for r in train if r['verdict'] != VESSEL)} not), "
          f"{len(dropped)} ambiguous held out")

    X, names = feature_matrix(train)
    X = np.asarray(X, dtype=float)
    y = np.asarray([1 if r["verdict"] == VESSEL else 0 for r in train])

    def model(kind: str):
        if kind == "logit":
            return make_pipeline(
                SimpleImputer(strategy="median"), StandardScaler(),
                LogisticRegression(max_iter=2000, C=1.0))
        return make_pipeline(
            SimpleImputer(strategy="median"),
            RandomForestClassifier(n_estimators=400, min_samples_leaf=3,
                                   random_state=SEED))

    cv = RepeatedStratifiedKFold(n_splits=N_SPLITS, n_repeats=N_REPEATS,
                                 random_state=SEED)
    print(f"\n  cross-validation: {N_SPLITS}-fold x {N_REPEATS} repeats "
          f"(n={len(y)})")
    print(f"    {'model':10}{'AUC':>16}{'accuracy @0.5':>18}")
    scores: dict[str, np.ndarray] = {}
    cv_auc: dict[str, float] = {}
    for kind in ("logit", "forest"):
        aucs, accs, oof = [], [], np.zeros((N_REPEATS, len(y)))
        rep = 0
        for i, (tr, te) in enumerate(cv.split(X, y)):
            m = model(kind).fit(X[tr], y[tr])
            p = m.predict_proba(X[te])[:, 1]
            oof[i // N_SPLITS, te] = p
            aucs.append(roc_auc_score(y[te], p))
            accs.append(float(((p >= 0.5).astype(int) == y[te]).mean()))
            rep = i // N_SPLITS
        scores[kind] = oof.mean(axis=0)
        cv_auc[kind] = float(np.mean(aucs))
        print(f"    {kind:10}{np.mean(aucs):>8.3f} +-{np.std(aucs):<7.3f}"
              f"{np.mean(accs):>10.3f} +-{np.std(accs):<7.3f}")

    # Select on the metric that was just reported. Ranking instead by AUC over
    # the pooled out-of-fold probabilities is a different, noisier quantity,
    # and it picked the worse-calibrated model on the first run.
    best = max(cv_auc, key=cv_auc.get)
    print(f"\n  out-of-fold confusion, model = {best}")
    p_oof = scores[best]
    print(f"    {'thr':>6}{'TP':>6}{'FP':>6}{'FN':>6}{'TN':>6}"
          f"{'precision':>22}{'recall':>22}")
    for t in THRESHOLDS:
        pred = (p_oof >= t).astype(int)
        tp = int(((pred == 1) & (y == 1)).sum())
        fp = int(((pred == 1) & (y == 0)).sum())
        fn = int(((pred == 0) & (y == 1)).sum())
        tn = int(((pred == 0) & (y == 0)).sum())
        pl, ph = wilson(tp, tp + fp)
        rl, rh = wilson(tp, tp + fn)
        pr = tp / (tp + fp) if tp + fp else float("nan")
        rc = tp / (tp + fn) if tp + fn else float("nan")
        print(f"    {t:>6.2f}{tp:>6}{fp:>6}{fn:>6}{tn:>6}"
              f"{pr:>10.3f} [{pl:.2f},{ph:.2f}]{rc:>10.3f} [{rl:.2f},{rh:.2f}]")

    # ---- the leakage test -------------------------------------------------
    # Can these same features recover which band a detection is in? If they
    # can, the corrected set is contaminated with the hypothesis.
    print("\n  leakage test -- can the features predict distance-to-limit?")
    sample_path = EVENTS / "label-sample.json"
    bands = {}
    if sample_path.exists():
        for s in json.loads(sample_path.read_text(encoding="utf-8"))["sample"]:
            bands[(round(s["lon"], 6), round(s["lat"], 6))] = \
                s["stratum"]["band"]
    have = [(i, bands.get((round(r["lon"], 6), round(r["lat"], 6))))
            for i, r in enumerate(train)]
    have = [(i, b) for i, b in have if b]
    if len(have) < 30:
        print("    not enough banded rows to test; skipped")
    else:
        idx = np.array([i for i, _ in have])
        near = np.array([1 if b == "<=2nm" else 0 for _, b in have])
        aucs = []
        cv2 = RepeatedStratifiedKFold(n_splits=N_SPLITS, n_repeats=5,
                                      random_state=SEED)
        for tr, te in cv2.split(X[idx], near):
            m = model("forest").fit(X[idx][tr], near[tr])
            aucs.append(roc_auc_score(
                near[te], m.predict_proba(X[idx][te])[:, 1]))
        mu, sd = float(np.mean(aucs)), float(np.std(aucs))
        print(f"    AUC for '<=2nm vs rest' from the same features: "
              f"{mu:.3f} +-{sd:.3f}  (n={len(idx)})")
        if mu < 0.60:
            print("    CLEAN -- no usable distance signal in the features")
        else:
            print("    RESIDUAL -- the features partly encode "
                  "distance-to-limit.")
            print("    This cannot be driven to chance: the things that "
                  "separate a vessel")
            print("    from clutter (local sea roughness, return size) are "
                  "themselves")
            print("    properties of where the water is. What matters is the "
                  "DIRECTION,")
            print("    reported below -- a model that under-states the "
                  "measured asymmetry")
            print("    yields a conservative correction.")
        # Direction: does the model reproduce the per-band contamination the
        # labels measured, or invert it?
        print(f"\n    {'band':10}{'n':>5}{'labelled P(vessel)':>20}"
              f"{'model mean P':>15}")
        for bd in ("<=2nm", "2-10nm", ">10nm"):
            ix = [i for (i, b) in have if b == bd]
            if not ix:
                continue
            print(f"    {bd:10}{len(ix):>5}{y[ix].mean():>20.3f}"
                  f"{p_oof[ix].mean():>15.3f}")

    # ---- fit on everything and score the full set -------------------------
    final = model(best).fit(X, y)
    keep = {s.strip() for s in args.reception.split(",") if s.strip()}
    score_rows = [r for r in rows
                  if not keep or r.get("reception", "heard") in keep]
    Xs, _ = feature_matrix(score_rows)
    p = final.predict_proba(np.asarray(Xs, dtype=float))[:, 1]

    out = {
        "what": "P(vessel) per gated candidate, from the labelled sample",
        "model": best,
        "features": names,
        "excluded_deliberately": [
            "lon", "lat", "nm_to_limit", "stratum", "ais_cell_vessels",
            "date", "scene", "reception",
        ],
        "trained_on": {"n": int(len(y)), "vessel": int(y.sum()),
                       "not_vessel": int(len(y) - y.sum()),
                       "ambiguous_held_out": len(dropped)},
        "thresholds": list(THRESHOLDS),
        "reception": sorted(keep),
        "scored": len(score_rows),
        "expected_vessels": float(p.sum()),
        "points": [
            {"lon": r["lon"], "lat": r["lat"], "date": r["date"],
             "p_vessel": round(float(pi), 4),
             "label": r["verdict"]}
            for r, pi in zip(score_rows, p, strict=True)
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"\n  scored {len(score_rows):,} candidates -> {args.out.name}")
    print(f"  expected vessels = sum P = {p.sum():.1f} "
          f"({p.sum() / len(p):.1%} of the set)")
    for t in THRESHOLDS:
        print(f"    p >= {t:.2f}: {int((p >= t).sum()):,} kept")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
