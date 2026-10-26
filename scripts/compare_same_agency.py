"""Are two feeds from one agency the same vehicles? Ask the vehicles.

    python scripts/compare_same_agency.py                 # every provisional pair
    python scripts/compare_same_agency.py --rounds 5 --gap 30

G2b kept ONE feed from each of two agencies published twice -- Virginia
Railway Express from two hosts, Pioneer Valley from two vendors -- because a
doubled source halves every inter-report gap and doubles every count without a
single error, while a missed one only shows as fewer vehicles. Both decisions
are marked PROVISIONAL in `SAME_AGENCY` until this comparison is run.

The pairs are read from `select_border_feeds.SAME_AGENCY` (every entry whose
decision is "keep:..."), and the URLs from G2b's own artefact. Nothing is
restated here.

WHAT COUNTS AS THE SAME VEHICLES, IN ORDER

    shared vehicle ids        the strongest: one fleet, one numbering
    shared labels             two vendors often number differently but carry
                              the fleet number the public sees
    shared positions          a vehicle in A within MATCH_M of one in B at the
                              same moment, for most of the smaller fleet

Each test is reported; the verdict is the first that holds for at least half
of the smaller fleet. **A pair where neither feed reports a vehicle is
UNDECIDED, not "different"** -- VRE runs weekday peaks only, and an empty
comparison at 21:00 on a Sunday says nothing about whether the feeds agree.

Writes `data/reference/roads/same-agency-<UTC stamp>.json`. Positions are
compared in memory and not written; the report keeps counts and the verdict.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import _bootstrap  # noqa: F401  (must precede the angels imports)
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import REFERENCE
from scripts.fetch_vehicles import (ANSWERED, SELECTION, feed_row, fetch,
                                    key_for, need_bindings)
from scripts.select_border_feeds import SAME_AGENCY

OUT_DIR = REFERENCE / "roads"
MATCH_M = 200.0
MAJORITY = 0.5

SAME_BY_ID = "SAME VEHICLES by id"
SAME_BY_LABEL = "SAME VEHICLES by label"
SAME_BY_POSITION = "SAME VEHICLES by position"
DIFFERENT = "DIFFERENT vehicles"
UNDECIDED_SILENT = "UNDECIDED: neither feed reports a vehicle right now"
UNDECIDED_ONE_EMPTY = "UNDECIDED: only one feed reports vehicles right now"
UNDECIDED_NO_ANSWER = "UNDECIDED: a feed did not answer"
VERDICTS = (SAME_BY_ID, SAME_BY_LABEL, SAME_BY_POSITION, DIFFERENT,
            UNDECIDED_SILENT, UNDECIDED_ONE_EMPTY, UNDECIDED_NO_ANSWER)


def provisional_pairs(table: dict = SAME_AGENCY) -> list[tuple[str, str, str]]:
    """(id_a, id_b, kept) for every pair decided by keeping one."""
    out = []
    for ids, (decision, _why) in table.items():
        if decision.startswith("keep:") and len(ids) == 2:
            out.append((ids[0], ids[1], decision.split(":", 1)[1]))
    return out


def urls_from(selection: dict) -> dict[str, dict]:
    return {r["id"]: r for r in selection.get("selected", [])
            + selection.get("excluded", [])}


def metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def compare(a: list[dict], b: list[dict]) -> dict[str, Any]:
    """Counts for one simultaneous pair of polls. Pure."""
    ids_a = {r["vehicle_id"] for r in a if r["vehicle_id"]}
    ids_b = {r["vehicle_id"] for r in b if r["vehicle_id"]}
    lab_a = {r["label"] for r in a if r["label"]}
    lab_b = {r["label"] for r in b if r["label"]}
    near = 0
    gaps = []
    for ra in a:
        best = min((metres(ra["lat"], ra["lon"], rb["lat"], rb["lon"])
                    for rb in b), default=None)
        if best is not None:
            gaps.append(best)
            if best <= MATCH_M:
                near += 1
    return {
        "n_a": len(a), "n_b": len(b),
        "shared_ids": len(ids_a & ids_b),
        "shared_labels": len(lab_a & lab_b),
        "near_within_m": near,
        "nearest_m_median": round(statistics.median(gaps), 1) if gaps else None,
    }


def verdict(c: dict[str, Any], answered: bool = True) -> str:
    if not answered:
        return UNDECIDED_NO_ANSWER
    small = min(c["n_a"], c["n_b"])
    if c["n_a"] == 0 and c["n_b"] == 0:
        return UNDECIDED_SILENT
    if small == 0:
        return UNDECIDED_ONE_EMPTY
    if c["shared_ids"] >= MAJORITY * small:
        return SAME_BY_ID
    if c["shared_labels"] >= MAJORITY * small:
        return SAME_BY_LABEL
    if c["near_within_m"] >= MAJORITY * small:
        return SAME_BY_POSITION
    return DIFFERENT


def poll_pair(fa: dict, fb: dict, pb2) -> tuple[dict, dict, list, list]:
    def one(f):
        status, body, el, err, trunc = fetch(f["url"], key_for(f["url"]))
        rec, rows, _ = feed_row(f, status=status, body=body, elapsed_s=el,
                                error=err, truncated=trunc,
                                fetched_at=int(time.time()), previous=None,
                                pb2=pb2)
        return rec, rows
    with ThreadPoolExecutor(max_workers=2) as pool:
        (ra, rows_a), (rb, rows_b) = pool.map(one, (fa, fb))
    return ra, rb, rows_a, rows_b


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--gap", type=float, default=30.0,
                    help="seconds between rounds")
    ap.add_argument("--selection", type=Path, default=SELECTION)
    args = ap.parse_args(argv)

    pb2 = need_bindings()
    if pb2 is None:
        print("gtfs-realtime-bindings is not installed: .\\tasks.ps1 roads")
        return 3
    sel = json.loads(args.selection.read_text(encoding="utf-8"))
    rows_by_id = urls_from(sel)
    report: dict[str, Any] = {
        "_what": "vehicle comparison of same-agency feed pairs (G2b SAME_AGENCY)",
        "_built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "_rules": {"match_m": MATCH_M, "majority_of_smaller_fleet": MAJORITY},
        "pairs": [],
    }
    for id_a, id_b, kept in provisional_pairs():
        fa, fb = rows_by_id[id_a], rows_by_id[id_b]
        rounds = []
        for k in range(args.rounds):
            ra, rb, rows_a, rows_b = poll_pair(fa, fb, pb2)
            answered = ra["outcome"] in ANSWERED and rb["outcome"] in ANSWERED
            c = compare(rows_a, rows_b)
            c.update(outcome_a=ra["outcome"], outcome_b=rb["outcome"],
                     verdict=verdict(c, answered))
            rounds.append(c)
            print(f"  {id_a} vs {id_b}, round {k + 1}: {c['verdict']}  "
                  f"({c['n_a']} / {c['n_b']} vehicles, {c['shared_ids']} ids, "
                  f"{c['shared_labels']} labels, {c['near_within_m']} within "
                  f"{MATCH_M:.0f} m; outcomes {c['outcome_a']}/{c['outcome_b']})")
            if k + 1 < args.rounds:
                time.sleep(args.gap)
        said = {r["verdict"] for r in rounds}
        decided = said - {UNDECIDED_SILENT, UNDECIDED_ONE_EMPTY,
                          UNDECIDED_NO_ANSWER}
        overall = (decided.pop() if len(decided) == 1 else
                   "INCONSISTENT across rounds: " + ", ".join(sorted(decided))
                   if decided else "UNDECIDED in every round")
        report["pairs"].append({"a": id_a, "b": id_b, "kept": kept,
                                "provider": fa.get("provider"),
                                "rounds": rounds, "overall": overall})
        print(f"{fa.get('provider')}: {overall}  (kept {kept})")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"same-agency-{datetime.now(timezone.utc):%Y-%m-%dT%H%M%SZ}.json"
    path.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
