"""Place search, offline. No network: every upstream call is faked.

A typed coordinate needs no lookup; TomTom is used when its key is set and
the key never appears in an answer; Nominatim is the keyless fallback; an
upstream failure is a 502 that names the source, never an empty list.
"""

from __future__ import annotations

import json

import pytest

from angels.api.routes import geo

TOMTOM_DOC = {"results": [
    {"type": "Geography", "entityType": "Municipality", "position": {"lat": 38.89, "lon": -77.03},
     "address": {"freeformAddress": "Washington, DC", "countrySubdivision": "DC", "country": "United States"},
     "viewport": {"topLeftPoint": {"lat": 38.99, "lon": -77.12}, "btmRightPoint": {"lat": 38.79, "lon": -76.91}}},
    {"type": "POI", "position": {"lat": 38.85, "lon": -77.04}, "poi": {"name": "Reagan National Airport"},
     "address": {"freeformAddress": "Arlington, VA 22202"}},
    {"type": "POI", "position": {}, "poi": {"name": "no position"}}]}

NOMINATIM_DOC = [{"lat": "39.29", "lon": "-76.61", "name": "Baltimore", "type": "city",
                  "display_name": "Baltimore, Maryland, United States",
                  "boundingbox": ["39.19", "39.37", "-76.71", "-76.52"]},
                 {"lat": "x", "lon": "-76"}]


def test_typed_coordinates_need_no_lookup() -> None:
    r = geo.as_coordinates("38.8977, -77.0365")
    assert r["lat"] == 38.8977 and r["lon"] == -77.0365 and r["kind"] == "coordinates"
    assert geo.as_coordinates("38.9 -77.0")["lon"] == -77.0
    assert geo.as_coordinates("95, 10") is None and geo.as_coordinates("Baltimore") is None


def test_queries_are_cleaned() -> None:
    assert geo.clean_query("  Balti\x00more \n  MD ") == "Baltimore MD"
    assert len(geo.clean_query("x" * 500)) == 120


def test_tomtom_and_nominatim_answers_become_the_same_rows() -> None:
    t = geo.parse_tomtom(TOMTOM_DOC)
    assert [r["name"] for r in t] == ["Washington, DC", "Reagan National Airport"]
    assert t[0]["bbox"] == [-77.12, 38.79, -76.91, 38.99] and t[1]["bbox"] is None
    assert t[1]["label"] == "Arlington, VA 22202"
    n = geo.parse_nominatim(NOMINATIM_DOC)
    assert len(n) == 1 and n[0]["name"] == "Baltimore" and n[0]["bbox"] == [-76.71, 39.19, -76.52, 39.37]


def _client():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(geo.router)
    return TestClient(app)


def test_tomtom_is_used_with_a_key_that_never_comes_back(monkeypatch) -> None:
    c = _client()
    geo._cache.clear()
    secret = "tomtom-secret-0123456789"
    monkeypatch.setenv(geo.TOMTOM_ENV, secret)
    asked = []

    def fake_get(url):
        asked.append(url)
        return json.dumps(TOMTOM_DOC).encode()

    monkeypatch.setattr(geo, "_get", fake_get)
    r = c.get("/geo/search", params={"q": "washington", "lon": -77, "lat": 38.9})
    assert r.status_code == 200 and r.json()["source"] == "TomTom" and len(r.json()["results"]) == 2
    assert secret not in r.text and "api.tomtom.com" in asked[0]
    c.get("/geo/search", params={"q": "Washington", "lon": -77.01, "lat": 38.91})
    assert len(asked) == 1, "the same search near the same place is answered from the cache"


def test_without_a_key_nominatim_answers(monkeypatch) -> None:
    c = _client()
    geo._cache.clear()
    monkeypatch.delenv(geo.TOMTOM_ENV, raising=False)
    monkeypatch.setattr(geo, "NOMINATIM_GAP_S", 0)
    asked = []
    monkeypatch.setattr(geo, "_get", lambda url: asked.append(url) or json.dumps(NOMINATIM_DOC).encode())
    doc = c.get("/geo/search", params={"q": "baltimore"}).json()
    assert doc["source"] == "OpenStreetMap Nominatim" and doc["results"][0]["name"] == "Baltimore"
    assert "nominatim.openstreetmap.org" in asked[0]


def test_a_failed_source_is_a_502_and_coordinates_never_ask(monkeypatch) -> None:
    c = _client()
    geo._cache.clear()
    monkeypatch.setenv(geo.TOMTOM_ENV, "k-0123456789")

    def offline(url):
        raise geo.urllib.error.URLError("offline")

    monkeypatch.setattr(geo, "_get", offline)
    r = c.get("/geo/search", params={"q": "nowhere"})
    assert r.status_code == 502 and "did not answer" in r.json()["detail"] and "k-0123456789" not in r.text
    assert c.get("/geo/search", params={"q": "38.9, -77"}).json()["source"] == "typed coordinates"
    assert c.get("/geo/search", params={"q": "x"}).status_code == 422
