"""The 3D view's context layers, parsed offline. No network.

Every payload says it is context and not evidence; malformed input is
skipped, never guessed; a failed source is a 502, never an empty layer.
"""

from __future__ import annotations

import json

import pytest

from angels.core import context as ctx

ISS = ("ISS (ZARYA)",
       "1 25544U 98067A   26300.50000000  .00016717  00000-0  10270-3 0  9005",
       "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.49815327 12345")


def test_tle_triples_are_parsed_and_junk_is_skipped() -> None:
    text = "\n".join(["garbage line", *ISS, "", "NOT A SAT", "1 short", "2 short"])
    sats = ctx.parse_tle(text, "stations")
    assert [s["name"] for s in sats] == ["ISS (ZARYA)"]
    assert sats[0]["line1"].startswith("1 25544") and sats[0]["group"] == "stations"


def test_alpr_is_asked_for_the_whole_country_once() -> None:
    q = ctx.overpass_query_us()
    assert '"surveillance:type"="ALPR"' in q and '"ISO3166-1"="US"' in q


def test_alpr_rows_are_locations_and_tags_only() -> None:
    doc = ctx.slim_alpr({"elements": [
        {"type": "node", "id": 1, "lat": 38.9, "lon": -77.0,
         "tags": {"operator": "City", "surveillance:type": "ALPR", "camera:direction": "90"}},
        {"type": "way", "id": 2}]})
    assert doc["n"] == 1 and doc["rows"][0] == [-77.0, 38.9, 1, "City", "", "90"]
    assert "viewer-only" in doc["scope"] and "not evidence" in doc["evidence"]
    with pytest.raises(ValueError):
        ctx.slim_alpr({"remark": "runtime error: query timed out"})


def test_the_routes_answer_without_the_network(monkeypatch, tmp_path) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from angels.api.routes import context as route

    route._cache.clear()
    monkeypatch.setattr(route, "ALPR_CACHE", tmp_path / "alpr-us.json")
    asked = []

    def fake_get(url, data=None, timeout=None):
        asked.append(url)
        if "celestrak" in url:
            return "\n".join(ISS).encode()
        if "overpass" in url:
            return json.dumps({"elements": [{"type": "node", "id": 5, "lat": 30.0, "lon": -97.0}]}).encode()
        raise route.urllib.error.URLError("offline")

    monkeypatch.setattr(route, "_get", fake_get)
    app = FastAPI()
    app.include_router(route.router)
    c = TestClient(app)
    assert c.get("/context/satellites").json()["n"] == 1
    assert c.get("/context/alpr").json()["n"] == 1
    assert (tmp_path / "alpr-us.json").exists(), "the country is cached for a restart"

    # A restart reads the file instead of asking Overpass for the country again.
    route._cache.clear()
    asked.clear()
    assert c.get("/context/alpr").json()["n"] == 1 and asked == []

    for gone in ("/context/quakes", "/context/fires", "/context/radar/3/1/1.png"):
        assert c.get(gone).status_code == 404, f"{gone} was removed on 2026-10-28"


def test_a_failed_source_is_a_502_not_an_empty_layer(monkeypatch, tmp_path) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from angels.api.routes import context as route

    route._cache.clear()
    monkeypatch.setattr(route, "ALPR_CACHE", tmp_path / "none.json")

    def offline(url, data=None, timeout=None):
        raise route.urllib.error.URLError("offline")

    monkeypatch.setattr(route, "_get", offline)
    app = FastAPI()
    app.include_router(route.router)
    r = TestClient(app).get("/context/alpr")
    assert r.status_code == 502 and "did not answer" in r.json()["detail"]
