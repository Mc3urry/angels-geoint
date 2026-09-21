"""Clip national AIS files down to AOI_SEA and store as Parquet.

MarineCadastre publishes one file per day covering ALL US waters -- roughly
320 MB compressed, 116.7 GB for a year. You want a few hundred square miles of
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

Output: data/raw/maritime/date=YYYY-MM-DD/ais.parquet
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

import duckdb

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

from angels.config import AOI_SEA, RAW, REFERENCE, REGION, box_area_km2, clip_expr

# MarineCadastre daily CSV schema, 2015 onward.
COLUMNS = [
    "MMSI", "BaseDateTime", "LAT", "LON", "SOG", "COG", "Heading",
    "VesselName", "IMO", "CallSign", "VesselType", "Status",
    "Length", "Width", "Draft", "Cargo", "TransceiverClass",
]


def find_inputs(src: Path) -> list[Path]:
    return sorted(list(src.glob("AIS_*.zip")) + list(src.glob("AIS_*.csv")))


def date_from_name(p: Path) -> str:
    """AIS_2024_09_09.zip -> 2024-09-09"""
    parts = p.stem.split("_")
    return "-".join(parts[1:4]) if len(parts) >= 4 else p.stem


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--src", type=Path, default=REFERENCE / "ais",
                    help="where the downloaded AIS_*.zip files are")
    ap.add_argument("--bbox", choices=["sea", "region"], default="sea",
                    help="AOI_SEA (default) or the whole REGION")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    bbox = AOI_SEA if args.bbox == "sea" else REGION
    files = find_inputs(args.src)

    if not files:
        print(f"no AIS files in {args.src}")
        print("Download daily files first:")
        print("  https://coast.noaa.gov/htdata/CMSP/AISDataHandler/2024/")
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
