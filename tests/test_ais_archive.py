"""Tests for the live-AIS archive.

This is the first thing in the project that WRITES third-party data to disk,
and what it writes is the denominator of every maritime claim that follows.
So the tests are about the ways a collected archive could differ from the
bulk one it is pretending to be -- a schema that drifts, a duplicate that
flatters the reporting cadence, a position that travels backwards in time --
rather than about whether a file appears.

Nothing here reaches the network.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from angels.adapters.maritime import ais
from angels.adapters.maritime.aisstream import Vessel
from angels.adapters.maritime.archive import EXTRA, AISArchive, row_from_vessel, schema

T0 = datetime(2026, 9, 23, 14, 0, tzinfo=UTC).timestamp()


def vessel(mmsi="367000001", lat=37.0, lon=-76.0, t=None, **kw):
    v = Vessel(mmsi=mmsi)
    v.lat, v.lon = lat, lon
    v.t_position = T0 if t is None else t
    v.sog_kn = kw.get("sog", 8.5)
    v.cog_deg = kw.get("cog", 91.0)
    v.heading_deg = kw.get("heading", 90.0)
    v.nav_status = kw.get("status", 0)
    v.name = kw.get("name", "TEST SHIP")
    v.call_sign = kw.get("call_sign", "WABC")
    v.ship_type = kw.get("ship_type", 70)
    v.length_m, v.width_m, v.draught_m = 120.0, 20.0, 7.5
    v.ais_class = kw.get("ais_class", "A")
    return v


# -- the schema is the bulk schema ------------------------------------------

def test_the_columns_are_the_bulk_columns_in_the_bulk_order() -> None:
    """THE WHOLE DESIGN. ais.COLUMNS is what clip_ais, the reception grid,
    the matcher and the boundary analysis all read. A collected file that
    differs by one name is a second maritime format, and every one of those
    tools would need teaching about it."""
    names = [f.name for f in schema()]
    assert names[:len(ais.COLUMNS)] == list(ais.COLUMNS)
    assert names[len(ais.COLUMNS):] == list(EXTRA)


def test_a_row_reads_back_through_the_ordinary_reader() -> None:
    """Not 'it parses' -- that the project's own row_to_report, which the
    bulk files go through, accepts it and recovers the position."""
    row = row_from_vessel(vessel(), T0)
    report = ais.row_to_report(row)
    assert report is not None
    assert report.platform_id == "367000001"
    assert report.position.lat == pytest.approx(37.0)
    assert report.position.lon == pytest.approx(-76.0)


def test_the_timestamp_is_the_vessels_clock_not_ours() -> None:
    """Stamping rows with now() would compress every vessel in a flush into
    one instant and manufacture a reporting cadence of zero."""
    row = row_from_vessel(vessel(t=T0 - 47), received_at=T0)
    assert row["BaseDateTime"].timestamp() == pytest.approx(T0 - 47)
    assert row["received_at"].timestamp() == pytest.approx(T0)


def test_nothing_without_a_position_is_written() -> None:
    assert row_from_vessel(Vessel(mmsi="1"), T0) is None            # never fixed
    # 91 / 181 are the AIS "not available" sentinels: a number is present and
    # it is nowhere. row_to_report drops them on read, so writing them would
    # only fill the archive with rows nothing can use.
    assert row_from_vessel(vessel(lat=91.0), T0) is None
    assert row_from_vessel(vessel(lon=181.0), T0) is None


# -- the rule that protects the cadence -------------------------------------

def test_the_same_report_offered_twice_is_written_once(tmp_path) -> None:
    a = AISArchive(tmp_path)
    v = vessel()
    assert a.add(row_from_vessel(v, T0), T0) is True
    assert a.add(row_from_vessel(v, T0 + 1), T0 + 1) is False
    assert a.pending == 1
    assert a.n_duplicates == 1


def test_dedup_survives_a_flush(tmp_path) -> None:
    """THE BUG THIS FILE EXISTS FOR.

    The collector reads the whole table once a second, so a vessel reporting
    every three minutes is offered ~180 times with the same timestamp. A
    dedup set cleared on each flush caught the repeats inside the window and
    missed the ones straddling it -- about two rows per report. Nothing
    crashes; the archive just carries duplicate positions at zero-second
    intervals, and the reception grid reads the median gap between one
    vessel's reports as HALF what it is. An error that flatters coverage is
    the worst kind here, because coverage licenses every dark-vessel claim.
    """
    a = AISArchive(tmp_path, flush_rows=1)
    v = vessel()
    a.add(row_from_vessel(v, T0), T0)
    a.flush(T0)
    assert a.n_written == 1

    # Same report, after the file was written.
    assert a.add(row_from_vessel(v, T0 + 30), T0 + 30) is False
    assert a.pending == 0


def test_a_position_that_travels_backwards_is_refused(tmp_path) -> None:
    """aisstream can redeliver an older position after a newer one. Every
    reader sorts by (MMSI, BaseDateTime), so that row would not crash -- it
    would silently become a NEGATIVE interval in the cadence."""
    a = AISArchive(tmp_path)
    a.add(row_from_vessel(vessel(t=T0), T0), T0)
    assert a.add(row_from_vessel(vessel(t=T0 - 10), T0), T0) is False
    assert a.pending == 1


def test_a_vessel_that_actually_moved_is_written_again(tmp_path) -> None:
    a = AISArchive(tmp_path)
    a.add(row_from_vessel(vessel(t=T0), T0), T0)
    assert a.add(row_from_vessel(vessel(t=T0 + 120), T0 + 120), T0 + 120) is True
    assert a.pending == 2


def test_vessels_are_forgotten_so_the_map_stays_bounded(tmp_path) -> None:
    """A season of collection must not accumulate every MMSI ever heard."""
    a = AISArchive(tmp_path, forget_after_s=600.0)
    a.add(row_from_vessel(vessel(mmsi="1", t=T0), T0), T0)
    a.add(row_from_vessel(vessel(mmsi="2", t=T0), T0), T0)
    assert a.forget(T0 + 300) == 0
    assert a.forget(T0 + 900) == 2
    assert a.last_t == {}
    # And a vessel heard again after being forgotten is simply new.
    assert a.add(row_from_vessel(vessel(mmsi="1", t=T0 + 1000), T0 + 1000),
                 T0 + 1000) is True


# -- writing ----------------------------------------------------------------

def test_flush_writes_one_hour_partitioned_file(tmp_path) -> None:
    a = AISArchive(tmp_path, dataset="maritime-live")
    for i in range(5):
        a.add(row_from_vessel(vessel(mmsi=str(367000000 + i), t=T0 + i),
                              T0 + i), T0 + i)
    when = datetime(2026, 9, 23, 14, 5, tzinfo=UTC)
    path = a.flush(T0, when=when)

    assert path is not None
    assert path.parent.name == "hour=2026092314"
    assert path.parent.parent.name == "maritime-live"
    assert a.n_written == 5 and a.n_files == 1 and a.pending == 0


def test_flushing_nothing_writes_nothing(tmp_path) -> None:
    assert AISArchive(tmp_path).flush(T0) is None


def test_due_respects_both_the_row_count_and_the_clock(tmp_path) -> None:
    """A collector killed with an hour of unwritten buffer has lost an hour,
    and a push feed has no backfill -- so time triggers a write even when the
    sea is quiet."""
    a = AISArchive(tmp_path, flush_rows=1000, flush_seconds=60.0)
    a.last_flush = T0
    assert a.due(T0 + 1) is False                       # nothing buffered
    a.add(row_from_vessel(vessel(), T0), T0)
    assert a.due(T0 + 1) is False                       # too few, too soon
    assert a.due(T0 + 61) is True                       # the clock
    a.rows.extend([a.rows[0]] * 1000)
    assert a.due(T0 + 1) is True                        # the count


def test_the_written_file_is_readable_by_the_project(tmp_path) -> None:
    """End to end: write a file, then load it with the same function the
    retrospective analysis uses on MarineCadastre's bulk files."""
    pq = pytest.importorskip("pyarrow.parquet")
    a = AISArchive(tmp_path, dataset="maritime-live")
    for i in range(4):
        a.add(row_from_vessel(vessel(mmsi=str(367000000 + i),
                                     lat=37.0 + i * 0.01, t=T0 + i),
                              T0 + i), T0 + i)
    path = a.flush(T0, when=datetime(2026, 9, 23, 14, 0, tzinfo=UTC))

    table = pq.read_table(path)
    assert table.num_rows == 4
    assert set(ais.COLUMNS).issubset(set(table.column_names))

    rows = table.to_pylist()
    reports = [ais.row_to_report(r) for r in rows]
    assert all(r is not None for r in reports)
    assert {r.platform_id for r in reports} == {
        str(367000000 + i) for i in range(4)}


# -- the two live stores overlap, and a read may not span them --------------

def _touch(root, store, name="ais_1.parquet"):
    d = root / store / "hour=2026092314"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(b"")          # _parquet_files only looks at paths
    return f


def test_reading_across_the_two_live_stores_is_refused(tmp_path) -> None:
    """THE TRAP THIS GUARD EXISTS FOR.

    maritime-live-conus contains the whole of maritime-live's box, so a read
    spanning both counts every study-area vessel twice at a zero-second
    interval. Nothing errors; the reception grid simply reads the median
    reporting gap as HALF what it is, and that gap is what licenses every
    dark-vessel claim. Same failure as the per-flush dedup bug, by a
    different road.
    """
    _touch(tmp_path, "maritime-live")
    _touch(tmp_path, "maritime-live-conus")

    with pytest.raises(ais.AISReadError) as exc:
        ais._parquet_files([tmp_path / "maritime-live",
                            tmp_path / "maritime-live-conus"])
    assert "counted twice" in str(exc.value)
    # The message has to name the way out, not just the problem.
    assert "maritime-live-conus already covers" in str(exc.value)


def test_a_glob_that_happens_to_span_both_is_refused_too(tmp_path, monkeypatch) -> None:
    """The realistic version: nobody lists both by hand, they write
    data/raw/maritime-live*/ and never notice."""
    _touch(tmp_path, "maritime-live")
    _touch(tmp_path, "maritime-live-conus")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ais.AISReadError):
        ais._parquet_files("maritime-live*/hour=*/*.parquet")


def test_either_store_alone_is_fine(tmp_path) -> None:
    for store in ("maritime-live", "maritime-live-conus"):
        _touch(tmp_path, store)
    assert len(ais._parquet_files(tmp_path / "maritime-live")) == 1
    assert len(ais._parquet_files(tmp_path / "maritime-live-conus")) == 1


def test_the_bulk_store_is_untouched_by_the_guard(tmp_path) -> None:
    """The rule is about the two OVERLAPPING live datasets. Reading the
    MarineCadastre clips, or the clips beside one live store, must not trip
    it -- they do not overlap each other."""
    _touch(tmp_path, "maritime")
    _touch(tmp_path, "maritime-live")
    files = ais._parquet_files([tmp_path / "maritime", tmp_path / "maritime-live"])
    assert len(files) == 2
