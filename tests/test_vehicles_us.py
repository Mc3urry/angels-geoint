"""US transit and intercity rail for the 3D view, offline.

The border feeds come from the research collector and are never polled
twice; the national poller reads a feed exactly the way the collector does;
Amtraker's trains become rows with a heading in degrees and a speed in m/s.
"""

from __future__ import annotations

import json

import pytest

from angels.api.routes import vehicles as v


def test_the_border_feeds_are_left_to_the_research_collector(tmp_path) -> None:
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


def test_keyed_feeds_join_only_when_their_key_is_set(monkeypatch) -> None:
    for k in ("API_511_ORG_KEY", "MTA_BUSTIME_KEY", "SWIFTLY_API_KEY", "OBA_PUGET_SOUND_KEY",
              "TRIMET_APP_ID", "METRA_API_TOKEN", "CTA_TRAIN_KEY"):
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
