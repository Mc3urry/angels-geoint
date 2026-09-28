"""Draw a blind subset for a second reader, and pre-register the analysis.

    python scripts/draw_review.py --who joshua --n 40
    python scripts/draw_review.py --who joshua --n 40 --dry-run

WHY THIS EXISTS

All 147 chip verdicts in labels.jsonl were made by one reader, and not by the
author of the study. That is the largest single weakness in the evidence
chain and it is not a technical one: no amount of further checking by the
same reader can fix it.

Re-reading all 147 would not fix it either -- it would replace one solo read
with another. What fixes it is a SECOND reader on a subset, and a published
agreement rate. "An AI labelled my data" becomes "the labelling was validated
by a second reader at kappa = x, and here are the disagreements", which is a
stronger methods claim than a solo read of any size.

THE SAMPLE IS SIMPLE RANDOM, DELIBERATELY

Not stratified. Stratifying on the first reader's own verdicts would bias the
agreement estimate in whichever direction the strata were chosen -- oversample
the classes that were hard and disagreement inflates, oversample the easy ones
and it deflates. An honest kappa needs the subset's marginal distribution to
match the population it is validating. So: simple random without replacement,
from a fixed seed, recorded here.

THE FIRST READER'S VERDICTS ARE NOT IN THIS FILE

The session carries keys and nothing else. The queue serves the same blinded
context the first read had -- strength, size, length, date, reception -- and
withholds the same things: distance to the nearest limit, the stratum, and
the chipper's heuristic guess. A second read that can see the first is not a
second read.

WHAT HAPPENS TO DISAGREEMENTS, DECIDED NOW

They are REPORTED, not resolved. This session measures; it does not relabel.
Deciding after the fact to adopt whichever verdict looks better would use the
same data twice -- once to validate the labels and once to change them -- and
the kappa would no longer describe anything. If the labels should change on
the strength of what the second reader saw, that is a separate step, declared
separately, and the agreement figure published here is the one measured
BEFORE it.

If agreement comes out poor, that is a finding about the labels and it gets
published as one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import EVENTS  # noqa: E402

LABELS = EVENTS / "labels.jsonl"
SESSION = EVENTS / "review-session.json"

PROTOCOL = [
    "Read each chip and give one verdict: vessel, fixed, clutter, ambiguous.",
    "vessel   = a compact bright cluster at the crosshair, separable from "
    "speckle, with a coherent shape.",
    "fixed    = that, plus a repeat coordinate or a known structure.",
    "clutter  = no coherent compact target at the crosshair.",
    "ambiguous= something is there but it is not separable. Use it. A reader "
    "who never says ambiguous is a reader whose hard cases became whichever "
    "label was easiest.",
    "Do not look at labels.jsonl, FINDINGS.md or the chip heuristic before "
    "finishing. The whole value of this is that it is independent.",
    "Stop when the queue is empty. Do not go back and revise: a revised "
    "verdict made after seeing later chips is a different measurement.",
]

ANALYSIS = [
    "Raw agreement, as a percentage of the subset.",
    "Cohen's kappa over the four categories, with a 95% interval.",
    "The full confusion matrix, both readers' marginals shown.",
    "Every disagreement listed individually, with both verdicts and both notes.",
    "No relabelling. This session measures; changing a label on the strength "
    "of it is a separate and separately declared step.",
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--who", required=True, help="the second reader's name")
    ap.add_argument("--n", type=int, default=40,
                    help="subset size. 40 is about an hour's reading and "
                         "gives kappa to roughly +-0.2 at n=40; smaller is "
                         "cheaper and correspondingly vaguer")
    ap.add_argument("--seed", type=int, default=20260927)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not LABELS.exists():
        print(f"\n  {LABELS} does not exist; nothing to validate.\n")
        return 1
    raw = LABELS.read_bytes()
    first: dict[str, str] = {}
    for line in raw.decode("utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            if rec.get("key"):
                first[rec["key"]] = rec["verdict"]

    keys = sorted(first)
    if args.n > len(keys):
        print(f"\n  only {len(keys)} labelled chips exist; asked for "
              f"{args.n}.\n")
        return 1

    picked = sorted(random.Random(args.seed).sample(keys, args.n))
    out_name = f"labels-review-{args.who}.jsonl"

    doc = {
        "who": args.who,
        "n": args.n,
        "seed": args.seed,
        "drawn_at": datetime.now(timezone.utc)
        .replace(microsecond=0).isoformat(),
        "population": len(keys),
        # The exact first-reader state this subset validates. If labels.jsonl
        # changes before the comparison runs, the comparison is against a
        # different thing and review_agreement.py will say so.
        "labels_sha256": hashlib.sha256(raw).hexdigest(),
        "method": "simple random without replacement; NOT stratified, so the "
                  "subset's marginal distribution matches the population and "
                  "the agreement estimate is unbiased",
        "out": out_name,
        "protocol": PROTOCOL,
        "preregistered_analysis": ANALYSIS,
        # Deliberately absent: the first reader's verdicts. Putting them here
        # would make the blinding depend on the reader not opening a file.
        "keys": picked,
    }

    if args.dry_run:
        print(json.dumps({k: v for k, v in doc.items() if k != "keys"},
                         indent=1))
        print(f"\n  would draw {len(picked)} of {len(keys)}; "
              f"--dry-run, nothing written\n")
        return 0

    if SESSION.exists():
        print(f"\n  {SESSION.name} already exists. A second draw would "
              f"replace an open session\n  and invalidate whatever has been "
              f"read into it. Finish or move it first.\n")
        return 1

    SESSION.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"\n  drew {len(picked)} of {len(keys)} chips, seed {args.seed}")
    print(f"  session  -> {SESSION}")
    print(f"  verdicts -> {EVENTS / out_name}  (labels.jsonl is not written "
          f"while this session is open)")
    print("\n  protocol:")
    for line in PROTOCOL:
        print(f"    - {line}")
    print("\n  pre-registered analysis, fixed before the first chip is read:")
    for line in ANALYSIS:
        print(f"    - {line}")
    print(f"\n  next:  .\\tasks.ps1 serve   then open web/label.html\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
