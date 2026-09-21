"""Clip national AIS files down to AOI_SEA and store as Parquet.

MarineCadastre publishes one file per day covering ALL US waters -- roughly
300 MB, 116.7 GB for a year. You want a few hundred square miles of
that. This does the reduction once, so nothing downstream ever touches the
national files again.

    python scripts/clip_ais.py                    # every zip in data/reference/ais
    python scripts/clip_ais.py --dry-run          # report only, write nothing
    python scripts/clip_ais.py --bbox region      # use REGION instead of AOI_SEA

DuckDB streams the CSV and only materialises rows that pass the WHERE clause,
so a national day never lands in memory. Tens of seconds per file rather than
the many minutes pandas would need, on a laptop.

Zips are unpacked to the system temp directory first -- DuckDB reads gzip
transparently but not zip. A daily national file expands to well over a
gigabyte, so keep that much scratch space free while this runs.

TWO INPUT FORMATS, ONE OUTPUT

    AIS_2024_09_25.zip        legacy daily CSV, coast.noaa.gov
    ais-2024-06-21.parquet    GeoParquet, MarineCadastre's Azure store

In 2026 MarineCadastre moved 2024 onward to GeoParquet and the coast.noaa.gov
zips began returning 404, though NOAA's index still lists them. The GeoParquet
renames every column to snake_case and replaces LAT/LON with a WKB point.
Both are written out in the legacy schema below, so nothing downstream can
tell -- or needs to know -- which one a day came from.

The point is decoded here with numpy rather than DuckDB's spatial extension.
Whether DuckDB even recognises the column as geometry depends on its version
(1.5 does, natively; older builds need the extension, which needs a network
connection to install), and a clip that works on one laptop and not another
is not a method. A WKB point is 21 fixed bytes; reading them directly works
everywhere pyarrow does.

THINNED, NOT ROUNDED -- AND NO MORE THAN BEFORE

MarineCadastre describes the data as downsampled to one minute. Measured:
timestamps keep their seconds (98% of 2024-06-21 is off the minute), and
consecutive reports from one vessel are at least about 60 s apart -- under
1% closer, in the 2024-09-25 CSV clip and the 2024-06-21 GeoParquet clip
alike. So a time is exact, the thinning is not new with the format change,
and nothing about per-report uncertainty changes. The sparser fixes are
what Track.position_at's interpolation penalty already charges for.

Output: data/raw/maritime/date=YYYY-MM-DD/ais.parquet
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import zipfile
from contextlib import contextmanager
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

# Third-party imports come AFTER the bootstrap, never before: _bootstrap
# re-executes this script under the project interpreter, and an import
# placed above it runs first -- under whatever Python the user typed --
# and dies with ModuleNotFoundError before the switch can happen.
import duckdb

from angels.config import AOI_SEA, RAW, REFERENCE, REGION, box_area_km2, clip_expr

# MarineCadastre daily CSV schema, 2015 onward.
COLUMNS = [
    "MMSI", "BaseDateTime", "LAT", "LON", "SOG", "COG", "Heading",
    "VesselName", "IMO", "CallSign", "VesselType", "Status",
    "Length", "Width", "Draft", "Cargo", "TransceiverClass",
]


# GeoParquet (2024 onward) column -> the legacy name above. geometry is not
# here: it becomes LAT and LON.
GEOPARQUET_RENAME = {
    "mmsi": "MMSI", "base_date_time": "BaseDateTime",
    "sog": "SOG", "cog": "COG", "heading": "Heading",
    "vessel_name": "VesselName", "imo": "IMO", "call_sign": "CallSign",
    "vessel_type": "VesselType", "status": "Status",
    "length": "Length", "width": "Width", "draft": "Draft",
    "cargo": "Cargo", "transceiver": "TransceiverClass",
}
GEOMETRY = "geometry"


def find_inputs(src: Path) -> list[Path]:
    """Every national day in src, one file per date.

    If a date is present in both formats the GeoParquet wins: it is the
    maintained product and the one a re-download would fetch.
    """
    by_date: dict[str, Path] = {}
    for p in sorted(list(src.glob("AIS_*.zip")) + list(src.glob("AIS_*.csv"))):
        by_date[date_from_name(p)] = p
    for p in sorted(src.glob("ais-*.parquet")):
        by_date[date_from_name(p)] = p
    return [by_date[d] for d in sorted(by_date)]


def date_from_name(p: Path) -> str:
    """AIS_2024_09_09.zip -> 2024-09-09;  ais-2024-06-21.parquet -> 2024-06-21"""
    stem = p.name.split(".")[0]
    if stem.startswith("ais-"):
        return stem[4:]
    parts = stem.split("_")
    return "-".join(parts[1:4]) if len(parts) >= 4 else stem


def wkb_points(arr):
    """(lon, lat) arrays from a column of WKB points. NaN where not a point.

    A 2-D WKB point is 21 bytes: byte order (1), geometry type (4), x (8),
    y (8). Anything else -- null, empty, a Z point, a line -- decodes to NaN
    and is counted by the caller rather than guessed at.
    """
    import numpy as np
    import pyarrow as pa

    if isinstance(arr, pa.ChunkedArray):
        arr = arr.combine_chunks()
    if isinstance(arr, pa.ExtensionArray):
        arr = arr.storage

    n = len(arr)
    lon = np.full(n, np.nan)
    lat = np.full(n, np.nan)
    if n == 0:
        return lon, lat

    off_t = np.dtype(np.int64 if pa.types.is_large_binary(arr.type) else np.int32)
    _, off_buf, data_buf = arr.buffers()
    offsets = np.frombuffer(off_buf, dtype=off_t, count=n + 1,
                            offset=arr.offset * off_t.itemsize).astype(np.int64)
    if data_buf is None:
        return lon, lat
    data = np.frombuffer(data_buf, dtype=np.uint8)

    ok = np.diff(offsets) == 21
    if arr.null_count:
        ok &= arr.is_valid().to_numpy(zero_copy_only=False)
    rows = np.flatnonzero(ok)
    if rows.size == 0:
        return lon, lat

    raw = data[offsets[rows][:, None] + np.arange(21)]
    little = (raw[:, 0] == 1) & (raw[:, 1:5] == [1, 0, 0, 0]).all(axis=1)
    big = (raw[:, 0] == 0) & (raw[:, 1:5] == [0, 0, 0, 1]).all(axis=1)

    xy = np.ascontiguousarray(raw[:, 5:21])
    if little.any():
        v = xy[little].view("<f8")
        lon[rows[little]], lat[rows[little]] = v[:, 0], v[:, 1]
    if big.any():
        v = xy[big].view(">f8")
        lon[rows[big]], lat[rows[big]] = v[:, 0], v[:, 1]
    return lon, lat


def csv_inside(zip_path: Path) -> str | None:
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
    return names[0] if names else None


@contextmanager
def as_csv(src: Path):
    """Yield a readable CSV path, extracting from a zip if needed.

    DuckDB reads gzip transparently but not zip, so a zip has to be unpacked
    first. Uses the system temp directory and removes the file afterwards --
    but note a daily national file expands to well over a gigabyte, so you
    need the headroom while this runs.
    """
    if src.suffix != ".zip":
        yield src
        return

    inner = csv_inside(src)
    if inner is None:
        yield None
        return

    with tempfile.TemporaryDirectory(prefix="angels_ais_") as tmp:
        with zipfile.ZipFile(src) as z:
            z.extract(inner, tmp)
        yield Path(tmp) / inner


def clip_one(con: duckdb.DuckDBPyConnection, src: Path, dest_root: Path,
             bbox, *, dry: bool) -> tuple[int, int]:
    """Returns (rows_in, rows_out)."""
    if src.suffix == ".parquet":
        return _clip_geoparquet(con, src, dest_root, bbox, dry=dry)
    with as_csv(src) as csv_path:
        if csv_path is None:
            print(f"  {src.name}: no csv inside, skipping")
            return 0, 0
        return _clip_csv(con, csv_path, src, dest_root, bbox, dry=dry)


def _clip_csv(con: duckdb.DuckDBPyConnection, csv_path: Path, src: Path,
              dest_root: Path, bbox, *, dry: bool) -> tuple[int, int]:
    read = f"read_csv_auto('{csv_path.as_posix()}', header=true)"
    total = con.sql(f"SELECT count(*) FROM {read}").fetchone()[0]
    kept = con.sql(f"SELECT count(*) FROM {read} WHERE {clip_expr(bbox)}").fetchone()[0]

    if not dry and kept:
        out = dest_root / "maritime" / f"date={date_from_name(src)}"
        out.mkdir(parents=True, exist_ok=True)
        con.sql(
            f"COPY (SELECT * FROM {read} WHERE {clip_expr(bbox)}) "
            f"TO '{(out / 'ais.parquet').as_posix()}' "
            f"(FORMAT PARQUET, COMPRESSION SNAPPY)"
        )
    return total, kept


def _clip_geoparquet(con: duckdb.DuckDBPyConnection, src: Path,
                     dest_root: Path, bbox, *, dry: bool) -> tuple[int, int]:
    """One GeoParquet day -> the legacy schema, clipped. Row group by row
    group, so a national day (about ten groups of a million rows) never has
    to be in memory at once."""
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(src)
    names = set(pf.schema_arrow.names)
    missing = sorted((set(GEOPARQUET_RENAME) | {GEOMETRY}) - names)
    if missing:
        # Selected by name for the reason ais.COLUMNS gives: a renamed or
        # dropped column must stop the clip, not shift fields silently.
        raise ValueError(f"not the expected GeoParquet schema; missing "
                         f"{', '.join(missing)}. Found: {sorted(names)}")

    lomin, lamin, lomax, lamax = bbox
    total = undecodable = 0
    kept: list[pa.Table] = []
    cols = list(GEOPARQUET_RENAME) + [GEOMETRY]
    for i in range(pf.num_row_groups):
        t = pf.read_row_group(i, columns=cols)
        total += t.num_rows
        lon, lat = wkb_points(t.column(GEOMETRY))
        undecodable += int(np.isnan(lon).sum())
        with np.errstate(invalid="ignore"):
            inside = ((lon >= lomin) & (lon <= lomax)
                      & (lat >= lamin) & (lat <= lamax))
        if not inside.any():
            continue
        part = t.drop_columns([GEOMETRY]).filter(pa.array(inside))
        part = part.rename_columns([GEOPARQUET_RENAME[c] for c in part.column_names])
        part = part.append_column("LAT", pa.array(lat[inside]))
        part = part.append_column("LON", pa.array(lon[inside]))
        kept.append(part)

    if undecodable:
        print(f"  {src.name}: {undecodable:,} row(s) without a readable point "
              f"geometry, skipped")

    n_out = sum(p.num_rows for p in kept)
    if not dry and n_out:
        clip = pa.concat_tables(kept)
        out = dest_root / "maritime" / f"date={date_from_name(src)}"
        out.mkdir(parents=True, exist_ok=True)
        # Legacy column order, and the legacy integer width for the integer
        # fields, so a CSV-year file and a GeoParquet-year file read the same.
        # Length/Width stay floating point: the new product carries decimals
        # and truncating them to match the old one would discard data.
        con.register("clip_view", clip)
        con.sql(
            "COPY (SELECT MMSI::BIGINT AS MMSI, BaseDateTime, LAT, LON, "
            "SOG::DOUBLE AS SOG, COG::DOUBLE AS COG, Heading::BIGINT AS Heading, "
            "VesselName, IMO, CallSign, VesselType::BIGINT AS VesselType, "
            "Status::BIGINT AS Status, Length::DOUBLE AS Length, "
            "Width::DOUBLE AS Width, Draft::DOUBLE AS Draft, "
            "Cargo::BIGINT AS Cargo, TransceiverClass "
            "FROM clip_view ORDER BY MMSI, BaseDateTime) "
            f"TO '{(out / 'ais.parquet').as_posix()}' "
            "(FORMAT PARQUET, COMPRESSION SNAPPY)")
        con.unregister("clip_view")
    return total, n_out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--src", type=Path, default=REFERENCE / "ais",
                    help="where the downloaded national files are "
                         "(AIS_*.zip or ais-*.parquet)")
    ap.add_argument("--bbox", choices=["sea", "region"], default="sea",
                    help="AOI_SEA (default) or the whole REGION")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    bbox = AOI_SEA if args.bbox == "sea" else REGION
    files = find_inputs(args.src)

    if not files:
        print(f"no AIS files in {args.src}")
        print("Download daily files first:")
        print("  python scripts/fetch_ais.py")
        print("There is no region to choose -- the files are national and")
        print("daily. This script is what makes them regional.")
        return 1

    print(f"clipping to {args.bbox.upper()} {bbox}  "
          f"(~{box_area_km2(bbox):,.0f} km2)")
    print(f"{len(files)} file(s) in {args.src}\n")

    con = duckdb.connect()
    grand_in = grand_out = 0

    for f in files:
        try:
            n_in, n_out = clip_one(con, f, RAW, bbox, dry=args.dry_run)
        except Exception as exc:
            print(f"  {f.name}: FAILED -- {exc}")
            continue
        grand_in += n_in
        grand_out += n_out
        pct = (100 * n_out / n_in) if n_in else 0.0
        print(f"  {date_from_name(f)}  {n_in:>12,} -> {n_out:>9,}  ({pct:4.1f}%)")

    print(f"\ntotal {grand_in:,} -> {grand_out:,} rows "
          f"({100 * grand_out / grand_in if grand_in else 0:.1f}% kept)")
    if args.dry_run:
        print("dry run -- nothing written")
    else:
        print(f"written to {RAW / 'maritime'}")
        print("\nquery it with:")
        print(f"  duckdb.sql(\"SELECT * FROM '{RAW.as_posix()}/maritime/*/*.parquet'\")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
