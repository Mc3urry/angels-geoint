"""Tests for clip_ais.py's GeoParquet path.

MarineCadastre moved 2024 onward from daily CSV zips to GeoParquet with
snake_case columns and a WKB point in place of LAT/LON. The clip has to turn
that back into the legacy schema exactly, because everything downstream
selects columns by name -- and a swapped lon/lat would put the whole fleet
in the wrong ocean without raising anything.

The fixture files are built here; nothing reaches the network.
"""

from __future__ import annotations

import importlib.util
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from angels.adapters.maritime import ais

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("clip_ais", ROOT / "scripts" / "clip_ais.py")
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)

BOX = (-77.2, 36.0, -71.0, 39.6)          # AOI_SEA


def wkb(lon: float, lat: float, *, big: bool = False) -> bytes:
    if big:
        return b"\x00" + struct.pack(">I", 1) + struct.pack(">dd", lon, lat)
    return b"\x01" + struct.pack("<I", 1) + struct.pack("<dd", lon, lat)


def geoparquet(path: Path, points: list, *, row_group_size: int = 2) -> Path:
    n = len(points)
    t0 = datetime(2024, 6, 21, 22, 58, 7)
    table = pa.table({
        "mmsi": pa.array(range(366000001, 366000001 + n), pa.int32()),
        "base_date_time": pa.array([t0] * n, pa.timestamp("ms")),
        "sog": pa.array([10.0] * n, pa.float32()),
        "cog": pa.array([90.0] * n, pa.float32()),
        "heading": pa.array([91] * n, pa.int32()),
        "vessel_name": ["V%d" % i for i in range(n)],
        "imo": [None] * n,
        "call_sign": ["CS"] * n,
        "vessel_type": pa.array([70] * n, pa.int32()),
        "status": pa.array([0] * n, pa.int32()),
        "length": pa.array([23.5] * n, pa.float32()),
        "width": pa.array([8] * n, pa.int32()),
        "draft": pa.array([4.2] * n, pa.float32()),
        "cargo": pa.array([None] * n, pa.int32()),
        "transceiver": ["A"] * n,
        "geometry": pa.array(points, pa.binary()),
    })
    pq.write_table(table, path, row_group_size=row_group_size)
    return path


# -- decoding the point ------------------------------------------------------

def test_lon_is_x_and_lat_is_y() -> None:
    """WKB is (x, y) = (lon, lat). Reversed, a Chesapeake fleet lands in
    Antarctica -- or, worse, somewhere plausible."""
    lon, lat = mod.wkb_points(pa.array([wkb(-76.3, 37.0)], pa.binary()))
    assert lon[0] == pytest.approx(-76.3) and lat[0] == pytest.approx(37.0)


def test_both_byte_orders_decode() -> None:
    lon, lat = mod.wkb_points(pa.array([wkb(-76.3, 37.0, big=True)], pa.binary()))
    assert lon[0] == pytest.approx(-76.3) and lat[0] == pytest.approx(37.0)


def test_what_is_not_a_2d_point_is_nan_not_a_guess() -> None:
    line = b"\x01" + struct.pack("<I", 2) + struct.pack("<I", 0)
    z_point = b"\x01" + struct.pack("<I", 1001) + struct.pack("<ddd", 1, 2, 3)
    wrong_type_21 = b"\x01" + struct.pack("<I", 2) + struct.pack("<dd", 1, 2)
    arr = pa.array([None, b"", line, z_point, wrong_type_21, wkb(-76.0, 37.0)],
                   pa.binary())
    lon, lat = mod.wkb_points(arr)
    assert np.isnan(lon[:5]).all() and np.isnan(lat[:5]).all()
    assert lon[5] == pytest.approx(-76.0)


def test_a_sliced_array_decodes_its_own_rows() -> None:
    """A row group read back can be a slice with a non-zero offset into
    shared buffers. Ignoring the offset decodes the neighbours' points."""
    arr = pa.array([wkb(-70.0, 30.0), wkb(-76.0, 37.0), wkb(-75.0, 38.0)],
                   pa.binary()).slice(1)
    lon, lat = mod.wkb_points(arr)
    assert list(lon) == pytest.approx([-76.0, -75.0])
    assert list(lat) == pytest.approx([37.0, 38.0])


def test_large_binary_decodes() -> None:
    lon, _ = mod.wkb_points(pa.array([wkb(-76.0, 37.0)], pa.large_binary()))
    assert lon[0] == pytest.approx(-76.0)


# -- the file ----------------------------------------------------------------

def test_names_and_dates_for_both_formats(tmp_path) -> None:
    assert mod.date_from_name(Path("ais-2024-06-21.parquet")) == "2024-06-21"
    assert mod.date_from_name(Path("AIS_2024_09_25.zip")) == "2024-09-25"
    (tmp_path / "AIS_2024_09_25.zip").write_bytes(b"")
    (tmp_path / "ais-2024-06-21.parquet").write_bytes(b"")
    assert [p.name for p in mod.find_inputs(tmp_path)] == [
        "ais-2024-06-21.parquet", "AIS_2024_09_25.zip"]


def test_a_date_in_both_formats_is_clipped_once(tmp_path) -> None:
    (tmp_path / "AIS_2024_06_21.zip").write_bytes(b"")
    (tmp_path / "ais-2024-06-21.parquet").write_bytes(b"")
    assert [p.name for p in mod.find_inputs(tmp_path)] == ["ais-2024-06-21.parquet"]


def test_the_clip_keeps_the_box_and_writes_the_legacy_schema(tmp_path) -> None:
    src = geoparquet(tmp_path / "ais-2024-06-21.parquet", [
        wkb(-76.3, 37.0),      # Chesapeake: in
        wkb(-94.2, 29.7),      # Galveston: out
        wkb(-74.0, 38.5),      # shelf: in, second row group
        None,                  # no geometry
        wkb(37.0, -76.3),      # the Chesapeake point with lon/lat swapped: out
    ])
    n_in, n_out = mod.clip_one(duckdb.connect(), src, tmp_path / "raw", BOX, dry=False)
    assert (n_in, n_out) == (5, 2)

    out = tmp_path / "raw" / "maritime" / "date=2024-06-21" / "ais.parquet"
    got = pq.read_table(out)
    assert got.column_names == mod.COLUMNS
    assert got.column("LON").to_pylist() == pytest.approx([-76.3, -74.0])
    assert got.column("LAT").to_pylist() == pytest.approx([37.0, 38.5])
    assert got.column("Length").to_pylist() == pytest.approx([23.5, 23.5])
    assert got.column("TransceiverClass").to_pylist() == ["A", "A"]


def test_a_dry_run_counts_and_writes_nothing(tmp_path) -> None:
    src = geoparquet(tmp_path / "ais-2024-06-21.parquet", [wkb(-76.3, 37.0)])
    assert mod.clip_one(duckdb.connect(), src, tmp_path / "raw", BOX, dry=True) == (1, 1)
    assert not (tmp_path / "raw").exists()


def test_a_renamed_upstream_column_stops_the_clip(tmp_path) -> None:
    src = geoparquet(tmp_path / "ais-2024-06-21.parquet", [wkb(-76.3, 37.0)])
    t = pq.read_table(src).rename_columns(
        ["latitude" if c == "sog" else c for c in pq.read_table(src).column_names])
    pq.write_table(t, src)
    with pytest.raises(ValueError, match="sog"):
        mod.clip_one(duckdb.connect(), src, tmp_path / "raw", BOX, dry=False)


def test_the_loader_reads_the_clip_into_reports(tmp_path) -> None:
    """End to end: GeoParquet -> clip -> ais.load -> an AIS report with the
    position, time, speed and length it was given."""
    src = geoparquet(tmp_path / "ais-2024-06-21.parquet", [wkb(-76.3, 37.0)])
    mod.clip_one(duckdb.connect(), src, tmp_path / "raw", BOX, dry=False)
    reps = ais.load(tmp_path / "raw" / "maritime")
    assert len(reps) == 1
    r = reps[0]
    assert (r.position.lat, r.position.lon) == pytest.approx((37.0, -76.3))
    assert r.position.t == datetime(2024, 6, 21, 22, 58, 7, tzinfo=timezone.utc)
    assert r.length_m == pytest.approx(23.5)
    assert r.transceiver == "A"


def test_legacy_and_geoparquet_days_load_together(tmp_path) -> None:
    """The legacy CSV clip stores Length as BIGINT, the GeoParquet clip as
    DOUBLE. Read together, DuckDB would otherwise cast every file to the
    first file's types -- so which days were loaded would decide a vessel's
    length."""
    raw = tmp_path / "raw"
    src = geoparquet(tmp_path / "ais-2024-06-21.parquet", [wkb(-76.3, 37.0)])
    mod.clip_one(duckdb.connect(), src, raw, BOX, dry=False)

    legacy = raw / "maritime" / "date=2024-05-01"   # sorts FIRST
    legacy.mkdir(parents=True)
    duckdb.sql(
        "COPY (SELECT 367000001::BIGINT AS MMSI, "
        "TIMESTAMP '2024-05-01 22:58:00' AS BaseDateTime, 37.1 AS LAT, "
        "-76.2 AS LON, 5.0 AS SOG, 180.0 AS COG, 181::BIGINT AS Heading, "
        "'OLD' AS VesselName, NULL::VARCHAR AS IMO, 'X' AS CallSign, "
        "70::BIGINT AS VesselType, 0::BIGINT AS Status, 100::BIGINT AS Length, "
        "20::BIGINT AS Width, 6.0 AS Draft, NULL::BIGINT AS Cargo, "
        "'A' AS TransceiverClass) "
        f"TO '{(legacy / 'ais.parquet').as_posix()}' (FORMAT PARQUET)")

    lengths = sorted(r.length_m for r in ais.load(raw / "maritime"))
    assert lengths == pytest.approx([23.5, 100.0])
