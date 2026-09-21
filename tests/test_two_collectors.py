"""The two footprints must not contaminate each other.

Running a 30-second metro collector alongside a 10-minute national one is only
safe because they are kept apart at every layer: separate Parquet trees,
separate lock files, separate heartbeat streams. Each of those separations is
load-bearing in a different way, and none of them announces itself when it
breaks.

    shared archive     -> report rates, gap lengths and track continuity all
                          become weighted averages of two incomparable
                          sampling regimes. Nothing errors; every rate is
                          simply wrong.

    shared lock        -> the two refuse to run side by side, which is the
                          entire design.

    shared heartbeats  -> the national collector's downtime is attributed to
                          the metro one, so gaps.py excludes windows it should
                          have analysed and trusts windows it should not have.

So they are tested rather than assumed.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from angels.adapters.aviation.opensky import read_archive
from angels.config import AOIS
from angels.core.uptime import CollectorLock, HeartbeatLog, sessions
from scripts.ingest_aviation import SCHEMA, write_partition

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _rows(icao: str, lat: float, lon: float, n: int = 5,
          step_s: int = 30) -> dict:
    cols = {f.name: [] for f in SCHEMA}
    for i in range(n):
        t = NOW - timedelta(seconds=step_s * (n - i))
        cols["fetched_at"].append(t)
        cols["icao24"].append(icao)
        cols["callsign"].append("TEST   ")
        cols["origin_country"].append("United States")
        cols["time_position"].append(int(t.timestamp()))
        cols["last_contact"].append(int(t.timestamp()))
        cols["longitude"].append(lon)
        cols["latitude"].append(lat)
        cols["baro_altitude"].append(9000.0)
        cols["on_ground"].append(False)
        cols["velocity"].append(200.0)
        cols["true_track"].append(90.0)
        cols["vertical_rate"].append(0.0)
        cols["geo_altitude"].append(9100.0)
        cols["squawk"].append("1200")
        cols["spi"].append(False)
        cols["position_source"].append(0)
    return cols


def _write(root, dataset: str, cols: dict) -> None:
    table = pa.Table.from_pydict(cols, schema=SCHEMA)
    write_partition(table, NOW, root, dataset)


# -- archives --------------------------------------------------------------

def test_each_collector_writes_its_own_tree(tmp_path) -> None:
    _write(tmp_path, "aviation", _rows("aaaaaa", 38.9, -77.0))
    _write(tmp_path, "aviation-conus", _rows("bbbbbb", 41.9, -87.6))

    assert (tmp_path / "aviation").is_dir()
    assert (tmp_path / "aviation-conus").is_dir()


def test_reading_one_archive_never_returns_the_other(tmp_path) -> None:
    """The contamination that would not look like a bug.

    Chicago traffic appearing in a DC-box query would read as aircraft outside
    the SFRA, which is precisely the comparison the thesis rests on.
    """
    _write(tmp_path, "aviation", _rows("aaaaaa", 38.9, -77.0))
    _write(tmp_path, "aviation-conus", _rows("bbbbbb", 41.9, -87.6))

    box = (-180.0, -90.0, 180.0, 90.0)      # deliberately global
    start, end = NOW - timedelta(hours=1), NOW + timedelta(hours=1)

    air = read_archive(tmp_path, start, end, box, dataset="aviation")
    conus = read_archive(tmp_path, start, end, box, dataset="aviation-conus")

    assert {r.platform_id for r in air} == {"aaaaaa"}
    assert {r.platform_id for r in conus} == {"bbbbbb"}


def test_the_default_dataset_is_still_the_legacy_one(tmp_path) -> None:
    """Existing callers that never heard of datasets must keep working."""
    _write(tmp_path, "aviation", _rows("aaaaaa", 38.9, -77.0))
    got = read_archive(tmp_path, NOW - timedelta(hours=1),
                       NOW + timedelta(hours=1), (-180.0, -90.0, 180.0, 90.0))
    assert {r.platform_id for r in got} == {"aaaaaa"}


# -- locks -----------------------------------------------------------------

def test_different_collectors_may_hold_locks_at_once(tmp_path) -> None:
    """The whole point of the change. A single global lock would make the
    two-collector design impossible while looking like a safety feature."""
    a = CollectorLock(tmp_path, "aviation", session_id="aaa")
    b = CollectorLock(tmp_path, "aviation-conus", session_id="bbb")
    a.acquire()
    try:
        b.acquire()          # must not raise
        b.release()
    finally:
        a.release()


def test_each_collector_gets_its_own_lock_file(tmp_path) -> None:
    a = CollectorLock(tmp_path, "aviation", session_id="aaa")
    b = CollectorLock(tmp_path, "aviation-conus", session_id="bbb")
    assert a.path != b.path
    a.acquire()
    b.acquire()
    try:
        locks = sorted(p.name for p in (tmp_path / "collector").glob("*.lock"))
        assert locks == ["aviation-conus.lock", "aviation.lock"]
    finally:
        a.release()
        b.release()


def test_a_duplicate_of_one_collector_is_still_refused(tmp_path) -> None:
    """Loosening the lock to allow two NAMES must not loosen it to allow two
    of the same name -- that is the double-quota bug it was built for."""
    from angels.core.uptime import AlreadyRunning

    held = CollectorLock(tmp_path, "aviation-conus", session_id="first")
    held.acquire()
    # Rewrite the lock as another live process. acquire() exempts its own PID
    # to avoid self-deadlock, so a same-process test would pass vacuously.
    held.path.write_text(
        '{"pid": %d, "session": "first", "collector": "aviation-conus", '
        '"iso": "2026-01-01T00:00:00+00:00"}' % os.getppid(), encoding="utf-8")

    second = CollectorLock(tmp_path, "aviation-conus", session_id="second")
    with pytest.raises(AlreadyRunning):
        second.acquire()


# -- heartbeats ------------------------------------------------------------

def test_uptime_is_tracked_per_collector(tmp_path) -> None:
    """One collector being down must not mark the other blind.

    If it did, gaps.py would discard windows the running collector observed
    perfectly well -- silently shrinking the study period, in a way that never
    shows up as an error and only ever reduces the number of findings.
    """
    air = HeartbeatLog(tmp_path, collector="aviation", interval_s=30)
    air.start()
    air.poll(ok=True, n=12)

    conus = HeartbeatLog(tmp_path, collector="aviation-conus", interval_s=600)
    conus.start()
    conus.poll(ok=True, n=7000)
    conus.stop(reason="signal")          # national one dies; metro keeps going

    start, end = NOW - timedelta(minutes=5), NOW + timedelta(minutes=5)

    air_s = sessions(tmp_path, start, end, collector="aviation")
    conus_s = sessions(tmp_path, start, end, collector="aviation-conus")

    assert len(air_s) == 1
    assert len(conus_s) == 1

    # The distinction that matters: one ended, one did not, and the log knows
    # which is which. Merge the two streams and this becomes unanswerable.
    assert air_s[0].ended is None, "the running collector was marked stopped"
    assert conus_s[0].ended is not None, "the stopped collector looks alive"
    assert air_s[0].collector == "aviation"
    assert conus_s[0].collector == "aviation-conus"


def test_heartbeats_record_which_collector_wrote_them(tmp_path) -> None:
    HeartbeatLog(tmp_path, collector="aviation-conus", interval_s=600).start()
    got = sessions(tmp_path, NOW - timedelta(minutes=5),
                   NOW + timedelta(minutes=5), collector="aviation")
    assert got == [], "a conus heartbeat was attributed to the air collector"


# -- the registry drives it all --------------------------------------------

def test_registry_names_match_what_the_writers_use(tmp_path) -> None:
    """config.AOIS is the single source of truth. This fails if a dataset name
    is changed there without the archive path following it."""
    for name, a in AOIS.items():
        _write(tmp_path, a["dataset"], _rows("cccccc", 38.9, -77.0))
        assert (tmp_path / a["dataset"]).is_dir(), name


# -- an error must not look like an empty sky ------------------------------

def test_a_missing_archive_is_empty_not_an_error(tmp_path) -> None:
    """Normal before the collector has ever run. Not a failure."""
    from angels.adapters.aviation.opensky import read_archive
    got = read_archive(tmp_path, NOW - timedelta(hours=1),
                       NOW + timedelta(hours=1), (-180.0, -90.0, 180.0, 90.0))
    assert got == []


def test_a_failed_read_raises_instead_of_returning_nothing(tmp_path,
                                                           monkeypatch) -> None:
    """THE FOURTH REGRESSION TEST, and the one that matters most.

    read_archive used to wrap its query in `except Exception: return []`, on
    the reasoning that an empty archive is normal. It is -- but the handler
    swallowed real failures too. When pandas was missing from a fresh
    environment, every caller was calmly told there were no aircraft: the map
    went blank and run_detectors.py printed "No tracks in the archive. Is the
    collector running?" while the collector ran perfectly.

    An error reported as an absence is precisely what this project exists to
    detect, and it had been living inside the project the whole time.
    """
    import angels.adapters.aviation.opensky as osk

    _write(tmp_path, "aviation", _rows("aaaaaa", 38.9, -77.0))

    class _Boom:
        @staticmethod
        def sql(_):
            raise RuntimeError("Required module 'pandas' failed to import")

    monkeypatch.setitem(__import__("sys").modules, "duckdb", _Boom)

    with pytest.raises(osk.ArchiveReadError) as e:
        osk.read_archive(tmp_path, NOW - timedelta(hours=1),
                         NOW + timedelta(hours=1),
                         (-180.0, -90.0, 180.0, 90.0))
    # The message must name the archive AND the underlying cause, so the
    # reader is not sent to look at the collector.
    assert "aviation" in str(e.value)
    assert "pandas" in str(e.value)


def test_the_query_does_not_select_the_timezone_aware_column(tmp_path) -> None:
    """fetched_at is TIMESTAMP WITH TIME ZONE, and DuckDB needs pytz to hand
    one back to Python. Nothing uses it -- every Report time comes from the
    integer unix seconds -- so selecting it would add a dependency to carry a
    value that is discarded."""
    from angels.adapters.aviation.opensky import _REPORT_COLUMNS
    assert "fetched_at" not in _REPORT_COLUMNS


def test_every_field_the_translator_reads_is_selected() -> None:
    """If a column is dropped from the SELECT, archive_row_to_report silently
    gets None for it -- producing Reports with no altitude or no speed rather
    than an error."""
    from angels.adapters.aviation.opensky import _REPORT_COLUMNS
    for needed in ("icao24", "latitude", "longitude", "time_position",
                   "last_contact", "geo_altitude", "baro_altitude",
                   "velocity", "true_track"):
        assert needed in _REPORT_COLUMNS, f"{needed} is read but not selected"
