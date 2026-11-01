"""The basemap proxy, offline: the key reaches CARTO and nothing else."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from angels.api.routes import basemap  # noqa: E402

KEY = "test-key-not-a-real-one-0123456789"


@pytest.fixture
def client(monkeypatch):
    basemap._cache.clear()
    asked = []

    def fake_get(url):
        asked.append(url)
        return b"\x89PNG fake"

    monkeypatch.setattr(basemap, "_get", fake_get)
    app = FastAPI()
    app.include_router(basemap.router)
    c = TestClient(app)
    c.asked = asked
    return c


def test_without_a_key_the_page_is_told_and_no_tile_is_asked_for(client, monkeypatch) -> None:
    monkeypatch.setattr(basemap.config, "CARTO_BASEMAPS_KEY", "", raising=False)
    assert client.get("/basemap/status").json()["carto"] is False
    r = client.get("/basemap/carto/light_all/3/2/3.png")
    assert r.status_code == 503 and "CARTO_BASEMAPS_KEY" in r.json()["detail"]
    assert client.asked == []


def test_the_key_goes_to_carto_and_never_back_to_the_browser(client, monkeypatch) -> None:
    monkeypatch.setattr(basemap.config, "CARTO_BASEMAPS_KEY", KEY, raising=False)
    s = client.get("/basemap/status")
    assert s.json()["carto"] is True and KEY not in s.text
    r = client.get("/basemap/carto/light_all/3/2/3.png")
    assert r.status_code == 200 and r.content == b"\x89PNG fake"
    assert KEY not in str(r.headers)
    assert client.asked == [
        f"https://b.basemaps.cartocdn.com/light_all/3/2/3.png?key={KEY}"]
    client.get("/basemap/carto/light_all/3/2/3.png")
    assert len(client.asked) == 1, "a tile served once is served from memory"


def test_a_failure_does_not_echo_the_url(client, monkeypatch) -> None:
    monkeypatch.setattr(basemap.config, "CARTO_BASEMAPS_KEY", KEY, raising=False)

    def refuse(url):
        raise basemap.urllib.error.HTTPError(url, 403, f"Forbidden {url}", {}, None)

    monkeypatch.setattr(basemap, "_get", refuse)
    r = client.get("/basemap/carto/light_all/4/1/1.png")
    assert r.status_code == 502 and KEY not in r.text


@pytest.mark.parametrize("path", ["/basemap/carto/evil/3/2/3.png",
                                  "/basemap/carto/light_all/3/9/3.png",
                                  "/basemap/carto/light_all/25/0/0.png"])
def test_only_real_tiles_are_asked_for(client, monkeypatch, path) -> None:
    monkeypatch.setattr(basemap.config, "CARTO_BASEMAPS_KEY", KEY, raising=False)
    assert client.get(path).status_code == 404 and client.asked == []
