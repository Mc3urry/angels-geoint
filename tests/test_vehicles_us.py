"""US transit and intercity rail for the 3D view, offline.

The border feeds come from the research collector and are never polled
twice; the national poller reads a feed exactly the way the collector does;
Amtraker's trains become rows with a heading in degrees and a speed in m/s.
"""

from __future__ import annotations

import json

import pytest

from angels.api.routes import vehicles as v


KEY_NAMES = ("API_511_ORG_KEY", "MTA_BUSTIME_KEY", "SWIFTLY_API_KEY", "OBA_PUGET_SOUND_KEY",
             "TRIMET_APP_ID", "METRA_API_TOKEN", "CTA_TRAIN_KEY")


def test_the_border_feeds_are_left_to_the_research_collector(tmp_path, monkeypatch) -> None:
    # The keys in a real .env must not decide what this test sees.
    for k in KEY_NAMES:
        monkeypatch.delenv(k, raising=False)
    reg = tmp_path / "transit-registry.json"
    reg.write_text(json.dumps({"selected": [
        {"id": "mdb-1", "provider": "Border Transit", "url": "https://a.example/vp.pb"},
        {"id": "mdb-2", "provider": "Inland Transit", "url": "https://b.example/vp.pb"},
        {"id": "mdb-3", "provider": "No URL", "url": ""}]}))
    feeds = v.national_feeds(reg, border={"mdb-1": {}})
    assert [f["id"] for f in feeds] == ["mdb-2"]


def test_a_national_poll_reads_feeds_the_collectors_way() -> None:
    pb2 = pytest.importorskip("google.transit.gtfs_realtime_pb2")
    fv = pytest.importorskip("scripts.fetch_vehicles")
    msg = pb2.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.timestamp = 1_792_800_000
    e = msg.entity.add(id="1")
    e.vehicle.vehicle.id = "bus-7"
    e.vehicle.trip.trip_id = "t1"
    e.vehicle.position.latitude = 40.0
    e.vehicle.position.longitude = -105.0
    e.vehicle.timestamp = 1_792_800_000
    body = msg.SerializeToString()

    class FakeFv:
        def __getattr__(self, name):
            return getattr(fv, name)

        @staticmethod
        def fetch(url, key):
            return 200, body, 0.1, None, False

    p = v.NationalPoller(feeds_fn=lambda: [{"id": "mdb-2", "provider": "Inland", "url": "https://b.example"}],
                         fv=FakeFv())
    p.cycle()
    assert p.doc["feeds"] == {"mdb-2": "ok"} and p.doc["providers"] == {"mdb-2": "Inland"}
    row = p.doc["vehicles"][0]
    assert row[:2] == ["mdb-2", "bus-7"] and row[9] is True, "a vehicle on a trip is in service"


def test_one_slow_or_broken_feed_does_not_hold_the_others() -> None:
    import threading
    gate = threading.Event()

    class Fake:
        def one(self, feed):
            if feed["id"] == "slow":
                gate.wait(5)
            if feed["id"] == "broken":
                raise ValueError("not a feed")
            return feed["id"], "ok", [[feed["id"], "v1"]]

    p = v.NationalPoller(feeds_fn=lambda: [{"id": "fast"}, {"id": "slow"}, {"id": "broken"}])
    fake = Fake()
    p.one = fake.one
    p.cycle(wait_s=0.5)
    assert p.doc["feeds"] == {"fast": "ok", "slow": "slow", "broken": "error: ValueError"}
    assert [r[0] for r in p.doc["vehicles"]] == ["fast"]
    gate.set()
    p.inflight["slow"].result(timeout=5)
    p.cycle(wait_s=0.5)
    assert p.doc["feeds"]["slow"] == "ok", "a slow feed's answer is used when it comes"


def test_swiftly_feeds_share_a_budget_that_survives_a_restart(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(v, "SWIFTLY_BUDGET", 4)
    feeds = [{"id": f"sw-{i}", "url": f"https://api.goswift.ly/real-time/a{i}/gtfs-rt-vehicle-positions"}
             for i in range(6)] + [{"id": "free", "url": "https://free.example/vp.pb"}]
    asked = []

    class Fake:
        def one(self, feed):
            asked.append(feed["id"])
            return feed["id"], "ok", []

    def poller():
        p = v.NationalPoller(feeds_fn=lambda: feeds)
        p.one = Fake().one
        p.swiftly_log_path = tmp_path / "swiftly.json"
        return p

    p = poller()
    p.cycle(wait_s=1)
    assert asked.count("free") == 1 and sum(a.startswith("sw-") for a in asked) == 4, "never past the budget"
    assert {p.doc["feeds"][f"sw-{i}"] for i in range(6)} >= {"queued"}
    assert p.swiftly_every_s == v.SWIFTLY_WINDOW_S * 6 / 4, "each agency's turn is spread over the window"
    asked.clear()
    p2 = poller()                                  # the API restarted inside the window
    p2.cycle(wait_s=1)
    assert asked == ["free"], "a restart does not spend the window twice"


def test_a_swiftly_agency_that_refuses_the_key_is_left_alone(tmp_path) -> None:
    feeds = [{"id": "granted", "url": "https://api.goswift.ly/real-time/a/gtfs-rt-vehicle-positions"},
             {"id": "refused", "url": "https://api.goswift.ly/real-time/b/gtfs-rt-vehicle-positions"}]
    p = v.NationalPoller(feeds_fn=lambda: feeds)
    p.swiftly_log_path = tmp_path / "swiftly.json"
    now = 1_000_000.0
    assert p.due(feeds[1], 2, now)
    p.denied_until["refused"] = now + v.SWIFTLY_DENIED_S
    assert not p.due(feeds[1], 2, now + 3600), "not asked again for six hours"
    assert p.due(feeds[0], 2, now + 3600)
    assert p.swiftly_every_s == max(v.INTERVAL_S, v.SWIFTLY_WINDOW_S * 1 / v.SWIFTLY_BUDGET), \
        "the budget goes to the agencies that accept the key"


def test_refusals_survive_a_restart(tmp_path) -> None:
    feeds = [{"id": "refused", "url": "https://api.goswift.ly/real-time/b/gtfs-rt-vehicle-positions"}]

    class Refuse:
        def __init__(self, p):
            self.p = p

        def one(self, feed):
            self.p.denied_until[feed["id"]] = time.time() + v.SWIFTLY_DENIED_S
            return feed["id"], "http_error 403", []

    import time
    p = v.NationalPoller(feeds_fn=lambda: feeds)
    p.swiftly_log_path = tmp_path / "swiftly.json"
    p.one = Refuse(p).one
    p.cycle(wait_s=1)
    p2 = v.NationalPoller(feeds_fn=lambda: feeds)
    p2.swiftly_log_path = tmp_path / "swiftly.json"
    assert not p2.due(feeds[0], 1, time.time()), "a restart does not ask a refusing agency again"


def test_one_feed_listed_twice_is_asked_once(tmp_path, monkeypatch) -> None:
    for k in KEY_NAMES:
        monkeypatch.delenv(k, raising=False)
    reg = tmp_path / "transit-registry.json"
    reg.write_text(json.dumps({"selected": [
        {"id": "a", "url": "https://x.example/vp.pb"}, {"id": "b", "url": "https://x.example/vp.pb"}]}))
    assert [f["id"] for f in v.national_feeds(reg, border={})] == ["a"]


def test_amtraker_trains_become_rows() -> None:
    doc = {"5": [{"trainID": "5-28", "trainNum": "5", "routeName": "California Zephyr",
                  "lat": 39.7, "lon": -105.0, "heading": "W", "velocity": 50,
                  "trainState": "Active", "updatedAt": "2026-10-28T12:00:00-06:00",
                  "provider": "Amtrak", "origName": "Chicago", "destName": "Emeryville"}],
           "6": [{"trainID": "6-28", "lat": 40, "lon": -100, "trainState": "Predeparture"}]}
    rows = v.parse_amtraker(doc, 1_792_800_000)
    assert len(rows) == 1
    r = rows[0]
    assert r[0] == "5-28" and r[3] == 270 and abs(r[4] - 22.35) < 0.01
    assert r[7] == "California Zephyr" and r[9] == "Chicago"


def test_vehicles_route_without_a_collector_says_so(monkeypatch, tmp_path) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(v, "BORDER_SNAPSHOT", tmp_path / "missing.json")
    monkeypatch.setattr(v.poller, "want", lambda: None)
    monkeypatch.setattr(v.poller, "doc", None)
    app = FastAPI()
    app.include_router(v.router)
    doc = TestClient(app).get("/vehicles/us").json()
    assert doc["n"] == 0 and doc["warming"] is True
    assert "not running" in doc["border"] and doc["kept"].startswith("nothing")
    assert isinstance(doc["border_feeds"], list), "the viewers mark the research feeds"


def test_keyed_feeds_join_only_when_their_key_is_set(monkeypatch) -> None:
    for k in KEY_NAMES:
        monkeypatch.delenv(k, raising=False)
    doc = {"excluded": [
        {"id": "mdb-1574", "provider": "LA Metro", "rule": "needs a key",
         "url": "https://api.goswift.ly/real-time/lametro/gtfs-rt-vehicle-positions"},
        {"id": "mdb-1710", "provider": "TriMet", "rule": "needs a key",
         "url": "http://developer.trimet.org/ws/V1/VehiclePositions"}]}
    assert v.keyed_feeds(doc) == []
    monkeypatch.setenv("SWIFTLY_API_KEY", "swift-secret")
    monkeypatch.setenv("TRIMET_APP_ID", "tri-secret")
    monkeypatch.setenv("API_511_ORG_KEY", "bay-secret")
    feeds = {f["id"]: f for f in v.keyed_feeds(doc)}
    assert feeds["mdb-1574"]["header"] == ("Authorization", "swift-secret")
    assert feeds["mdb-1710"]["url"].endswith("?appID=tri-secret")
    assert "agency=RG" in feeds["511org-RG"]["url"]
    assert v.transit_keys()["SWIFTLY_API_KEY"] is True and v.transit_keys()["CTA_TRAIN_KEY"] is False


def test_cta_trains_and_iowa_plows_become_rows() -> None:
    cta = {"ctatt": {"route": [{"@name": "red", "train": [
        {"rn": "801", "lat": "41.88", "lon": "-87.63", "heading": "178"},
        {"rn": "802", "lat": "x", "lon": "-87.6"}]}]}}
    rows = v.parse_cta(cta, 100)
    assert rows == [["cta-train", "rn-801", "801", "red", 41.88, -87.63, 178.0, None, 100, True, "none"]]
    fc = {"features": [{"geometry": {"type": "Point", "coordinates": [-93.6, 41.6]},
                        "properties": {"LABEL": "A123", "HEADING": 90, "SPEED": 25,
                                       "LOGDT": 1_792_800_000_000}}]}
    r = v.parse_plows(fc, "IA", "Iowa DOT", 0)[0]
    assert r[0] == "IA-A123" and r[3] == 90.0 and abs(r[4] - 11.18) < 0.01 and r[5] == 1_792_800_000
