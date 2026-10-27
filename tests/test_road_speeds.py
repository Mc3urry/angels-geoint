"""G4's speed measurement, against fixes whose answer is known.

What is defended:

  * a speed is two fixes of one vehicle on the vehicle's own clock, and every
    pair that is not one is counted by reason, never dropped silently;
  * local time follows the US rule, so DC, St. Louis and El Paso keep their
    own clocks across the end of daylight saving inside the window;
  * the proposed anomaly rule compares a cell with itself, so a bus stop that
    is slow every day is not an anomaly and a cell that is slow on one day is.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scripts.road_speeds import (FEED_STD_OFFSET_H, N_BINS, SELECTION, Stream,
                                 anomalies, cell_centre, cell_of, cell_rows,
                                 check_offsets, local_time, metres, pair,
                                 quantile, run)

T0 = int(datetime(2026, 10, 27, 20, 0, tzinfo=timezone.utc).timestamp())


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc).timestamp()


# -- the clock ------------------------------------------------------------

@pytest.mark.parametrize("ts,off,expected", [
    (utc(2026, 10, 27, 20, 0), -5, "2026-10-27 16:00"),   # EDT
    (utc(2026, 11, 2, 20, 0), -5, "2026-11-02 15:00"),    # EST
    (utc(2026, 10, 27, 20, 0), -7, "2026-10-27 14:00"),   # MDT, El Paso
    (utc(2026, 11, 1, 5, 59), -5, "2026-11-01 01:59"),    # last EDT minute
    (utc(2026, 11, 1, 6, 0), -5, "2026-11-01 01:00"),     # first EST minute
    (utc(2027, 3, 14, 6, 59), -5, "2027-03-14 01:59"),    # last EST minute
    (utc(2027, 3, 14, 7, 0), -5, "2027-03-14 03:00"),     # first EDT minute
])
def test_local_time_follows_the_us_rule(ts, off, expected) -> None:
    assert local_time(ts, off).strftime("%Y-%m-%d %H:%M") == expected


def test_every_selected_feed_has_a_clock() -> None:
    sel = json.loads(SELECTION.read_text(encoding="utf-8"))
    check_offsets(r["id"] for r in sel["selected"])
    assert len(FEED_STD_OFFSET_H) >= len(sel["selected"])


def test_a_feed_without_a_clock_is_refused_not_defaulted() -> None:
    with pytest.raises(SystemExit, match="mdb-9999"):
        check_offsets(["mdb-1850", "mdb-9999"])


# -- space ----------------------------------------------------------------

def test_cells_are_about_250_m_on_a_side_at_dc() -> None:
    r, c = cell_of(38.9, -77.0)
    lat, lon = cell_centre(r, c)
    lat2, lon2 = cell_centre(r + 1, c)
    lat3, lon3 = cell_centre(r, c + 1)
    # Rows are staggered (see cell_of), so the row step is measured on one
    # meridian and the column step along one parallel.
    assert 240 < metres(lat, lon, lat2, lon) < 260
    assert 240 < metres(lat, lon, lat3, lon3) < 260
    assert lat3 == lat
    assert cell_of(lat, lon) == (r, c)


# -- pairs ----------------------------------------------------------------

def fx(ts, lat=38.9, lon=-77.0, key="v1", feed="mdb-1850", trip="t1"):
    return {"feed_id": feed, "vehicle_key": key, "trip_id": trip, "lat": lat,
            "lon": lon, "vehicle_ts": ts, "fetched_at": (ts or T0) + 3}


@pytest.mark.parametrize("dt,dlat,reason", [
    (0, 0.0, "duplicate"), (-30, 0.0, "out_of_order"), (10, 0.0, "too_close"),
    (300, 0.0, "gap"), (30, 0.1, "impossible"),
])
def test_every_rejected_pair_has_a_reason(dt, dlat, reason) -> None:
    v, why = pair(fx(T0), fx(T0 + dt, lat=38.9 + dlat))
    assert v is None and why == reason


def test_a_bus_moving_300_m_in_30_s_is_10_m_per_s() -> None:
    v, why = pair(fx(T0), fx(T0 + 30, lat=38.9 + 300 / 111_195))
    assert why is None and v == pytest.approx(10.0, rel=0.01)


def test_the_stream_counts_everything_it_does_not_use() -> None:
    s = Stream()
    for r in [fx(T0), fx(T0 + 30, lat=38.9005), fx(T0 + 30),       # dup
              fx(None), fx(T0, key="entity:7"), fx(T0 + 400, lat=38.91)]:
        s.fix(r)
    c = s.counts["mdb-1850"]
    assert c["fixes"] == 6
    assert (c["first_fix"], c["pairs"], c["duplicate"], c["no_vehicle_ts"],
            c["entity_key"], c["gap"]) == (1, 1, 1, 1, 1, 1)


def test_a_pair_lands_in_its_local_hour() -> None:
    s = Stream()
    s.fix(fx(T0))
    s.fix(fx(T0 + 30, lat=38.9005))
    (key,) = s.cells.hist
    assert key[2:] == ("2026-10-27", 16), "20:00 UTC is 16:00 in DC in October"


def test_the_quantile_reads_the_histogram() -> None:
    h = [0] * N_BINS
    h[0], h[20] = 1, 3                  # 0.25 m/s once, 10.25 m/s three times
    assert quantile(h, 0.5) == 10.25 and quantile(h, 0.0) == 0.25
    assert quantile([0] * N_BINS, 0.5) is None


def test_pairs_cross_file_boundaries(tmp_path) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    for i, (hour, ts) in enumerate((("2026102719", T0), ("2026102720", T0 + 30))):
        d = tmp_path / f"hour={hour}"
        d.mkdir()
        pq.write_table(pa.Table.from_pylist([fx(ts, lat=38.9 + i * 0.002)]),
                       d / f"positions_{hour}0000.parquet")
    s = run(sorted(tmp_path.glob("hour=*/*.parquet")))
    assert s.counts["mdb-1850"]["pairs"] == 1
    rows = cell_rows(s.cells)
    assert rows[0]["n_pairs"] == 1 and rows[0]["n_vehicles"] == 1


# -- the proposed anomaly rule ----------------------------------------------

def cellhour(day, median, row=1, n=10, vehicles=3, weekday=1, hour=17):
    return {"cell_row": row, "cell_col": 1, "local_date": day,
            "local_hour": hour, "weekday": weekday, "n_pairs": n,
            "n_vehicles": vehicles, "median_mps": median}


RULE = dict(ratio=0.5, min_pairs=5, min_vehicles=2, min_days=5)


def test_a_cell_slow_on_one_day_is_an_anomaly() -> None:
    rows = [cellhour(f"2026-10-{d}", 10.0) for d in (19, 20, 21, 22)] + \
           [cellhour("2026-10-23", 3.0)]
    out = anomalies(rows, **RULE)
    assert [r["local_date"] for r in out] == ["2026-10-23"]
    assert out[0]["baseline_mps"] == 10.0


def test_a_stop_slow_every_day_is_not() -> None:
    rows = [cellhour(f"2026-10-{d}", 1.0) for d in (19, 20, 21, 22, 23)]
    assert anomalies(rows, **RULE) == []


def test_too_few_days_is_no_baseline_and_no_anomaly() -> None:
    rows = [cellhour("2026-10-22", 10.0), cellhour("2026-10-23", 1.0)]
    assert anomalies(rows, **RULE) == []


def test_thin_cell_hours_do_not_count() -> None:
    rows = [cellhour(f"2026-10-{d}", 10.0) for d in (19, 20, 21, 22)] + \
           [cellhour("2026-10-23", 1.0, vehicles=1)]
    assert anomalies(rows, **RULE) == []


def test_weekdays_and_weekends_are_separate_baselines() -> None:
    weekday = [cellhour(f"2026-10-{d}", 10.0) for d in (19, 20, 21, 22, 23)]
    weekend = [cellhour(f"2026-10-{d}", 4.0, weekday=6) for d in (4, 11, 18, 25, 26)]
    assert anomalies(weekday + weekend, **RULE) == []


def test_finished_dates_are_handed_off_and_late_pairs_counted(tmp_path) -> None:
    """A month must not sit in memory. A date is written once the files are
    FLUSH_LAG_H past it; a pair that still lands on it afterwards is `late`."""
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    day1 = int(utc(2026, 10, 26, 18, 0))                  # 14:00 EDT on the 26th
    day2 = int(utc(2026, 10, 27, 19, 0))                  # 15:00 EDT on the 27th
    plan = [("2026102618", [fx(day1), fx(day1 + 30, lat=38.9005)]),
            ("2026102720", [fx(day2, key="v9"), fx(day2 + 30, lat=38.9005, key="v9")]),
            ("2026102721", [fx(day1 + 60, lat=38.901, key="v2"),
                            fx(day1 + 90, lat=38.9015, key="v2")])]
    for hour, rows in plan:
        d = tmp_path / f"hour={hour}"
        d.mkdir()
        pq.write_table(pa.Table.from_pylist(rows), d / f"positions_{hour}0000.parquet")
    handed = []
    s = run(sorted(tmp_path.glob("hour=*/*.parquet")),
            sink=lambda c: handed.append(sorted({k[2] for k in c.hist})))
    assert handed == [["2026-10-26"], ["2026-10-27"]], "each date handed off once"
    assert s.counts["mdb-1850"]["late"] == 1, (
        "v2's pair is on the 26th, which was already written")


def test_a_parked_bus_out_of_service_is_not_traffic() -> None:
    """SEPTA, 2026-10-27: 82 per cent of bus fixes had no trip, and the
    feed's median speed was 0.2 m/s. Standing still without a trip is a
    depot. Standing still WITH a trip is a dwell at a stop, which is traffic,
    and moving without a trip is a deadhead, which is traffic too."""
    s = Stream()
    for r in (fx(T0, trip=""), fx(T0 + 30, trip="")):              # parked
        s.fix(r)
    for r in (fx(T0, key="v2"), fx(T0 + 30, key="v2")):            # dwell
        s.fix(r)
    for r in (fx(T0, key="v3", trip=""),                           # deadhead
              fx(T0 + 30, lat=38.9005, key="v3", trip="")):
        s.fix(r)
    c = s.counts["mdb-1850"]
    assert c["parked"] == 1 and c["pairs"] == 2
