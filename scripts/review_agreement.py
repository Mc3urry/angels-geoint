"""Compare a second reader's verdicts with the first reader's, once.

    python scripts/review_agreement.py
    python scripts/review_agreement.py --partial     # explicitly, and it says so

WHAT THIS IS FOR

All 147 chip verdicts were made by one reader who is not the author of the
study. This computes what a second, blind reader agreed on: raw agreement,
Cohen's kappa with an interval, the confusion matrix, and every disagreement
in full.

IT REFUSES TO RUN EARLY, AND THAT IS THE POINT

A partial agreement figure, looked at while chips remain, is not a
measurement -- it is feedback. A reader who learns after fifteen chips that
they are running at 60% has been told to read differently, and whatever the
final number is, it no longer describes an independent read. So this stops
unless the session is complete. `--partial` exists because a flat refusal
invites a one-line script that does the same arithmetic unlabelled; it prints
the figures with the contamination stated on every one.

KAPPA, NOT ACCURACY

Raw agreement on four categories where one holds 60% of the mass is flattered
by chance: two readers assigning labels at random in the observed proportions
would agree a good deal of the time. Cohen's kappa subtracts that. Both are
printed, because kappa is unintuitive and raw agreement is what a reader
expects to see first.

The interval is the standard normal approximation on kappa's asymptotic
variance. At n = 40 it is wide -- roughly +-0.2 -- and that width is a fact
about the exercise, not a flaw in it: it is the price of an hour's reading
rather than six. Reporting kappa without its interval would be the same
defect this project has catalogued sixteen times.

DISAGREEMENTS ARE REPORTED, NOT RESOLVED

Fixed before the first chip was read, in draw_review.py, and restated here:
this measures, it does not relabel. Adopting whichever verdict looks better
afterwards would use the same reading twice -- once to validate the labels
and once to change them -- and the kappa would describe nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import EVENTS  # noqa: E402

LABELS = EVENTS / "labels.jsonl"
SESSION = EVENTS / "review-session.json"
CATS = ("vessel", "fixed", "clutter", "ambiguous")


def last_per_key(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("key"):
                out[rec["key"]] = rec
    return out


def kappa(pairs: list[tuple[str, str]]) -> tuple[float, float, float, float]:
    """Cohen's kappa and a 95% normal interval. Returns (po, pe, k, se)."""
    n = len(pairs)
    if n == 0:
        return (float("nan"),) * 4
    po = sum(1 for a, b in pairs if a == b) / n
    ma = Counter(a for a, _ in pairs)
    mb = Counter(b for _, b in pairs)
    pe = sum((ma[c] / n) * (mb[c] / n) for c in set(ma) | set(mb))
    if pe >= 1.0:
        # Both readers used one category for everything. Kappa is undefined:
        # there is no chance-agreement baseline to subtract from.
        return po, pe, float("nan"), float("nan")
    k = (po - pe) / (1 - pe)
    se = math.sqrt(po * (1 - po) / (n * (1 - pe) ** 2))
    return po, pe, k, se



def _report(title: str, pairs: list[tuple[str, str]], sess: dict) -> None:
    po, pe, k, se = kappa(pairs)
    print(f"\n  {title}")
    print(f"    raw agreement    {po * 100:5.1f}%      "
          f"chance {pe * 100:4.1f}%")
    if k == k:
        print(f"    Cohen's kappa    {k:5.3f}   95% "
              f"[{k - 1.96 * se:.3f}, {k + 1.96 * se:.3f}]   {_gloss(k)}")
    else:
        print("    Cohen's kappa    undefined -- one category carries "
              "everything")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--partial", action="store_true",
                    help="report on an unfinished session, labelled as such")
    args = ap.parse_args()

    if not SESSION.exists():
        print(f"\n  no {SESSION.name}. Draw one with "
              f"scripts/draw_review.py first.\n")
        return 1
    sess = json.loads(SESSION.read_text(encoding="utf-8"))
    review = last_per_key(EVENTS / sess["out"])
    keys = list(sess["keys"])
    read = [k for k in keys if k in review]

    print(f"\n  session: {sess['who']}, {len(keys)} chips drawn with seed "
          f"{sess['seed']} from {sess['population']} labelled")
    print(f"  read:    {len(read)} of {len(keys)}")

    if len(read) < len(keys) and not args.partial:
        print(f"\n  {len(keys) - len(read)} chip(s) still unread. Refusing to "
              f"compute agreement.\n  A partial figure seen mid-read is "
              f"feedback, not a measurement: it tells the\n  reader how they "
              f"are doing while they still have chips to judge, and whatever\n"
              f"  the final number is it no longer describes an independent "
              f"read.\n\n  Finish the queue, or pass --partial to print it "
              f"with that stated.\n")
        return 2

    raw = LABELS.read_bytes()
    now = hashlib.sha256(raw).hexdigest()
    if now != sess.get("labels_sha256"):
        print(f"\n  WARNING: labels.jsonl has changed since this subset was "
              f"drawn.\n    at draw   {sess.get('labels_sha256')}\n"
              f"    now       {now}\n  The comparison below is against the "
              f"CURRENT first-reader verdicts, which are\n  not the ones the "
              f"subset was drawn from. Say so if you publish it.")

    first = last_per_key(LABELS)
    pairs, rows = [], []
    for k in read:
        a = first.get(k, {}).get("verdict")
        b = review[k].get("verdict")
        if a is None:
            continue
        pairs.append((a, b))
        rows.append((k, a, b, first.get(k, {}).get("note"),
                     review[k].get("note")))

    if args.partial:
        print("\n  *** PARTIAL: this session is not finished. Every figure "
              "below is contaminated\n  *** by being computable before the "
              "read was complete. Do not publish it.")

    if sess.get("amendments"):
        print("\n  amendments to the plan, declared before the read:")
        for am in sess["amendments"]:
            print(f"    {am['at']}  {am['what']}")

    # The collapsed figure is primary, and the reason is not a preference.
    # `fixed` in the first read came from cross-date persistence, not from
    # the chip, so a second reader with one image in front of them cannot
    # reach it. Charging them for that measures a definitional gap neither
    # reader chose. Merged, both readers are answering the same question:
    # is a real target present.
    merged = [("vessel" if a == "fixed" else a,
               "vessel" if b == "fixed" else b) for a, b in pairs]
    _report("three-way (fixed merged into vessel) -- PRIMARY", merged, sess)
    _report("four-way, as labelled", pairs, sess)

    po, pe, k, se = kappa(pairs)
    print(f"\n  n = {len(pairs)}")
    print(f"  raw agreement      {po * 100:5.1f}%")
    print(f"  chance agreement   {pe * 100:5.1f}%   (both readers' marginals)")
    if k == k:
        lo, hi = k - 1.96 * se, k + 1.96 * se
        print(f"  Cohen's kappa      {k:5.3f}   95% [{lo:.3f}, {hi:.3f}]")
        print(f"                     {_gloss(k)}")
    else:
        print("  Cohen's kappa      undefined -- one category carries "
              "everything, so there is\n                     no chance "
              "baseline to subtract")

    used = [c for c in CATS if any(a == c or b == c for a, b in pairs)]
    print(f"\n  confusion, rows = first reader, columns = {sess['who']}")
    print("    " + " " * 11 + "".join(f"{c:>11}" for c in used) + f"{'total':>9}")
    m = Counter(pairs)
    for r in used:
        cells = "".join(f"{m[(r, c)]:>11}" for c in used)
        print(f"    {r:11}{cells}{sum(m[(r, c)] for c in used):>9}")
    print("    " + f"{'total':11}"
          + "".join(f"{sum(m[(r, c)] for r in used):>11}" for c in used)
          + f"{len(pairs):>9}")

    bad = [t for t in rows if t[1] != t[2]]
    print(f"\n  {len(bad)} disagreement(s), listed in full because a rate "
          f"without its cases is not evidence:")
    for key, a, b, na, nb in bad:
        print(f"\n    {key.split('/')[-1][:-4]}")
        print(f"      first  {a:10} {(na or '')[:70]}")
        print(f"      {sess['who']:6} {b:10} {(nb or '')[:70]}")
    if not bad:
        print("    none.")

    print("\n  Pre-registered: these are reported, not resolved. Changing a "
          "label on the\n  strength of this comparison is a separate step, "
          "declared separately.\n")
    return 0


def _gloss(k: float) -> str:
    """Landis and Koch's bands, named as the convention they are."""
    for lo, name in ((0.81, "almost perfect"), (0.61, "substantial"),
                     (0.41, "moderate"), (0.21, "fair"), (0.0, "slight")):
        if k >= lo:
            return f"{name} agreement, on Landis & Koch's conventional bands"
    return "worse than chance"


if __name__ == "__main__":
    raise SystemExit(main())
