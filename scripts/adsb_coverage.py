"""Measure where, and at what height, this ADS-B feed can actually hear.

    python scripts/adsb_coverage.py                    # the CONUS archive
    python scripts/adsb_coverage.py --aoi air          # the Chesapeake box
    python scripts/adsb_coverage.py --cell 2.0 --since 2026-09-15
    python scripts/adsb_coverage.py --at -117.0,40.0   # one column, printed

Writes

    data/interim/coverage/reception-<aoi>.json     the grid

WHY THIS COMES BEFORE THE VIEWER GOES NATIONAL

Over the two-degree box around Washington, receiver coverage is uniform
enough to ignore. Over the continental United States it is not: OpenSky is a
volunteer network, dense along the Northeast corridor and thin over the
Great Basin. Until this has been run, a national map of aircraft that
stopped reporting is a map of where nobody is listening, and the project's
whole argument -- that the discrepancy between cooperative reporting and
independent observation is the intelligence product -- collapses into an
artefact of the receiver network.

This is the aviation counterpart of ais_coverage.py, and it answers the same
question in the same words: if something had transmitted here, would this
dataset show it?

WHAT IS DIFFERENT, AND WHY

Height. ADS-B is line-of-sight, so a ground station hears an airliner at
eleven kilometres from hundreds of kilometres away and a light aircraft at
five hundred metres only when it is nearly overhead. Coverage is therefore a
property of a CELL AND A BAND, never of a cell alone, and this script
reports a vertical profile rather than a single class per square.

READ THE OUTPUT AS A LIMIT ON THE STUDY AREA

If the national floor sits at six kilometres over the interior west, then
low-altitude behaviour there is simply not answerable with this data, and
the boundary comparison has to be made among the places where it is. That is
a fact about the archive worth stating in the methods, not a disappointment.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.adapters.aviation.coverage import (
    BAND_NAMES,
    BANDS,
    CELL_DEG,
    GROUND,
    AirReceptionGrid,
)
from angels.config import AOIS, INTERIM, RAW
from angels.core.coverage import HEARD, INTERMITTENT, THIN

OUT = INTERIM / "coverage"


def files_for(dataset: str, since: str | None, until: str | None) -> list[Path]:
    """Snapshot files, oldest first, filtered by the hour= partition.

    The partition is the filter rather than the timestamps inside, so a date
    range never costs a read of the files it excludes.
    """
    root = RAW / dataset
    if not root.exists():
        raise SystemExit(
            f"\n  No archive at {root}.\n"
            f"  The collector writes it: .\\collector.ps1 status\n")
    out = []
    for p in sorted(root.glob("hour=*/states_*.parquet")):
        day = p.parent.name[len("hour="):][:8]
        iso = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
        if since and iso < since:
            continue
        if until and iso > until:
            continue
        out.append(p)
    return out


def polls_from(path: Path):
    """Each snapshot in one file, as (rows) grouped by the instant we asked.

    A file holds several polls -- the collector buffers three before writing
    -- and they MUST be kept apart. Presence is counted per poll, so merging
    them would turn three moments of evidence into one and understate the
    coverage of every cell in the file.
    """
    import pyarrow.parquet as pq

    t = pq.read_table(path, columns=[
        "fetched_at", "icao24", "longitude", "latitude",
        "baro_altitude", "on_ground", "last_contact"])
    d = t.to_pydict()
    by_poll: dict[object, list] = {}
    for i in range(t.num_rows):
        lon, lat = d["longitude"][i], d["latitude"][i]
        if lon is None or lat is None:
            continue                      # no position, no cell. Not guessed.
        fetched = d["fetched_at"][i]
        age = None
        if d["last_contact"][i] is not None and fetched is not None:
            age = fetched.timestamp() - float(d["last_contact"][i])
        by_poll.setdefault(fetched, []).append((
            d["icao24"][i], lon, lat, d["baro_altitude"][i],
            bool(d["on_ground"][i]), age))
    for _, rows in sorted(by_poll.items(), key=lambda kv: str(kv[0])):
        yield rows


def parse_at(spec: str) -> tuple[float, float]:
    """"lon,lat" -> (lon, lat), tolerantly.

    PowerShell treats a bare comma as an array separator, so `--at=-117.0,40.0`
    typed at a PowerShell prompt arrives here as "-117.0 40.0" -- comma eaten,
    space in its place. That is not the user's mistake to fix, so both
    separators are accepted, and so is the quoted form.
    """
    parts = [p for p in spec.replace(",", " ").split() if p]
    if len(parts) != 2:
        raise ValueError(f"expected lon,lat -- got {spec!r}")
    return float(parts[0]), float(parts[1])


def bar(n: int, total: int, width: int = 24) -> str:
    filled = 0 if not total else round(width * n / total)
    return "#" * filled + "." * (width - filled)


def print_profile(grid: AirReceptionGrid, lon: float, lat: float) -> None:
    print(f"\n  the column at {lat:.2f}, {lon:.2f}")
    prof = grid.profile(lon, lat)
    for band in reversed(BANDS):
        c = grid.band_at(lon, lat, band)
        print(f"    {band:>9}  {prof[band]:<13} "
              f"{c.n_aircraft:>6,} aircraft  "
              f"{100 * c.presence(grid.n_polls):5.1f}% of polls")
    floor = grid.floor(lon, lat)
    print(f"\n    heard down to: {floor or 'NOT AT ALL, at any height'}")
    if floor and floor != GROUND:
        print(f"    Below {floor}, this dataset has no opinion about this "
              f"place.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--aoi", default="conus", choices=sorted(AOIS),
                    help="which collection footprint (default conus)")
    ap.add_argument("--cell", type=float, default=CELL_DEG,
                    help=f"cell size in degrees (default {CELL_DEG})")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD, inclusive")
    ap.add_argument("--until", default=None, help="YYYY-MM-DD, inclusive")
    ap.add_argument("--at", default=None,
                    help="lon,lat -- also print the vertical profile there")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N files (for a quick look)")
    args = ap.parse_args()

    # VALIDATED BEFORE ANY WORK. The first version parsed --at at the end,
    # after the grid was built: a mistyped coordinate cost a full pass over
    # 2,062 files and then threw the whole run away on a traceback. An
    # argument that can be checked in microseconds is checked first.
    at = None
    if args.at:
        try:
            at = parse_at(args.at)
        except ValueError as exc:
            print(f"\n  --at: {exc}")
            print("  Expected two numbers: --at=-117.0,40.0  (or "
                  '"--at=-117.0 40.0")\n')
            return 2

    aoi = AOIS[args.aoi]
    files = files_for(aoi["dataset"], args.since, args.until)
    if args.limit:
        files = files[:args.limit]
    if not files:
        print(f"\n  No snapshots in {RAW / aoi['dataset']} for that range.\n")
        return 1

    span = (files[0].parent.name[len("hour="):][:8],
            files[-1].parent.name[len("hour="):][:8])
    print(f"\n  {aoi['label']}  ({args.aoi})")
    print(f"  {len(files):,} snapshot file(s), {span[0]} to {span[1]}, "
          f"{args.cell} deg cells")

    grid = AirReceptionGrid(args.cell)
    n_states = 0
    for i, p in enumerate(files, 1):
        for rows in polls_from(p):
            n_states += grid.add_poll(rows)
        if i % 25 == 0 or i == len(files):
            print(f"\r    {i:,}/{len(files):,} files, {grid.n_polls:,} polls, "
                  f"{n_states:,} states", end="", flush=True)
    print()

    if not grid.n_polls:
        print("\n  Nothing readable in those files.\n")
        return 1

    # -- the national picture ---------------------------------------------
    print(f"\n  {grid.n_polls:,} polls, {n_states:,} positioned states, "
          f"{len(grid.cells):,} cell-bands")
    if grid.n_no_altitude:
        print(f"  {grid.n_no_altitude:,} states had no altitude and are "
              f"counted in no band\n  (they are dropped, not floored -- an "
              f"unknown height is unknown)")

    fresh = grid.freshness()
    if fresh.get("n"):
        print(f"\n  freshness (NOT coverage): median contact age "
              f"{fresh['median_s']:.0f} s, p90 {fresh['p90_s']:.0f} s, "
              f"{100 * fresh['p_stale']:.1f}% over {fresh['stale_s']:.0f} s")
        print("  This measures how current a held fix is. It was measured to "
              "be flat\n  nationally, so it cannot stand in for coverage; "
              "the bands below can.")

    summary = grid.summary()
    print("\n  CELLS BY BAND -- how much of the sky this feed can speak for")
    print(f"    {'band':>9}  {'heard':>6} {'intermit':>9} {'thin':>6} "
          f"{'':>26}")
    for band in reversed(BANDS):
        s = summary[band]
        total = sum(s.values())
        print(f"    {band:>9}  {s[HEARD]:>6,} {s[INTERMITTENT]:>9,} "
              f"{s[THIN]:>6,}  {bar(s[HEARD], total)}")
    print("\n  A cell absent from a band was never heard in it at all, and is "
          "counted\n  in none of these columns. 'heard' is the only class in "
          "which a silence\n  is a finding.")

    # -- the honest limit --------------------------------------------------
    heard_area = {b: grid.area_km2(b, HEARD) for b in BANDS}
    widest = max(heard_area.values()) or 1.0
    print("\n  HEARD AREA BY BAND, as a share of the best-covered band")
    for band in reversed(BANDS):
        print(f"    {band:>9}  {heard_area[band]:>12,.0f} km2  "
              f"{100 * heard_area[band] / widest:5.1f}%")
    # HOW MUCH OF THIS COLUMN IS COVERAGE AND HOW MUCH IS TRAFFIC.
    #
    # Line-of-sight range only ever GROWS with height, so under coverage
    # alone this column could only fall as you read downward. Anywhere it
    # rises instead, traffic is doing the work -- there is simply more or
    # less flying at that height -- and the two effects are not separable
    # from these numbers.
    #
    # The Chesapeake box shows both at once: 0-1 km at 97%, 1-3 km at 97%,
    # then 3-6 km at 87% and 6-9 km at 65% -- a dip through the climb-out
    # altitudes where little cruises -- and back to 100% at 9-12 km. Reading
    # that dip as a coverage hole at 6-9 km would be wrong.
    #
    # So the safe readings are stated, and the unsafe one is named.
    #
    # The GROUND band is excluded from the monotonic reading entirely. An
    # on-ground contact can only exist where there is somewhere to be on the
    # ground, so that band tracks the distribution of airports and says
    # nothing about the sky. It is reported above and reasoned about nowhere.
    airborne = list(BAND_NAMES)
    rises = [(a, b) for a, b in zip(airborne, airborne[1:], strict=False)
             if heard_area[b] > heard_area[a] + 1.0]
    low = 100 * heard_area[BAND_NAMES[0]] / widest
    print("\n  WHAT THIS COLUMN DOES AND DOES NOT SAY")
    # The same template read backwards at 97% and at 35%, so the sentence is
    # chosen by the number rather than assuming the number is small.
    if low >= 80:
        print(f"    Coverage reaches the deck here: the 0-1 km band is "
              f"{low:.0f}% of the widest\n    band, so low-altitude "
              f"behaviour IS answerable over most of this box.")
    elif low >= 40:
        print(f"    Coverage reaches low over part of this box: the 0-1 km "
              f"band is {low:.0f}% of\n    the widest band. Below a "
              f"kilometre, WHERE matters -- ask the grid per point,\n    "
              f"not the column.")
    else:
        print(f"    The bottom of this column is a coverage floor: the "
              f"0-1 km band is only\n    {low:.0f}% of the widest band, so "
              f"low-altitude behaviour is not answerable\n    over most of "
              f"this box.")
    print(f"    ({BANDS[0]} is left out of this reading: it can only exist "
          f"where there is\n    somewhere to land, so it maps airports, not "
          f"reception.)")
    if rises:
        print(f"    The column is NOT monotonic -- it rises again at "
              f"{', '.join(b for _, b in rises)}.")
        print("    Coverage cannot do that, since radio range only grows "
              "with height. That\n    is traffic: more or fewer aircraft "
              "fly at those altitudes. Coverage and\n    traffic are not "
              "separable from these numbers.")
    print("    The ONE safe comparison is the same band in two different "
          "places. Never\n    one band against another, and never a band "
          "against the column's peak.")

    if at:
        print_profile(grid, *at)

    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"reception-{args.aoi}.json"
    doc = grid.to_json()
    doc["aoi"] = args.aoi
    doc["bbox"] = list(aoi["box"])
    doc["span"] = list(span)
    doc["n_files"] = len(files)
    out.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    print(f"\n  wrote {out}  ({out.stat().st_size / 1e6:.1f} MB)")
    print("\n  Next: nothing in the aviation archive should be called a "
          "silence\n  without asking this grid first.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
