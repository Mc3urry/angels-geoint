"""The parsed-feed measurement: cost in the unit the collector writes.

No network. `parquet_bytes` is exercised against pyarrow, which the project
already depends on; `summarise` is pure.

WHY THE BATCHED FIGURE EXISTS

The first version of this measurement wrote one parquet file per poll and
reported 2.41x against raw. **The collector does not write that file.** It
flushes every five minutes -- ten polls at a 30-second interval -- into one
file, and across ten polls the same 800 vehicle ids, trip ids and route ids
repeat ten times, which is precisely what dictionary encoding is for.

Sizing one poll was measuring a unit that will never exist on disk: the same
shape as a field called `n_polls` holding a count of files, and as a
credential guard pointed at urls while the payload sat in bodies. The
quantity next to the one that matters, measured carefully.
"""

from __future__ import annotations

import random

import pytest

from scripts.probe_transit_parse import parquet_bytes, summarise


def poll(flush: dict | None = None, **over) -> dict:
    d = {"ok": True, "parsed": True, "raw_bytes": 75_694,
         "parquet_bytes": 31_346, "vehicles": 804, "entities": 804,
         "header_ts": 1000, "vehicles_changed": 758, "lag_samples": [1, 5, 9]}
    d.update(over)
    if flush:
        d["flush"] = flush
    return d


def run(n: int = 10, batch_bytes: int = 150_000,
        sorted_bytes: int = 120_000) -> list[dict]:
    return [poll() for _ in range(n - 1)] + [poll(
        {"polls": n, "rows": 804 * n, "bytes": batch_bytes,
         "bytes_sorted": sorted_bytes})]


# -- the batched unit -------------------------------------------------------

def test_the_projection_uses_what_the_collector_will_store() -> None:
    """Not the per-poll figure, which sizes a file that never gets written."""
    s = summarise(run(), 30)
    assert "batched" in s["projection_parsed"]["basis"]
    assert s["projection_parsed"]["bytes_per_poll_used"] == 12_000


def test_without_a_flush_the_basis_says_so_rather_than_implying_it() -> None:
    """A run too short to fill a file still reports -- and names the unit it
    used, so nobody reads a per-poll projection as a storage forecast."""
    s = summarise([poll() for _ in range(3)], 30)
    assert "does NOT write" in s["projection_parsed"]["basis"]
    assert s["projection_parsed"]["bytes_per_poll_used"] == 31_346
    assert "batched" not in s


def test_the_gain_over_per_poll_is_reported_as_its_own_number() -> None:
    """The decision this measurement exists to inform is whether batching
    changes the answer, so the comparison is a field and not an inference
    left to the reader."""
    s = summarise(run(batch_bytes=150_000, sorted_bytes=120_000), 30)
    b = s["batched"]
    assert b["bytes_per_poll"] == 15_000
    assert b["bytes_per_poll_sorted"] == 12_000
    assert b["ratio_vs_raw"] == pytest.approx(5.05, abs=0.02)
    assert b["ratio_vs_raw_sorted"] == pytest.approx(6.31, abs=0.02)
    assert b["gain_over_per_poll"] == pytest.approx(2.09, abs=0.02)


def test_batching_that_buys_nothing_is_flagged() -> None:
    """If repeated identifiers are not compressing across polls, something
    is wrong with the column types -- and concluding 'the feed is
    incompressible' from that would be a wrong answer with a right shape."""
    s = summarise(run(batch_bytes=313_460, sorted_bytes=313_460), 30)
    assert s["batched"]["gain_over_per_poll"] == pytest.approx(1.0, abs=0.01)
    assert any("BATCHING BOUGHT ALMOST NOTHING" in f for f in s["flags"])


def test_a_real_gain_is_not_flagged() -> None:
    """Rule 16. A flag that fired on every run would be noise."""
    s = summarise(run(), 30)
    assert not any("BATCHING" in f for f in s["flags"])


# -- what the header and the vehicles each say ------------------------------

def test_a_moving_header_the_vehicles_corroborate_is_not_a_warning() -> None:
    """REFINED 2026-10-06.

    The first version flagged a header that changed every poll as ambiguous
    -- the feed regenerating, or a timestamp written at generation -- and
    left it there. The vehicles answer it: if most of them changed too, the
    header moves because the fleet does, and there is nothing to warn about.

    The real WMATA run has 94 per cent of vehicles changing between polls,
    so the flag fired on every single run while the evidence beside it had
    already settled the question. That is rule 16's false alarm, and a flag
    that always fires teaches the reader to skip the whole list.
    """
    s = summarise([poll(header_ts=1000 + i * 30, vehicles_changed=778)
                   for i in range(6)], 30)
    assert s["header_corroborated_by_vehicles"] is True
    assert not any("HEADER" in f for f in s["flags"])
    assert "fleet is moving" in s["note_on_header"]


def test_a_moving_header_the_vehicles_contradict_is_the_real_fault() -> None:
    """A header that advances while the fleet sits still IS a timestamp
    written at generation, and that is the case worth shouting about."""
    s = summarise([poll(header_ts=1000 + i * 30, vehicles_changed=3)
                   for i in range(6)], 30)
    assert "header_corroborated_by_vehicles" not in s
    assert any("WHILE THE VEHICLES" in f for f in s["flags"])


def test_a_header_frozen_across_every_poll_is_a_different_fault() -> None:
    s = summarise([poll(header_ts=1000) for _ in range(6)], 30)
    assert any("NEVER CHANGED" in f for f in s["flags"])
    assert not any("CHANGES EVERY POLL" in f for f in s["flags"])


def test_position_age_is_a_distribution_because_the_tail_is_the_problem() -> None:
    """A median of 5 s with a maximum of 592 s is the real shape: most
    vehicles are current and a few are ten minutes stale. Computing a
    30-second speed from a 10-minute-old fix gives a spurious near-zero that
    looks entirely plausible downstream.

    The first version of this test used five samples per poll, one of them
    592, which puts a fifth of the distribution in the "tail" -- so p90
    equalled the max and the assertion failed. The fixture was wrong, not
    the function: a value present in twenty per cent of samples is not a
    tail, and asserting a shape the fixture could not have was a claim about
    my own test data rather than about the code.
    """
    polls = [poll(lag_samples=[1, 2, 3, 4, 5, 6, 7, 8, 9, 592])
             for _ in range(4)]
    a = summarise(polls, 30)["position_age_s"]
    assert a["max"] == 592
    assert a["p10"] < a["median"] < a["p90"] < a["max"], (
        "with 592 at one sample in ten, it should sit beyond p90")
    assert a["p90"] == 9, "the ninetieth percentile is the last ordinary value"


def test_nothing_parsed_is_an_outage_not_a_measurement() -> None:
    s = summarise([{"ok": False} for _ in range(4)], 30)
    assert s["polls_parsed"] == 0
    assert "parquet_bytes_median" not in s
    assert any("NOTHING PARSED" in f for f in s["flags"])


# -- parquet, against the real library --------------------------------------

def rows(n: int = 800, vehicles: int = 80) -> list[dict]:
    return [{"vehicle_id": f"v{i % vehicles}", "trip_id": f"t{i % vehicles}",
             "route_id": f"r{i % 12}", "lat": 38.9 + i * 1e-5,
             "lon": -77.0 - i * 1e-5, "bearing": float(i % 360),
             "speed": 5.0, "vehicle_ts": 1000 + i, "current_status": 1,
             "stop_id": f"s{i % 40}", "fetched_at": 1000 + (i // vehicles)}
            for i in range(n)]


def test_sorting_shrinks_a_repeating_column() -> None:
    """Clustering each vehicle's rows together is what lets parquet encode a
    repeating identifier once. Whether it is worth doing is measured here
    rather than asserted in a comment."""
    r = rows()
    random.Random(0).shuffle(r)
    assert parquet_bytes(r, sort=True) < parquet_bytes(r)


def test_sorting_does_not_change_what_is_stored() -> None:
    """Only the order. A reordering that dropped or altered a row would be
    a compression win paid for in data."""
    import pyarrow.parquet as pq             # noqa: PLC0415
    import pyarrow as pa                     # noqa: PLC0415
    r = rows(200, 20)
    sink = pa.BufferOutputStream()
    pq.write_table(pa.Table.from_pylist(sorted(
        r, key=lambda x: (x["vehicle_id"], x["fetched_at"]))), sink,
        compression="snappy")
    back = pq.read_table(pa.BufferReader(sink.getvalue())).to_pylist()
    assert len(back) == len(r)
    assert sorted(x["vehicle_ts"] for x in back) == \
        sorted(x["vehicle_ts"] for x in r)


def test_an_empty_batch_is_zero_bytes_and_not_an_error() -> None:
    assert parquet_bytes([]) == 0
    assert parquet_bytes([], sort=True) == 0


def test_absent_is_preserved_as_null_not_as_zero() -> None:
    """A bearing of 0.0 is due north; a bearing never sent is unknown.
    Collapsing them puts every silent vehicle on a northward heading."""
    import pyarrow.parquet as pq             # noqa: PLC0415
    import pyarrow as pa                     # noqa: PLC0415
    r = [dict(x, bearing=None, speed=None) for x in rows(10, 2)]
    sink = pa.BufferOutputStream()
    pq.write_table(pa.Table.from_pylist(r), sink, compression="snappy")
    back = pq.read_table(pa.BufferReader(sink.getvalue())).to_pylist()
    assert all(x["bearing"] is None for x in back)
