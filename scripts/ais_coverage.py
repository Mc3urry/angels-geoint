"""Measure how far offshore this AIS dataset can hear, and score candidates.

    python scripts/ais_coverage.py                 # whole days, pooled
    python scripts/ais_coverage.py --window 1800   # only around each pass

Builds a reception grid from the clipped AIS itself -- for the same
half-hour windows the radar passes fall in -- and writes

    data/interim/coverage/reception.json        the grid
    data/events/candidates-scored.geojson       candidates + reception class

WHY THIS COMES BEFORE ANY DARK-VESSEL COUNT

MarineCadastre's bulk AIS comes from SHORE-BASED receivers. AOI_SEA reaches
about 300 km offshore; VHF from a coastal antenna does not. Out there a
vessel broadcasting normally produces no report, and the pipeline cannot
tell that from a vessel that switched its transponder off. Every candidate
in unheard water is therefore uninterpretable -- not evidence of hiding, and
not evidence of honesty either.

The method is in angels/adapters/maritime/coverage.py. In one line: in water
a receiver covers, consecutive reports from one vessel arrive about a minute
apart; where coverage fades, the same vessels report in bursts with long
silences.

READ THE OUTPUT AS A LIMIT ON THE STUDY AREA, NOT AS A RESULT

If reception stops at 60 nm, then the 3, 12 and 24 nm limits -- the
governance discontinuities this project is about -- are all comfortably
inside heard water, and the offshore part of the box is simply not
answerable with this data. That is a fact about the study area worth
stating in the methods, not a disappointment.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.adapters.maritime import coverage
from angels.adapters.maritime.coverage import HEARD, ReceptionGrid
from angels.config import AOI_SEA, AOI_SEA_CONUS, EVENTS, INTERIM, RAW

# WHICH STORE, AND THEREFORE WHICH BOX AND WHICH RECEIVERS.
#
# The bulk store is MarineCadastre: shore-based receivers, months late. The
# live stores are aisstream, collected by this project's own collectors in
# the same schema -- which is the whole reason a second reader was never
# needed. Measuring the two and comparing them is the point: a real-time
# dark-vessel claim needs the LIVE feed's reception envelope, and there is no
# reason to assume it matches the bulk one.
STORES = {
    "maritime":            (RAW / "maritime",            AOI_SEA,       "bulk, shore-based"),
    "maritime-live":       (RAW / "maritime-live",       AOI_SEA,       "live, aisstream"),
    "maritime-live-conus": (RAW / "maritime-live-conus", AOI_SEA_CONUS, "live, aisstream"),
}

OUT = INTERIM / "coverage"


def scene_times() -> list[datetime]:
    """The acquisition instants, from the detection files."""
    out = []
    for p in sorted(EVENTS.glob("sar-*.geojson")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            t = doc["properties"].get("acquired")
            if t:
                out.append(datetime.fromisoformat(t))
        except (OSError, ValueError, KeyError):
            continue
    return sorted(set(out))


def day_groups(store: Path) -> list[tuple[str, list[Path]]]:
    """Every parquet file under the store, GROUPED BY DAY.

    THE GROUPING IS LOAD-BEARING, and it is the one thing that differs
    between the two kinds of store.

    ReceptionGrid measures how long one vessel goes between reports, so the
    stream it is fed has to be continuous for as long as that measurement is
    meant to span. The bulk store writes one file per day, so feeding it a
    file at a time was already correct. The LIVE store writes a file every
    20,000 rows or two minutes -- feeding those one at a time would cut every
    vessel's history into two-minute slices, and almost every gap the grid
    exists to measure would fall across a join and never be seen. The feed
    would then look flawless everywhere, which is the failure mode this whole
    project is about: a measurement that cannot record its own blindness.

    Days, not everything at once, because an interval measured across
    midnight would span whatever the collector did overnight.
    """
    files = sorted(store.rglob("*.parquet"))
    if not files:
        raise SystemExit(f"\n  No AIS under {store}.\n")

    groups: dict[str, list[Path]] = {}
    for f in files:
        # bulk: date=YYYY-MM-DD/...   live: hour=YYYYMMDDHH/...
        day = ""
        for part in f.parts:
            if part.startswith("date="):
                day = part[5:]
            elif part.startswith("hour=") and len(part) >= 13:
                day = f"{part[5:9]}-{part[9:11]}-{part[11:13]}"
        groups.setdefault(day or f.stem, []).append(f)
    return sorted(groups.items())


def reports_in(paths: list[Path], box, window: tuple[datetime, datetime] | None):
    """(mmsi, t, lon, lat) for one day, ORDERED BY VESSEL AND TIME.

    The order is the point: ReceptionGrid.feed_sorted then measures each
    vessel's reporting interval from one row to the next, and the whole day
    never has to be in memory. DuckDB does the sort, which is what it is for.
    """
    import duckdb

    lomin, lamin, lomax, lamax = box
    where = [f'"LON" BETWEEN {lomin} AND {lomax}',
             f'"LAT" BETWEEN {lamin} AND {lamax}']
    if window is not None:
        t0, t1 = window
        where += [f"\"BaseDateTime\" >= TIMESTAMP '{t0:%Y-%m-%d %H:%M:%S}'",
                  f"\"BaseDateTime\" <= TIMESTAMP '{t1:%Y-%m-%d %H:%M:%S}'"]
    listed = ", ".join(f"'{p.as_posix()}'" for p in paths)
    sql = (f'SELECT "MMSI", "BaseDateTime", "LON", "LAT" '
           f"FROM read_parquet([{listed}]) "
           f"WHERE {' AND '.join(where)} "
           f'ORDER BY "MMSI", "BaseDateTime"')
    for mmsi, t, lon, lat in duckdb.sql(sql).fetchall():
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        yield str(mmsi), t, float(lon), float(lat)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--window", type=float, default=None,
                    help="restrict to this many seconds either side of each "
                         "acquisition. Default: the whole of every day on "
                         "disk, which is what a receiver network's reach is "
                         "a property of")
    ap.add_argument("--cell", type=float, default=coverage.CELL_DEG)
    ap.add_argument("--store", choices=sorted(STORES), default="maritime",
                    help="which AIS store to measure. Default is the bulk "
                         "MarineCadastre archive; the live stores are what "
                         "this project's own collectors write")
    args = ap.parse_args()

    store, box, kind = STORES[args.store]

    # Only the WINDOWED mode needs the pass times; whole-days mode is a
    # property of the receiver network and has nothing to do with when a
    # satellite happened to fly over. Requiring them unconditionally made the
    # script unrunnable against a live feed, which has no passes at all.
    times = scene_times() if args.window is not None else []
    if args.window is not None and not times:
        print(f"\n  No detection files in {EVENTS}; nothing to window "
              f"around.\n")
        return 1

    grid = ReceptionGrid(cell_deg=args.cell)
    n_reports = 0
    groups = day_groups(store)
    for day, paths in groups:
        if args.window is None:
            n_reports += grid.feed_sorted(reports_in(paths, box, None))
            continue
        half = timedelta(seconds=args.window)
        for t in times:
            if f"{t:%Y-%m-%d}" != day:
                continue
            n_reports += grid.feed_sorted(
                reports_in(paths, box, (t - half, t + half)))

    span = ("whole days" if args.window is None
            else f"+/-{args.window / 60:.0f} min around {len(times)} pass(es)")
    print(f"\n  store: {args.store}  ({kind})")
    print(f"  {n_reports:,} AIS reports, {span}, over {len(groups)} date(s), "
          f"{sum(len(g) for _, g in groups):,} file(s)")
    counts = grid.summary()
    total_cells = len(grid.cells)
    print(f"  {total_cells:,} cells of {args.cell} deg with any AIS at all")
    for k in ("heard", "intermittent", "thin"):
        print(f"    {k:13} {counts.get(k, 0):6,} cells  "
              f"{grid.area_km2(k):9,.0f} km2")

    # -- the offshore edge, band by band -----------------------------------
    #
    # The number this whole file exists to produce: how far out the shore
    # receivers reach. Printed per latitude band because the coastline is
    # not a straight line and neither is the edge.
    print("\n  furthest HEARD cell, by latitude band")
    print(f"    {'band':>14}{'east edge':>12}{'heard cells':>13}")
    lamin, lamax = box[1], box[3]
    band = 0.5
    y = lamin
    while y < lamax:
        cells = [(col, row) for (col, row), c in grid.cells.items()
                 if y <= (row + 0.5) * grid.cell_deg < y + band
                 and c.reception() == HEARD]
        if cells:
            east = max(col for col, _ in cells) * grid.cell_deg
            print(f"    {y:6.1f}-{y + band:4.1f} N{east:12.2f}{len(cells):13,}")
        else:
            print(f"    {y:6.1f}-{y + band:4.1f} N{'none':>12}{0:13}")
        y += band

    OUT.mkdir(parents=True, exist_ok=True)
    name = ("reception.json" if args.store == "maritime"
            else f"reception-{args.store}.json")
    (OUT / name).write_text(json.dumps(grid.to_json(), indent=1),
                            encoding="utf-8")
    print(f"\n  wrote {OUT / name}")

    # -- score the candidates ----------------------------------------------
    #
    # ONLY FROM THE BULK STORE, and this is a guard rather than a preference.
    #
    # The candidates are Sentinel-1 detections from twelve 2024 passes. The
    # live stores hold this week. Scoring 2024 detections against a reception
    # grid built from last night measures nothing, and it would do it by
    # OVERWRITING candidates-scored.geojson -- the file boundary_analysis.py
    # reads, and therefore the file the project's only published result rests
    # on. A live measurement must not be able to destroy a retrospective one
    # by being run with the wrong flag.
    if args.store != "maritime":
        print(f"\n  Reception only: candidates are 2024 SAR detections and "
              f"this grid is\n  {kind}. Scoring them against it would "
              f"measure nothing and would\n  overwrite "
              f"candidates-scored.geojson. Compare the two grids instead.\n")
        return 0

    cand = EVENTS / "candidates.geojson"
    if not cand.exists():
        print("\n  No candidates.geojson yet -- run "
              "scripts/persistent_sites.py to make one.\n")
        return 0

    doc = json.loads(cand.read_text(encoding="utf-8"))
    tally: dict[str, int] = {}
    for f in doc.get("features", []):
        lon, lat = f["geometry"]["coordinates"]
        klass = grid.reception(lon, lat)
        f["properties"]["reception"] = klass
        f["properties"]["ais_cell_vessels"] = grid.cell_at(lon, lat).n_vessels
        tally[klass] = tally.get(klass, 0) + 1

    doc["properties"] = {
        **doc.get("properties", {}),
        "reception_window_s": args.window,
        "reception_span": span,
        "reception_cell_deg": args.cell,
        "reception_counts": tally,
        "what": ("candidates with AIS reception measured at each one. Only "
                 "those in 'heard' water support a dark-vessel reading."),
    }
    out = EVENTS / "candidates-scored.geojson"
    out.write_text(json.dumps(doc, indent=1), encoding="utf-8")

    n = sum(tally.values())
    print(f"\n  {n:,} candidates by reception at their own position")
    for k in ("heard", "intermittent", "thin", "unheard"):
        if tally.get(k):
            print(f"    {k:13} {tally[k]:6,}  "
                  f"{100 * tally[k] / n:4.1f}%")
    print(f"\n  wrote {out.name}")
    print("  Only the 'heard' ones are candidate dark vessels. The rest are "
          "water\n  this dataset cannot speak for -- state them, do not "
          "count them.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
