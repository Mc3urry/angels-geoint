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
from angels.config import AOI_SEA, EVENTS, INTERIM, RAW

AIS_STORE = RAW / "maritime"
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


def ais_files() -> list[Path]:
    files = sorted(AIS_STORE.rglob("*.parquet"))
    if not files:
        raise SystemExit(f"\n  No clipped AIS under {AIS_STORE}. "
                         f"Run scripts/clip_ais.py first.\n")
    return files


def reports_in(path: Path, window: tuple[datetime, datetime] | None):
    """(mmsi, t, lon, lat) from one clipped day, ORDERED BY VESSEL AND TIME.

    The order is the point: ReceptionGrid.feed_sorted then measures each
    vessel's reporting interval from one row to the next, and the whole day
    never has to be in memory. DuckDB does the sort, which is what it is for.
    """
    import duckdb

    lomin, lamin, lomax, lamax = AOI_SEA
    where = [f'"LON" BETWEEN {lomin} AND {lomax}',
             f'"LAT" BETWEEN {lamin} AND {lamax}']
    if window is not None:
        t0, t1 = window
        where += [f"\"BaseDateTime\" >= TIMESTAMP '{t0:%Y-%m-%d %H:%M:%S}'",
                  f"\"BaseDateTime\" <= TIMESTAMP '{t1:%Y-%m-%d %H:%M:%S}'"]
    sql = (f'SELECT "MMSI", "BaseDateTime", "LON", "LAT" '
           f"FROM read_parquet(['{path.as_posix()}']) "
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
    args = ap.parse_args()

    times = scene_times()
    if not times:
        print(f"\n  No detection files in {EVENTS}; nothing to measure "
              f"around.\n")
        return 1

    grid = ReceptionGrid(cell_deg=args.cell)
    n_reports = 0
    files = ais_files()
    for f in files:
        # One clipped day at a time. Each is fed separately, so no interval
        # is ever measured across the join between two days.
        if args.window is None:
            n_reports += grid.feed_sorted(reports_in(f, None))
            continue
        half = timedelta(seconds=args.window)
        for t in times:
            if f"date={t:%Y-%m-%d}" not in str(f):
                continue
            n_reports += grid.feed_sorted(reports_in(f, (t - half, t + half)))

    span = ("whole days" if args.window is None
            else f"+/-{args.window / 60:.0f} min around {len(times)} pass(es)")
    print(f"\n  {n_reports:,} AIS reports, {span}, over {len(files)} date(s)")
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
    lamin, lamax = AOI_SEA[1], AOI_SEA[3]
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
    (OUT / "reception.json").write_text(json.dumps(grid.to_json(), indent=1),
                                        encoding="utf-8")
    print(f"\n  wrote {OUT / 'reception.json'}")

    # -- score the candidates ----------------------------------------------

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
