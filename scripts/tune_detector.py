"""Choose a detection gate on one pass, then test it on another.

    python scripts/tune_detector.py                           # Sept trains, June tests
    python scripts/tune_detector.py --train 2024-06-21 --test 2024-09-25
    python scripts/tune_detector.py --min-rate-kept 0.9

THE PROBLEM

The detector runs at k = 6 sigma with clusters as small as 2 pixels, and it
returns hundreds of detections per slice where AIS accounts for a handful.
Most of the rest are sea clutter: sea in SAR is heavy-tailed, and a 6-sigma
line admits a long tail of bright speckle. Every one of them is an
"unmatched detection", and an unmatched detection is the raw material of a
dark-vessel claim.

A stricter gate removes clutter and, past some point, real vessels too. This
script measures that trade instead of guessing it.

WHAT IT DOES

For every combination of a minimum SNR and a minimum cluster size, it keeps
only the detections that pass, re-runs the matching, and reports two numbers:

    detection rate    for vessels of 25 m or more, scored exactly as
                      match_maritime.py scores them (calibration.py)
    unmatched         detections no report explains, per 1,000 km2 searched

Both come from the SAME detections, so the trade is measured, not modelled.
A gate is applied after detection rather than by re-running it: raising the
SNR floor keeps exactly the clusters whose peak clears the higher line, which
is what a higher k would find, give or take cluster edges.

WHY TWO PASSES

Choosing the gate on the data it is then scored on is fitting the noise. So
the gate is CHOSEN on --train and only REPORTED on --test, and the rule for
choosing is written down before either is looked at:

    the strictest gate whose training detection rate (>= 25 m) stays at or
    above --min-rate-kept of the ungated rate

"Strictest" means fewest unmatched detections per km2. The test pass then
says whether that choice holds on a day it never saw. If the two disagree,
that is the finding, and a third pass decides.

Needs detection files from the CURRENT detector (see the water mask fix of
2026-09-22): run detect_ships.py on every slice first.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.adapters.maritime.ais import AISReadError, tracks_at
from angels.adapters.maritime.searched import SearchedArea
from angels.config import AOI_SEA, EVENTS, RAW
from angels.core.detectors import calibration, matching

sys.path.insert(0, str(Path(__file__).parent))
from match_maritime import (in_footprint, read_detections,  # noqa: E402
                            scene_date, split_by_searched)

AIS_STORE = RAW / "maritime"

SNR_GATES = (6, 7, 8, 9, 10, 12, 15, 20)
PIXEL_GATES = (2, 3, 4, 6)


@dataclass
class Scene:
    name: str
    day: date
    obs: list
    tracks: list
    t: object
    searched_km2: float


@dataclass
class Outcome:
    headline: calibration.Rate
    unmatched: int
    searched_km2: float

    @property
    def per_1000km2(self) -> float:
        return 1000 * self.unmatched / self.searched_km2 if self.searched_km2 else float("nan")


def load_scene(path: Path, window_s: float) -> Scene | None:
    obs, meta = read_detections(path)
    if not obs or not meta.get("footprint") or not meta.get("searched_km2"):
        return None
    t = obs[0].position.t
    try:
        tracks = tracks_at(AIS_STORE, t, window_s=window_s, bbox=AOI_SEA)
    except AISReadError:
        return None
    tracks = in_footprint(tracks, t, meta["footprint"])
    area = SearchedArea.from_json(meta.get("searched_grid"))
    if area is None:
        return None
    tracks, _, _ = split_by_searched(tracks, t, area)
    if not tracks:
        return None
    return Scene(meta.get("scene", path.stem), t.date(), obs, tracks, t,
                 float(meta["searched_km2"]))


def gated(obs, min_snr: float, min_px: int):
    return [o for o in obs
            if o.attributes.get("snr", 0) >= min_snr
            and o.attributes.get("pixels", 0) >= min_px]


def score(scenes: list[Scene], min_snr: float, min_px: int, k: float) -> Outcome:
    head = calibration.Rate()
    unmatched = 0
    km2 = 0.0
    for s in scenes:
        obs = gated(s.obs, min_snr, min_px)
        result = matching.associate(obs, s.tracks, s.t, k=k)
        # Who is scored is fixed by the UNGATED detections, so every gate is
        # compared on the same vessels. See calibration.calibrate.
        cal = calibration.calibrate(
            result, s.tracks, obs, s.t, searched_km2=s.searched_km2, k=k,
            density_per_km2=len(s.obs) / s.searched_km2,
            obs_uncertainty_m=statistics.median(
                o.position.uncertainty_m for o in s.obs))
        head = head + cal.headline
        unmatched += len(result.unmatched_observations)
        km2 += s.searched_km2
    return Outcome(head, unmatched, km2)


def choose(table: dict, min_rate_kept: float) -> tuple[float, int] | None:
    """The pre-registered rule. See the module docstring."""
    base = table[(SNR_GATES[0], PIXEL_GATES[0])].headline.rate
    if base != base:
        return None
    ok = [(g, o) for g, o in table.items()
          if o.headline.rate == o.headline.rate
          and o.headline.rate >= min_rate_kept * base]
    if not ok:
        return None
    return min(ok, key=lambda go: (go[1].per_1000km2, -go[0][0]))[0]


def fmt(o: Outcome) -> str:
    r = o.headline
    lo, hi = r.interval
    rate = "  n/a" if r.scored == 0 else f"{100 * r.rate:4.0f}%"
    rng = "" if r.scored == 0 else f"({100 * lo:.0f}-{100 * hi:.0f})"
    return (f"{r.found:>3}/{r.scored:<3}{rate} {rng:>9}  "
            f"{o.per_1000km2:7.1f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--train", default="2024-09-25")
    ap.add_argument("--test", default="2024-06-21")
    ap.add_argument("--min-rate-kept", type=float, default=0.9,
                    help="keep at least this share of the ungated detection "
                         "rate (default 0.9)")
    ap.add_argument("--k", type=float, default=matching.K_SIGMA)
    ap.add_argument("--window", type=float, default=1800.0)
    args = ap.parse_args()

    train_day, test_day = (date.fromisoformat(args.train),
                           date.fromisoformat(args.test))
    files = sorted(EVENTS.glob("sar-*.geojson"))
    scenes = {train_day: [], test_day: []}
    for f in files:
        d = scene_date(f)
        if d not in scenes:
            continue
        s = load_scene(f, args.window)
        if s is not None:
            scenes[d].append(s)
            print(f"  {d}  {s.name[:48]}  {len(s.obs)} detections, "
                  f"{len(s.tracks)} searched AIS vessels")

    if not scenes[train_day] or not scenes[test_day]:
        print(f"\n  Need scorable scenes on both days; have "
              f"{len(scenes[train_day])} on {train_day} and "
              f"{len(scenes[test_day])} on {test_day}.\n")
        return 1

    train, test = {}, {}
    for snr in SNR_GATES:
        for px in PIXEL_GATES:
            train[(snr, px)] = score(scenes[train_day], snr, px, args.k)
            test[(snr, px)] = score(scenes[test_day], snr, px, args.k)

    L = calibration.DETECTABLE_LENGTH_M
    print(f"\n  detection rate for vessels >= {L:.0f} m, and unmatched "
          f"detections per 1,000 km2 searched\n")
    print(f"  {'gate':>14}   {'TRAIN ' + str(train_day):^30}   "
          f"{'TEST ' + str(test_day):^30}")
    print(f"  {'snr   px':>14}   {'found/scored  rate (95%)  unm/1k':^30}   "
          f"{'found/scored  rate (95%)  unm/1k':^30}")
    pick = choose(train, args.min_rate_kept)
    for g in train:
        mark = "  <-- chosen on TRAIN" if g == pick else ""
        print(f"  {g[0]:>8} {g[1]:>4}   {fmt(train[g]):30}   "
              f"{fmt(test[g]):30}{mark}")

    if pick is None:
        print("\n  No gate kept the training detection rate -- nothing to choose.\n")
        return 1
    b_tr, p_tr = train[(SNR_GATES[0], PIXEL_GATES[0])], train[pick]
    b_te, p_te = test[(SNR_GATES[0], PIXEL_GATES[0])], test[pick]
    print(f"\n  chosen gate: SNR >= {pick[0]}, pixels >= {pick[1]}  "
          f"(rule: strictest gate keeping >= {100 * args.min_rate_kept:.0f}% "
          f"of the ungated TRAIN rate)")
    print(f"    train  unmatched/1k km2 {b_tr.per_1000km2:.1f} -> "
          f"{p_tr.per_1000km2:.1f}, rate {fmt(b_tr).split()[1]} -> "
          f"{fmt(p_tr).split()[1]}")
    print(f"    TEST   unmatched/1k km2 {b_te.per_1000km2:.1f} -> "
          f"{p_te.per_1000km2:.1f}, rate {fmt(b_te).split()[1]} -> "
          f"{fmt(p_te).split()[1]}")
    print("\n  The TEST line is the one that counts: it was not used to choose.")
    print("  Apply the gate with:")
    print(f"    python scripts/match_maritime.py --all --min-snr {pick[0]} "
          f"--min-pixels {pick[1]}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
