"""G4: speeds from transit vehicles, per 250 m cell per local hour.

    python scripts/road_speeds.py                       # the whole archive
    python scripts/road_speeds.py --since 2026-10-27    # from a UTC date

Reads data/raw/roads/vehicles/ (G2c). Writes, under data/interim/roads/speeds/:
    cell-hours/date=YYYY-MM-DD.parquet   one row per (cell, local date, local hour)
    report.json          every fix and pair, counted by what happened to it

WHAT A SPEED IS HERE

Two consecutive fixes of one vehicle, both carrying the vehicle's OWN
timestamp, 15 to 120 seconds apart: distance over time, assigned to the cell
holding the midpoint. That is all. The `speed` field some feeds send is the
vehicle's own claim and is not used. A computed speed and a declared one are
the two layers of this project's thesis, and they are not mixed.

WHY A CELL AND NOT A ROAD SEGMENT

The road layer G3 fetched is AADT **stations**, which are points. Matching
fixes to a road network would add a model (a map-matcher) whose errors land
exactly where the roads are densest, which is near cities and often near state
lines. A 250 m cell is cruder and honest about it. A bus at a stop is slow in
its cell every day, so the comparison that matters is a cell with itself,
hour by hour, not a cell against a speed limit.

WHAT IS EXCLUDED, AND COUNTED

    no_vehicle_ts     a fix aged only by our poll; its time is wrong by its age
    entity_key        the feed sent no vehicle id; the track is not a vehicle
    duplicate         the same (vehicle, timestamp) twice, e.g. across a restart
    out_of_order      a timestamp at or before the previous one
    too_close         under 15 s apart; position noise dominates
    gap               over 120 s apart; the path between is unknown
    impossible        over 60 m/s; a jump in position, not a speed
    parked            no trip at either end and under 5 m moved: a bus out of
                      service in a depot or layover, still reporting

THE PARKED RULE, FOUND ON THE FIRST REAL DAY (2026-10-27). SEPTA's bus feed
gave 1.87 million pairs with a MEDIAN speed of 0.2 m/s. 82 per cent of its
fixes carry no trip: out-of-service buses parked and reporting a fresh
timestamp every poll. They are not traffic. A depot is slow every day, so it
would never be an anomaly, but it would dominate every per-feed figure and
make "stopped" mean "parked". Moving without a trip (deadheading) is still
traffic and is kept; standing still WITH a trip is a dwell at a stop and is
kept too.

Every one is a count in report.json, by feed. A filter nobody can see is the
class of fault docs/reporting-defects.md is a catalogue of.

WHAT THIS FILE DOES NOT DO

It does not split anything by distance to a state line, and it does not flag
anomalies. Both are G6, and the anomaly rule is PROPOSED in
docs/phase-g-prereg.md, to be registered before it is run. `anomalies()` is
here so that it can be tested now. `main` does not call it.

LOCAL TIME

Traffic follows the local clock. Each feed's standard UTC offset is in
FEED_STD_OFFSET_H, and US daylight saving is applied by rule: from the second
Sunday in March to the first Sunday in November, 02:00 local. A feed missing
from the table is a refusal, not a default; DC's clock applied to St. Louis
would shift every hour by one.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from array import array
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import _bootstrap  # noqa: F401  (must precede the angels imports)
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import INTERIM, RAW, REFERENCE

VEHICLES = RAW / "roads" / "vehicles"
SELECTION = REFERENCE / "roads" / "border-feeds.json"
OUT_DIR = INTERIM / "roads" / "speeds"

CELL_DEG = 0.00225               # ~250 m north-south
MIN_DT_S, MAX_DT_S = 15, 120
MAX_SPEED_MPS = 60.0             # ~134 mph; commuter rail tops out below it
BIN_MPS = 0.5
N_BINS = int(MAX_SPEED_MPS / BIN_MPS) + 1
STOPPED_MPS = 0.5
EARTH_R_M = 6_371_008.8

# Standard (winter) UTC offset per selected feed, from the states each serves.
FEED_STD_OFFSET_H: dict[str, int] = {
    **{f: -5 for f in (
        "mdb-1489", "mdb-1530", "mdb-1602", "mdb-1662", "mdb-1695", "mdb-1790",
        "mdb-1850", "mdb-1853", "mdb-2422", "mdb-2665", "mdb-3060", "mdb-3063",
        "mdb-3070", "mdb-3077", "mdb-3080", "mdb-3199", "mdb-3436", "mdb-3443",
        "tld-1127-vp", "tld-4337-vp", "tld-4347-vp", "tld-4451-vp",
        "tld-4532-vp", "tld-4552-vp", "tld-4664-vp", "tld-4679-vp",
        "tld-4785-vp", "tld-4790-vp", "tld-5539-vp", "tld-5903-vp",
        "tld-7022-vp", "tld-7031-vp")},
    **{f: -6 for f in ("mdb-1414", "mdb-1461", "mdb-1683")},  # St. Louis, Duluth, Twin Cities
    **{f: -7 for f in ("mdb-3204", "tld-6744-vp")},           # El Paso, northern New Mexico
    "mdb-1921": -8,                                            # C-TRAN, Vancouver WA
}

REASONS = ("no_vehicle_ts", "entity_key", "duplicate", "out_of_order",
           "too_close", "gap", "impossible", "parked", "late")
PARKED_M = 5.0

# A local date is written out once the files have moved this far past it, so
# a month never sits in memory. 14 h covers the widest offset here (UTC-8)
# plus the flush delay with room to spare. A pair that still lands on a date
# already written is counted as `late`, never silently merged.
FLUSH_LAG_H = 14


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------

def _nth_sunday(year: int, month: int, n: int) -> datetime:
    d = datetime(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def local_time(ts: float, std_offset_h: int) -> datetime:
    """Naive local wall-clock time under the US daylight-saving rule."""
    std = datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None) \
        + timedelta(hours=std_offset_h)
    start = _nth_sunday(std.year, 3, 2) + timedelta(hours=2)
    end = _nth_sunday(std.year, 11, 1) + timedelta(hours=1)   # 02:00 daylight
    return std + timedelta(hours=1) if start <= std < end else std


def check_offsets(feed_ids: Iterable[str]) -> None:
    missing = sorted(set(feed_ids) - set(FEED_STD_OFFSET_H))
    if missing:
        raise SystemExit(f"no UTC offset for {', '.join(missing)}: add them to "
                         f"FEED_STD_OFFSET_H rather than let a default shift "
                         f"their hours")


# --------------------------------------------------------------------------
# space
# --------------------------------------------------------------------------

def cell_of(lat: float, lon: float) -> tuple[int, int]:
    """A ~250 m cell: rows in latitude, columns narrowed by the row's cosine
    so a cell is roughly square at any latitude.

    The rows are therefore STAGGERED: column c of one row is not due north of
    column c of the next. Found by the test that assumed it was. It does not
    matter for anything here, because a cell is only ever compared with
    itself. Any distance between cells is taken between their centres."""
    row = math.floor(lat / CELL_DEG)
    lat_c = (row + 0.5) * CELL_DEG
    col = math.floor(lon * math.cos(math.radians(lat_c)) / CELL_DEG)
    return row, col


def cell_centre(row: int, col: int) -> tuple[float, float]:
    lat = (row + 0.5) * CELL_DEG
    lon = (col + 0.5) * CELL_DEG / math.cos(math.radians(lat))
    return lat, lon


def metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * math.asin(math.sqrt(min(1.0, a)))


# --------------------------------------------------------------------------
# pairs
# --------------------------------------------------------------------------

def pair(a: dict[str, Any], b: dict[str, Any]) -> tuple[float | None, str | None]:
    """(speed in m/s, None) or (None, reason). `a` is the earlier fix."""
    dt = b["vehicle_ts"] - a["vehicle_ts"]
    if dt == 0:
        return None, "duplicate"
    if dt < 0:
        return None, "out_of_order"
    if dt < MIN_DT_S:
        return None, "too_close"
    if dt > MAX_DT_S:
        return None, "gap"
    v = metres(a["lat"], a["lon"], b["lat"], b["lon"]) / dt
    if v > MAX_SPEED_MPS:
        return None, "impossible"
    return v, None


class Cells:
    """Per (cell, local date, local hour): a speed histogram, the vehicles
    and feeds seen.

    MEASURED 2026-10-27: one day is 740,000 cell-hours. As Python lists of
    121 ints, a month would have needed about 20 GB. Histograms are now
    compact arrays, vehicles are small ints, and finished dates are written
    out and dropped (FLUSH_LAG_H), so memory holds about a day and a half."""

    def __init__(self) -> None:
        self.hist: dict[tuple, array] = {}
        self.vehicles: dict[tuple, set] = defaultdict(set)
        self.feeds: dict[tuple, set] = defaultdict(set)

    def add(self, key: tuple, speed: float, vehicle: int, feed: str) -> None:
        h = self.hist.get(key)
        if h is None:
            h = self.hist[key] = array("I", bytes(4 * N_BINS))
        h[min(N_BINS - 1, int(speed / BIN_MPS))] += 1
        self.vehicles[key].add(vehicle)
        self.feeds[key].add(feed)

    def pop_before(self, date_iso: str) -> "Cells":
        """Remove and return every cell-hour on a local date before this one."""
        out = Cells()
        for key in [k for k in self.hist if k[2] < date_iso]:
            out.hist[key] = self.hist.pop(key)
            out.vehicles[key] = self.vehicles.pop(key)
            out.feeds[key] = self.feeds.pop(key)
        return out


def quantile(hist: list[int], q: float) -> float | None:
    """Bin-centre quantile of a histogram."""
    n = sum(hist)
    if n == 0:
        return None
    target = q * (n - 1)
    run = 0
    for i, c in enumerate(hist):
        run += c
        if run > target:
            return (i + 0.5) * BIN_MPS
    return (len(hist) - 0.5) * BIN_MPS


class Stream:
    """Fixes in, pairs out, with every exclusion counted by feed."""

    def __init__(self) -> None:
        self.last: dict[tuple[str, str], dict[str, Any]] = {}
        self.ids: dict[tuple[str, str], int] = {}
        self.flushed_before = ""          # every local date before this is written
        self.cells = Cells()
        self.counts: dict[str, dict[str, int]] = defaultdict(
            lambda: defaultdict(int))
        self.feed_hist: dict[str, list[int]] = defaultdict(lambda: [0] * N_BINS)

    def fix(self, r: dict[str, Any]) -> None:
        feed = r["feed_id"]
        c = self.counts[feed]
        c["fixes"] += 1
        if r["vehicle_key"].startswith("entity:"):
            c["entity_key"] += 1
            return
        if r["vehicle_ts"] is None:
            c["no_vehicle_ts"] += 1
            return
        key = (feed, r["vehicle_key"])
        prev = self.last.get(key)
        if prev is not None and r["vehicle_ts"] <= prev["vehicle_ts"]:
            c["duplicate" if r["vehicle_ts"] == prev["vehicle_ts"]
              else "out_of_order"] += 1
            return
        self.last[key] = r
        if prev is None:
            c["first_fix"] += 1
            return
        v, why = pair(prev, r)
        if why:
            c[why] += 1
            return
        if (not prev.get("trip_id") and not r.get("trip_id")
                and v * (r["vehicle_ts"] - prev["vehicle_ts"]) < PARKED_M):
            c["parked"] += 1
            return
        c["pairs"] += 1
        self.feed_hist[feed][min(N_BINS - 1, int(v / BIN_MPS))] += 1
        mid_lat = (prev["lat"] + r["lat"]) / 2
        mid_lon = (prev["lon"] + r["lon"]) / 2
        t = local_time((prev["vehicle_ts"] + r["vehicle_ts"]) / 2,
                       FEED_STD_OFFSET_H[feed])
        day = t.date().isoformat()
        if day < self.flushed_before:
            c["late"] += 1
            c["pairs"] -= 1
            return
        vid = self.ids.setdefault(key, len(self.ids))
        self.cells.add((*cell_of(mid_lat, mid_lon), day, t.hour), v, vid, feed)


# --------------------------------------------------------------------------
# the anomaly rule, PROPOSED and not run by main (see the module docstring)
# --------------------------------------------------------------------------

def anomalies(rows: list[dict[str, Any]], *, ratio: float, min_pairs: int,
              min_vehicles: int, min_days: int) -> list[dict[str, Any]]:
    """Cell-hours whose median speed is below `ratio` times the same cell's
    usual median at the same local hour on the same kind of day.

    The baseline is the median of that cell-hour's daily medians over the
    window. A cell-hour counts only with `min_pairs` pairs from
    `min_vehicles` vehicles, and a baseline only with `min_days` such days.
    A slow stop is slow every day; this asks for slow against itself."""
    ok = [r for r in rows if r["n_pairs"] >= min_pairs
          and r["n_vehicles"] >= min_vehicles and r["median_mps"] is not None]
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for r in ok:
        groups[(r["cell_row"], r["cell_col"], r["local_hour"],
                r["weekday"] < 5)].append(r)
    out = []
    for members in groups.values():
        if len({m["local_date"] for m in members}) < min_days:
            continue
        meds = sorted(m["median_mps"] for m in members)
        base = meds[len(meds) // 2] if len(meds) % 2 else \
            (meds[len(meds) // 2 - 1] + meds[len(meds) // 2]) / 2
        if base <= 0:
            continue
        for m in members:
            if m["median_mps"] < ratio * base:
                out.append({**m, "baseline_mps": base,
                            "ratio": round(m["median_mps"] / base, 3)})
    return out


# --------------------------------------------------------------------------
# reading and writing
# --------------------------------------------------------------------------

COLUMNS = ["feed_id", "vehicle_key", "trip_id", "lat", "lon", "vehicle_ts",
           "fetched_at"]


def files_since(root: Path, since: str | None) -> list[Path]:
    files = sorted(Path(root).glob("hour=*/positions_*.parquet"))
    if since:
        tag = since.replace("-", "")
        files = [f for f in files if f.parent.name[5:13] >= tag]
    return files


def partition_time(f: Path) -> datetime | None:
    try:
        return datetime.strptime(f.parent.name[5:], "%Y%m%d%H")
    except ValueError:
        return None


def run(files: list[Path], sink=None) -> Stream:
    """Stream the files in order. With a `sink`, each local date is handed to
    it once the files are FLUSH_LAG_H past it, and dropped from memory."""
    import pyarrow.parquet as pq           # noqa: PLC0415
    s = Stream()
    for f in files:
        rows = pq.read_table(f, columns=COLUMNS).to_pylist()
        rows.sort(key=lambda r: (r["feed_id"], r["vehicle_key"],
                                 r["vehicle_ts"] if r["vehicle_ts"] is not None
                                 else -1, r["fetched_at"]))
        for r in rows:
            s.fix(r)
        when = partition_time(f)
        if sink is not None and when is not None:
            cutoff = (when - timedelta(hours=FLUSH_LAG_H)).date().isoformat()
            if cutoff > s.flushed_before:
                done = s.cells.pop_before(cutoff)
                s.flushed_before = cutoff
                if done.hist:
                    sink(done)
    if sink is not None and s.cells.hist:
        sink(s.cells)
        s.cells = Cells()
    return s


def cell_rows(cells: Cells) -> list[dict[str, Any]]:
    out = []
    for key, hist in cells.hist.items():
        row, col, day, hour = key
        lat, lon = cell_centre(row, col)
        n = sum(hist)
        out.append({
            "cell_row": row, "cell_col": col,
            "lat": round(lat, 6), "lon": round(lon, 6),
            "local_date": day, "local_hour": hour,
            "weekday": datetime.fromisoformat(day).weekday(),
            "n_pairs": n, "n_vehicles": len(cells.vehicles[key]),
            "feeds": ",".join(sorted(cells.feeds[key])),
            "median_mps": quantile(hist, 0.5),
            "p25_mps": quantile(hist, 0.25), "p75_mps": quantile(hist, 0.75),
            "share_stopped": round(sum(hist[:int(STOPPED_MPS / BIN_MPS)]) / n, 4),
        })
    return out


def report(s: Stream, files: list[Path], cell_hours: int = 0,
           cells: int = 0) -> dict[str, Any]:
    feeds = {}
    for f, c in sorted(s.counts.items()):
        feeds[f] = {**{k: int(v) for k, v in c.items()},
                    "median_mps": quantile(s.feed_hist[f], 0.5),
                    "p90_mps": quantile(s.feed_hist[f], 0.9)}
    total = defaultdict(int)
    for c in s.counts.values():
        for k, v in c.items():
            total[k] += v
    return {"_what": "G4 speeds: every fix and pair counted by outcome",
            "_built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "_rules": {"cell_deg": CELL_DEG, "min_dt_s": MIN_DT_S,
                       "max_dt_s": MAX_DT_S, "max_speed_mps": MAX_SPEED_MPS,
                       "bin_mps": BIN_MPS,
                       "anomalies": "NOT computed: the rule is PROPOSED in "
                                    "docs/phase-g-prereg.md"},
            "files": len(files),
            "first_file": files[0].parent.name if files else None,
            "last_file": files[-1].parent.name if files else None,
            "totals": dict(total), "by_feed": feeds,
            "cell_hours": cell_hours, "cells": cells}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", help="first UTC date to read, YYYY-MM-DD")
    ap.add_argument("--root", type=Path, default=VEHICLES)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--replace", action="store_true",
                    help="remove an earlier run's output first")
    args = ap.parse_args(argv)

    sel = json.loads(SELECTION.read_text(encoding="utf-8"))
    check_offsets(r["id"] for r in sel["selected"])
    files = files_since(args.root, args.since)
    if not files:
        print("  no vehicle files; is roads-vehicles running")
        return 1
    print(f"  {len(files)} parquet file(s), {files[0].parent.name} to "
          f"{files[-1].parent.name}")
    import pyarrow as pa                    # noqa: PLC0415
    import pyarrow.parquet as pq            # noqa: PLC0415
    parts = args.out_dir / "cell-hours"
    old = sorted(parts.glob("date=*.parquet")) if parts.exists() else []
    if old and not args.replace:
        print(f"  {parts} already holds {len(old)} date(s) from an earlier run. "
              f"Pass --replace to rebuild; mixing two runs' dates in one "
              f"directory would make a table no single run produced.")
        return 1
    for p in old:
        p.unlink()
    parts.mkdir(parents=True, exist_ok=True)
    seen_cells: set = set()
    n_rows = 0

    def sink(done: Cells) -> None:
        nonlocal n_rows
        by_day: dict[str, list] = defaultdict(list)
        for row in cell_rows(done):
            by_day[row["local_date"]].append(row)
            seen_cells.add((row["cell_row"], row["cell_col"]))
        for day, rows in by_day.items():
            pq.write_table(pa.Table.from_pylist(rows),
                           parts / f"date={day}.parquet", compression="zstd")
            n_rows += len(rows)
            print(f"    {day}: {len(rows):,} cell-hours")

    s = run(files, sink)
    rep = report(s, files, n_rows, len(seen_cells))
    (args.out_dir / "report.json").write_text(json.dumps(rep, indent=1),
                                              encoding="utf-8")
    t = rep["totals"]
    print(f"  {t.get('fixes', 0):,} fixes -> {t.get('pairs', 0):,} speed pairs "
          f"in {rep['cells']:,} cells, {rep['cell_hours']:,} cell-hours")
    for k in REASONS:
        if t.get(k):
            print(f"    excluded {k:14s} {t[k]:>10,}")
    print(f"  wrote {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
