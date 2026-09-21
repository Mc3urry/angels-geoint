"""Do we ALREADY have observations, sitting in the archive?

THE POINT

Every OpenSky state vector carries a `position_source` field saying how that
position was derived. The ingest script has been storing it in column 17 since
day one, and nothing has ever looked at it.

    0  ADS-B     the aircraft broadcasting its own position      REPORT
    1  ASTERIX   Eurocontrol's radar data exchange format --
                 a position derived from RADAR                   OBSERVATION
    2  MLAT      multilaterated from receiver arrival times      OBSERVATION
    3  FLARM     glider/light-aircraft beacon, self-reported     REPORT

Two of those four are observations in the strict sense of this project. Which
means the question "can I get an independent observation channel" may already
have been answered, weeks ago, by a collector that was running while nobody
was looking at that column.

WHY THIS IS WORTH RUNNING BEFORE SPENDING ANY MONEY

OpenSky said they have no HISTORICAL MLAT tables for the mid-Atlantic. That is
a statement about their Trino archive, not about the live states feed -- and
the live feed is what this project has been collecting. The two are different
products and it is entirely possible to be refused one while already holding
the other.

It costs nothing to check, uses data already on disk, and takes a second.

    python scripts/check_position_sources.py
    python scripts/check_position_sources.py --aoi conus
    python scripts/check_position_sources.py --aoi conus --by-region

THE NATIONAL ARCHIVE IS THE INTERESTING ONE. MLAT needs several receivers with
overlapping coverage, so it is a property of where the feeders are, not of the
sky. Nothing over Washington does not mean nothing over Denver -- and the conus
collector now covers both. If MLAT turns up anywhere in the country, that is an
argument for where the study area should be, made with evidence.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Re-runs this script under the interpreter that has ANGELS installed, if the
# one invoking it does not. See scripts/_bootstrap.py.
#
# The name check matters. `import _bootstrap` only resolves when scripts/ is on
# sys.path, which is true when this file is RUN and false when the test suite
# IMPORTS it as scripts.<name>. Swallowing every ModuleNotFoundError here would
# also swallow the one _bootstrap raises about 'angels' itself -- turning a
# clear "wrong interpreter" message back into a confusing one.
try:
    import _bootstrap  # noqa: F401  (must precede the angels imports)
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.adapters.aviation.opensky import (
    SRC_ADSB, SRC_ASTERIX, SRC_FLARM, SRC_MLAT,
)
from angels.config import AOIS, RAW

KIND = {
    SRC_ADSB: ("ADS-B", "report", "aircraft broadcasting its own position"),
    SRC_ASTERIX: ("ASTERIX", "OBSERVATION", "position derived from RADAR"),
    SRC_MLAT: ("MLAT", "OBSERVATION", "multilaterated from receiver geometry"),
    SRC_FLARM: ("FLARM", "report", "glider beacon, self-reported"),
}


def query(sql: str):
    import duckdb
    try:
        return duckdb.sql(sql).df()
    except Exception as exc:
        print(f"\n  archive read failed: {exc}")
        print("  (has the collector written anything yet?)\n")
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--aoi", choices=sorted(AOIS), default="air")
    ap.add_argument("--by-region", action="store_true",
                    help="for observations, show WHERE they were, rounded to "
                         "whole degrees. The answer to 'should I move the "
                         "study area'.")
    args = ap.parse_args()

    aoi = AOIS[args.aoi]
    pattern = (Path(RAW) / aoi["dataset"] / "hour=*" / "*.parquet").as_posix()

    print(f"\n  archive: {aoi['dataset']} ({aoi['label']})")

    df = query(f"""
        SELECT position_source AS src,
               COUNT(*) AS rows,
               COUNT(DISTINCT icao24) AS craft,
               MIN(last_contact) AS first_t,
               MAX(last_contact) AS last_t
        FROM read_parquet('{pattern}')
        WHERE latitude IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC
    """)
    if df is None or df.empty:
        print("  empty -- nothing collected into this archive yet.\n")
        return 1

    total = int(df["rows"].sum())
    print(f"  {total:,} positioned rows\n")
    print(f"  {'source':<10}{'kind':<13}{'rows':>12}{'craft':>8}   what it is")
    print("  " + "-" * 74)

    observations = 0
    for _, r in df.iterrows():
        src = int(r["src"])
        name, kind, what = KIND.get(src, (f"code {src}", "unknown", "?"))
        if kind == "OBSERVATION":
            observations += int(r["rows"])
        print(f"  {name:<10}{kind:<13}{int(r['rows']):>12,}"
              f"{int(r['craft']):>8}   {what}")

    print("  " + "-" * 74)
    pct = 100 * observations / total
    print(f"  {'':<23}{observations:>12,}   observations ({pct:.3f}%)\n")

    if observations == 0:
        print("  Everything in this archive is self-reported. No observation")
        print("  channel here.\n")
        if args.aoi == "air":
            print("  Try the national archive before concluding anything:")
            print("    python scripts/check_position_sources.py --aoi conus\n")
            print("  MLAT is a property of where the receivers are, not of the")
            print("  sky. Absent over Washington says nothing about Denver,")
            print("  and the conus collector now covers both.\n")
        return 1

    print("  YOU ALREADY HAVE AN OBSERVATION CHANNEL.")
    print()
    print("  These rows were sensed rather than self-reported, they are")
    print("  already on your disk, and they cost nothing. adapters/aviation")
    print("  can implement observations() by filtering the archive on")
    print("  position_source IN (1, 2) -- no Trino access, no hardware, no")
    print("  waiting on anyone.\n")

    if args.by_region:
        print("  Where they were:\n")
        rdf = query(f"""
            SELECT CAST(FLOOR(latitude) AS INT) AS lat,
                   CAST(FLOOR(longitude) AS INT) AS lon,
                   COUNT(*) AS rows, COUNT(DISTINCT icao24) AS craft
            FROM read_parquet('{pattern}')
            WHERE position_source IN ({SRC_ASTERIX}, {SRC_MLAT})
              AND latitude IS NOT NULL
            GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 25
        """)
        if rdf is not None and not rdf.empty:
            print(f"    {'lat':>5}{'lon':>6}{'rows':>10}{'craft':>8}")
            for _, r in rdf.iterrows():
                print(f"    {int(r['lat']):>5}{int(r['lon']):>6}"
                      f"{int(r['rows']):>10,}{int(r['craft']):>8}")
            print()
            print("  Each row is a one-degree cell. Dense cells are where the")
            print("  feeder network is thick enough to multilaterate -- which")
            print("  is a defensible, evidence-based argument for where the")
            print("  study area should be, if it needs to move.\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
