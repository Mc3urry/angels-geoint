"""G2c's decisions, without the network or the clock.

What is defended, in order of how quietly it would ruin G4 and G6:

  * a feed that did not answer, a feed that answered with no vehicles, and a
    feed we never asked (no credential) leave three different footprints;
  * the same fix fetched twice is one observation, and the archive says how
    many repeats it dropped;
  * a body is GTFS-Realtime because it says so, not because protobuf failed
    to object -- protobuf accepts many short byte strings as an empty message;
  * a credential travels in a header and nowhere else.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pb2 = pytest.importorskip("google.transit.gtfs_realtime_pb2")

from scripts.fetch_vehicles import (ANSWERED, COLUMNS, FETCH_ERROR,  # noqa: E402
                                    HTTP_ERROR, KEY_MISSING, NOT_GTFS_RT,
                                    NOT_POSITIONS, OK, OUTCOMES, SELECTION,
                                    TRUNCATED, append_ledger, cycle_health,
                                    cycle_sentence, cycle_summary, feed_row,
                                    key_for, load_selection, narrow,
                                    new_fixes, parse, write_positions)

FEED = {"id": "mdb-0000", "provider": "Test Transit", "states": ["24", "51"],
        "url": "https://example.org/vp.pb"}
T = 1_792_800_000


def feed_message(vehicles: list[dict] | None = None, *, header_ts: int = T,
                 trip_updates: int = 0, version: str = "2.0") -> bytes:
    msg = pb2.FeedMessage()
    msg.header.gtfs_realtime_version = version
    msg.header.timestamp = header_ts
    for i, v in enumerate(vehicles or []):
        ent = msg.entity.add()
        ent.id = v.get("entity", f"e{i}")
        vp = ent.vehicle
        if v.get("id") is not None:
            vp.vehicle.id = v["id"]
        if v.get("label"):
            vp.vehicle.label = v["label"]
        vp.position.latitude = v.get("lat", 38.9)
        vp.position.longitude = v.get("lon", -77.0)
        if "bearing" in v:
            vp.position.bearing = v["bearing"]
        if v.get("ts") is not None:
            vp.timestamp = v["ts"]
    for i in range(trip_updates):
        ent = msg.entity.add()
        ent.id = f"tu{i}"
        ent.trip_update.trip.trip_id = f"trip{i}"
    return msg.SerializeToString()


def poll(body: bytes | None, *, status: int | None = 200, previous=None,
         truncated: bool = False, error: str | None = None, **kw):
    return feed_row(FEED, status=status, body=body, elapsed_s=0.2,
                    error=error, truncated=truncated, fetched_at=T + 5,
                    previous=previous, pb2=pb2, **kw)


# -- what a body is ----------------------------------------------------------

def test_a_real_feed_parses_and_is_ok() -> None:
    rec, rows, _ = poll(feed_message([{"id": "b1", "ts": T - 4}]))
    assert rec["outcome"] == OK and rec["returned"] is True
    assert rec["n_vehicles"] == 1 and len(rows) == 1
    assert rows[0]["vehicle_key"] == "b1"
    assert rec["position_age_s_median"] == 4


@pytest.mark.parametrize("body", [
    b"<!DOCTYPE html><html><body>Service unavailable</body></html>",
    b"\x08\x01",            # parses as protobuf, carries no header
    b"",
    b"\xff\xfe\xfd\xfc garbage that is not a message",
])
def test_bytes_are_gtfs_rt_only_if_they_say_so(body) -> None:
    """Protobuf is lenient. `\\x08\\x01` is a valid encoding of SOME message,
    and a parser that only checked for an exception would call it a feed with
    zero vehicles -- an empty road, from our misreading."""
    assert parse(body, pb2) is None
    rec, rows, _ = poll(body)
    assert rec["outcome"] == NOT_GTFS_RT and rows == []
    assert rec["returned"] is False


def test_a_feed_with_no_vehicles_is_an_answer_not_a_failure() -> None:
    """Three in the morning. The agency said: nothing is moving."""
    rec, rows, _ = poll(feed_message([]))
    assert rec["outcome"] == OK and rec["returned"] is True
    assert rec["n_vehicles"] == 0 and rows == []


def test_trip_updates_at_a_vehicle_url_are_named() -> None:
    rec, rows, _ = poll(feed_message([], trip_updates=3))
    assert rec["outcome"] == NOT_POSITIONS and rows == []
    assert rec["n_entities"] == 3


def test_failures_are_told_apart() -> None:
    assert poll(b"busy", status=503)[0]["outcome"] == HTTP_ERROR
    rec = poll(None, status=None, error="URLError: getaddrinfo failed")[0]
    assert rec["outcome"] == FETCH_ERROR and rec["returned"] is False
    assert "getaddrinfo" in rec["error"]
    assert poll(feed_message([{"id": "x"}]), truncated=True)[0][
        "outcome"] == TRUNCATED


def test_a_missing_key_is_not_asked_and_says_so() -> None:
    rec, rows, prev = poll(None, status=None, previous={"a": (1, 2, 3)},
                           key_missing=True)
    assert rec["outcome"] == KEY_MISSING and rec["returned"] is False
    assert rows == [] and prev == {"a": (1, 2, 3)}


def test_every_outcome_is_a_distinct_name() -> None:
    assert len(set(OUTCOMES)) == len(OUTCOMES) == 7
    assert KEY_MISSING not in ANSWERED


# -- what a vehicle row holds ----------------------------------------------

def test_an_unsent_bearing_is_none_not_north() -> None:
    _, rows, _ = poll(feed_message([{"id": "a", "bearing": 0.0},
                                    {"id": "b"}]))
    by = {r["vehicle_key"]: r for r in rows}
    assert by["a"]["bearing"] == 0.0
    assert by["b"]["bearing"] is None


def test_a_vehicle_without_an_id_is_keyed_by_entity_and_counted() -> None:
    """Some vendors renumber entities every poll; a track on those is not a
    vehicle. The fallback is marked in the key and counted in the ledger."""
    rec, rows, _ = poll(feed_message([{"id": None, "entity": "77"}]))
    assert rows[0]["vehicle_key"] == "entity:77"
    assert rows[0]["vehicle_id"] == ""
    assert rec["n_no_vehicle_id"] == 1


def test_rows_carry_exactly_the_archive_columns() -> None:
    _, rows, _ = poll(feed_message([{"id": "a", "ts": T, "label": "7001"}]))
    assert tuple(rows[0]) == COLUMNS
    assert rows[0]["label"] == "7001" and rows[0]["header_ts"] == T


# -- one observation, not one poll ------------------------------------------

def test_the_first_poll_is_a_baseline_and_keeps_everything() -> None:
    rec, rows, state = poll(feed_message([{"id": "a", "ts": T}]))
    assert rec["baseline"] is True and rec["n_new"] == 1
    # GTFS-Realtime positions are 32-bit floats: 38.9 arrives as
    # 38.900001525878906. Compared approximately here; the dedupe compares
    # the arrived value with the arrived value, so it is exact there.
    assert list(state) == ["a"] and state["a"][0] == T
    assert state["a"][1:] == pytest.approx((38.9, -77.0), abs=1e-5)


def test_the_same_fix_fetched_twice_is_stored_once() -> None:
    body = feed_message([{"id": "a", "ts": T}, {"id": "b", "ts": T}])
    _, _, state = poll(body)
    moved = feed_message([{"id": "a", "ts": T}, {"id": "b", "ts": T + 30,
                                                 "lat": 38.91}])
    rec, rows, _ = poll(moved, previous=state)
    assert rec["baseline"] is False
    assert [r["vehicle_key"] for r in rows] == ["b"]
    assert rec["n_new"] == 1 and rec["n_repeat"] == 1


def test_a_vehicle_with_no_timestamp_is_compared_by_position() -> None:
    _, _, state = poll(feed_message([{"id": "a"}]))
    rec, rows, _ = poll(feed_message([{"id": "a"}]), previous=state)
    assert rows == [] and rec["n_repeat"] == 1 and rec["n_no_vehicle_ts"] == 1
    rec, rows, _ = poll(feed_message([{"id": "a", "lat": 39.0}]),
                        previous=state)
    assert len(rows) == 1


def test_a_failed_poll_keeps_the_comparison_state() -> None:
    """Otherwise the next good poll would be a baseline again and store
    every unchanged fix a second time."""
    _, _, state = poll(feed_message([{"id": "a", "ts": T}]))
    _, _, after_fail = poll(None, status=None, previous=state,
                            error="TimeoutError")
    assert after_fail == state
    rec, rows, _ = poll(feed_message([{"id": "a", "ts": T}]),
                        previous=after_fail)
    assert rows == [] and rec["baseline"] is False


def test_new_fixes_with_no_previous_returns_everything() -> None:
    rows = [{"vehicle_key": "a", "vehicle_ts": 1, "lat": 0.0, "lon": 0.0}]
    fresh, state = new_fixes(rows, None)
    assert fresh == rows and state == {"a": (1, 0.0, 0.0)}


# -- the cycle --------------------------------------------------------------

def rec(feed: str, outcome: str, n: int = 3) -> dict:
    return {"feed": feed, "outcome": outcome,
            "returned": outcome in ANSWERED,
            "n_vehicles": n if outcome == OK else 0,
            "n_new": n if outcome == OK else 0, "n_repeat": 0,
            "bytes_received": 100}


def test_everything_we_asked_failing_is_our_outage() -> None:
    s = cycle_summary([rec("a", FETCH_ERROR), rec("b", FETCH_ERROR),
                       rec("c", KEY_MISSING)])
    ok, why = cycle_health(s)
    assert ok is False and "all 2 feeds asked failed" in why


def test_one_feed_failing_is_their_silence() -> None:
    s = cycle_summary([rec("a", OK), rec("b", HTTP_ERROR)])
    assert cycle_health(s) == (True, None)
    assert "http_error: b" in cycle_sentence(s)


def test_nobody_asked_is_its_own_fault() -> None:
    ok, why = cycle_health(cycle_summary([rec("w", KEY_MISSING)]))
    assert ok is False and "credential" in why


def test_the_sentence_separates_quiet_unasked_and_failed() -> None:
    s = cycle_summary([rec("a", OK), rec("q", OK, n=0),
                       rec("w", KEY_MISSING), rec("d", FETCH_ERROR),
                       rec("t", NOT_POSITIONS)])
    text = cycle_sentence(s)
    # a, q and t answered (t without positions); w was never asked.
    assert "3 of 4 feeds asked answered" in text
    assert "reporting no vehicles: q;" in text, "t is not quiet, it is wrong"
    assert "NOT ASKED, no credential configured: w" in text
    assert "fetch_error: d" in text
    assert "answered without positions: t" in text


# -- credentials ------------------------------------------------------------

def test_wmata_is_keyed_by_header_and_nothing_else_is() -> None:
    url = "https://api.wmata.com/gtfs/bus-gtfsrt-vehiclepositions.pb"
    assert key_for(url, SimpleNamespace(WMATA_API_KEY="k")) == ("api_key", "k")
    assert key_for(url, SimpleNamespace(WMATA_API_KEY="")) == ("", "")
    assert key_for("https://cdn.mbta.com/realtime/VehiclePositions.pb",
                   SimpleNamespace(WMATA_API_KEY="k")) is None


def test_no_selected_url_carries_a_credential() -> None:
    feeds, _ = load_selection()
    for f in feeds:
        assert "api_key" not in f["url"].lower()
        assert "token" not in f["url"].lower()


# -- the selection ----------------------------------------------------------

def test_the_real_selection_is_g2bs_38() -> None:
    feeds, sha = load_selection(SELECTION)
    assert len(feeds) == 38 and len(sha) == 64
    ids = {f["id"] for f in feeds}
    # The same-agency decisions of G2b: one of each provisional pair kept.
    assert "tld-1127-vp" in ids and "mdb-1734" not in ids
    assert "tld-4679-vp" in ids and "mdb-2670" not in ids


def test_a_selection_that_disagrees_with_its_own_count_is_refused(
        tmp_path) -> None:
    p = tmp_path / "sel.json"
    p.write_text(json.dumps({"n_selected": 2, "selected": [FEED]}))
    with pytest.raises(ValueError, match="n_selected"):
        load_selection(p)


def test_narrowing_to_a_name_that_matches_nothing_is_an_error() -> None:
    with pytest.raises(ValueError, match="no selected feed"):
        narrow([FEED], ("mdb-9999",))
    assert narrow([FEED], ("MDB-0000",)) == [FEED]
    assert narrow([FEED], ()) == [FEED]


# -- storage ----------------------------------------------------------------

def test_the_ledger_appends(tmp_path) -> None:
    from datetime import datetime, timezone
    when = datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)
    append_ledger(tmp_path, [rec("a", OK)], when)
    p = append_ledger(tmp_path, [rec("b", FETCH_ERROR)], when)
    lines = p.read_text().splitlines()
    assert [json.loads(x)["feed"] for x in lines] == ["a", "b"]


def test_positions_round_trip_through_parquet(tmp_path) -> None:
    pq = pytest.importorskip("pyarrow.parquet")
    from datetime import datetime, timezone
    _, rows, _ = poll(feed_message([{"id": "a", "ts": T, "bearing": 90.0},
                                    {"id": None, "entity": "9"}]))
    when = datetime(2026, 10, 26, 15, 4, 5, tzinfo=timezone.utc)
    path = write_positions(rows, tmp_path, when)
    assert path.parent.name == "hour=2026102615"
    back = pq.read_table(path).to_pylist()
    assert {r["vehicle_key"] for r in back} == {"a", "entity:9"}
    nb = [r for r in back if r["vehicle_key"] == "entity:9"][0]
    assert nb["bearing"] is None and nb["vehicle_ts"] is None


# -- the loop, end to end, with the network replaced ------------------------

def test_two_cycles_leave_a_ledger_a_heartbeat_and_one_file(
        tmp_path, monkeypatch) -> None:
    """The run loop walked once, so the parts are known to fit: a keyed feed
    with no key is never fetched, a repeat is not stored twice, and the
    buffer is written when the run ends rather than lost."""
    import scripts.fetch_vehicles as fv
    pq = pytest.importorskip("pyarrow.parquet")
    monkeypatch.setattr(fv, "WMATA_API_KEY", "")
    asked: list[str] = []

    def fake_fetch(url, key):
        asked.append(url)
        return 200, feed_message([{"id": "a", "ts": T}]), 0.1, None, False

    monkeypatch.setattr(fv, "fetch", fake_fetch)
    wmata = {"id": "mdb-1850", "provider": "WMATA",
             "url": "https://api.wmata.com/gtfs/bus-gtfsrt-vehiclepositions.pb"}
    p = fv.Poller([FEED, wmata], tmp_path, interval=0.01, flush_every=10)
    p.run(max_cycles=2)

    assert asked == [FEED["url"]] * 2, "the keyed feed must never be asked"
    ledger = [json.loads(x) for f in sorted(
        (tmp_path / fv.DATASET).glob("attempts-*.jsonl"))
        for x in f.read_text().splitlines()]
    mine = [r for r in ledger if r["feed"] == FEED["id"]]
    assert [r["n_new"] for r in mine] == [1, 0]
    assert [r["baseline"] for r in mine] == [True, False]
    assert {r["outcome"] for r in ledger if r["feed"] == "mdb-1850"} == {
        KEY_MISSING}

    files = list((tmp_path / fv.DATASET).glob("hour=*/positions_*.parquet"))
    assert len(files) == 1, "flushed on exit, once"
    assert pq.read_table(files[0]).num_rows == 1

    beats = [json.loads(x) for f in tmp_path.rglob("*.jsonl")
             if "attempts-" not in f.name
             for x in f.read_text().splitlines()]
    events = [b["event"] for b in beats if b.get("collector") == fv.COLLECTOR_NAME]
    assert events[0] == "start" and events[-1] == "stop"
    assert events.count("poll") == 2
