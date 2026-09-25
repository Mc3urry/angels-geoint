"""The labelling surface: a human verdict on each sampled candidate.

    GET  /labels/queue          what to read, in a stable interleaved order
    GET  /labels/chip/{key}     the image
    POST /labels                one verdict, appended
    GET  /labels/progress       how far through, per stratum

WHY THIS IS A ROUTE AND NOT A FOLDER OF PNGs

Two hundred images in a directory is not a method. It produces verdicts that
drift with fatigue, no record of what was decided when, and no way to stop
halfway and resume without losing your place. The archive this project keeps
about its own collectors is more careful than that, and the labels are more
load-bearing than the heartbeats: they are the evidence under the clutter
rate, and the training data the inversion classifier cannot exist without.

So verdicts are appended to data/events/labels.jsonl as they are made --
one line, flushed, with a timestamp -- and the queue skips whatever is
already there.

WHAT THE READER IS SHOWN, AND WHAT THEY ARE NOT

Shown: the chip, and the measurements that bear on "is this a vessel" --
strength, size in pixels, approximate length, the date, and whether the
water has AIS reception at all.

NOT shown, deliberately:

  distance to the nearest maritime limit    the variable under test
  which stratum the candidate was drawn for  the same thing, restated
  the chipper's own first reading            an anchor

The first two are the whole point. The question being answered is "is there a
vessel here", which does not require knowing how far the point sits from a
legal line -- and a reader who knows a chip is half a mile inside the
territorial sea is a reader who has been told which answer would be
interesting. The boundary result is a comparison of rates across exactly
that variable; letting it reach the person generating the labels would put
the hypothesis inside its own measurement.

The third matters just as much. `inspect_candidates.py` prints a heuristic
guess -- "bright target at the candidate", "nothing bright" -- and on the
2026-09-25 draw it called 61 of 147 clutter. Showing that guess beside the
image would convert a human reading into agreement with a threshold.
"""

from __future__ import annotations

import csv
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from angels.config import EVENTS, INTERIM

router = APIRouter(prefix="/labels", tags=["labels"])

CHIPS = INTERIM / "candidates"
SAMPLE = EVENTS / "label-sample.json"
LABELS = EVENTS / "labels.jsonl"

# Fixed, and fixed BEFORE the first image is read. A vocabulary settled
# afterwards is a vocabulary fitted to what was seen.
#
# `ambiguous` is not a convenience. Without it every hard case silently
# becomes whichever label is easiest to justify, and the rate that comes out
# is a rate of easy cases.
VERDICTS = ("vessel", "fixed", "clutter", "ambiguous")

# Interleave the strata. Reading all the near-line candidates in a row, then
# all the offshore ones, puts fatigue and stratum on the same axis -- and
# fatigue would then look like a difference between bands.
SHUFFLE_SEED = 20260925


def _rows() -> dict[str, dict]:
    """The chipper's CSV, keyed by chip path. Measurements only."""
    out: dict[str, dict] = {}
    csv_path = CHIPS / "candidates.csv"
    if not csv_path.exists():
        return out
    with csv_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            png = (row.get("png") or "").replace("\\", "/")
            if png:
                out[png] = row
    return out


# Chip positions are matched to sampled candidates by DISTANCE, not by a
# rounded key.
#
# The first version keyed on lat/lon rounded to 4 dp and silently lost 14 of
# 147: the chipper's CSV has already rounded to 5 dp, and rounding a 5-dp
# value again to 4 crosses a tie boundary about a tenth of the time. The
# symptom was a queue of 133 with no error anywhere -- a stratified sample
# quietly missing a tenth of its draw, which is the one thing a stratified
# sample must not be.
#
# A metre is far tighter than any two distinct detections are ever apart and
# far looser than any rounding.
MATCH_TOLERANCE_M = 1.0


def _nearest(lat: float, lon: float, rows: dict[str, dict]):
    """The chip row at this position, or None if nothing is within a metre."""
    best, best_d = None, None
    for key, r in rows.items():
        try:
            dlat = (float(r["lat"]) - lat) * 111_320.0
            dlon = ((float(r["lon"]) - lon) * 111_320.0
                    * math.cos(math.radians(lat)))
        except (TypeError, ValueError):
            continue
        d = math.hypot(dlat, dlon)
        if best_d is None or d < best_d:
            best, best_d = (key, r), d
    return best if best_d is not None and best_d <= MATCH_TOLERANCE_M else None


def _queue() -> list[dict]:
    if not SAMPLE.exists():
        raise HTTPException(404, "No label-sample.json. Run "
                                 "scripts/sample_candidates.py first.")
    drawn = json.loads(SAMPLE.read_text(encoding="utf-8"))["sample"]
    rows = _rows()

    items = []
    for rec in drawn:
        hit = _nearest(rec["lat"], rec["lon"], rows)
        if hit is None:
            continue                      # no chip for it; nothing to read
        png, row = hit
        items.append({
            "key": png,
            "date": rec.get("date"),
            "snr": rec.get("snr"),
            "pixels": rec.get("pixels"),
            "length_m_approx": rec.get("length_m_approx"),
            "reception": rec.get("reception"),
            "ais_cell_vessels": rec.get("ais_cell_vessels"),
            "background_dn": row.get("background_dn"),
            # lon/lat travel so a verdict can be tied back to a candidate;
            # the page does not display them, and they say nothing about the
            # variable under test.
            "lon": rec["lon"], "lat": rec["lat"],
        })
    random.Random(SHUFFLE_SEED).shuffle(items)
    return items


def _done() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not LABELS.exists():
        return out
    for line in LABELS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue                      # a torn final line, not a reason to stop
        if rec.get("key"):
            out[rec["key"]] = rec         # last verdict for a key wins
    return out


@router.get("/queue")
def queue(include_done: bool = False) -> dict:
    items = _queue()
    done = _done()
    todo = [i for i in items if include_done or i["key"] not in done]
    return {
        "verdicts": list(VERDICTS),
        "total": len(items),
        "labelled": len(done),
        "remaining": len(items) - len(done),
        "items": todo,
    }


@router.get("/chip/{key:path}")
def chip(key: str):
    p = (CHIPS / key).resolve()
    if CHIPS.resolve() not in p.parents or not p.exists():
        raise HTTPException(404, "no such chip")
    return FileResponse(p, media_type="image/png")


class Verdict(BaseModel):
    key: str
    verdict: str
    note: str | None = None
    lon: float | None = None
    lat: float | None = None


@router.post("")
def label(v: Verdict) -> dict:
    if v.verdict not in VERDICTS:
        raise HTTPException(400, f"verdict must be one of {VERDICTS}")
    rec = {
        "key": v.key,
        "verdict": v.verdict,
        "note": (v.note or "").strip() or None,
        "lon": v.lon, "lat": v.lat,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    LABELS.parent.mkdir(parents=True, exist_ok=True)
    # Append and flush per verdict. A labelling session that loses the last
    # forty judgements to a crash is a labelling session done twice.
    with LABELS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
        fh.flush()
    done = _done()
    return {"ok": True, "labelled": len(done),
            "remaining": max(0, len(_queue()) - len(done))}


@router.get("/progress")
def progress() -> dict:
    """Counts by verdict, and by the stratum the reader never saw.

    The join happens HERE, after the fact, which is the only place it can
    happen without the strata reaching the person making the judgements.
    """
    done = _done()
    drawn = (json.loads(SAMPLE.read_text(encoding="utf-8"))["sample"]
             if SAMPLE.exists() else [])
    rows = _rows()

    tally: dict[str, int] = {}
    per_stratum: dict[str, dict[str, int]] = {}
    for rec in drawn:
        hit = _nearest(rec["lat"], rec["lon"], rows)
        got = done.get(hit[0]) if hit else None
        if not got:
            continue
        s = rec.get("stratum", {})
        name = f"{s.get('band','?')} / {s.get('strength','?')}"
        tally[got["verdict"]] = tally.get(got["verdict"], 0) + 1
        per_stratum.setdefault(name, {})
        per_stratum[name][got["verdict"]] = \
            per_stratum[name].get(got["verdict"], 0) + 1

    return {"labelled": len(done), "by_verdict": tally,
            "by_stratum": per_stratum}
