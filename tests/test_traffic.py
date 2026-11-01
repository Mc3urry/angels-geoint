"""The road-speed tiles, offline: the TomTom key reaches TomTom and nothing else."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from angels.api.routes import traffic  # noqa: E402

KEY = "tomtom-test-key-not-real"


@pytest.fixture
def client(monkeypatch):
    traffic._cache.clear()
    asked = []
    monkeypatch.setattr(traffic, "_get", lambda url: asked.append(url) or b"\x89PNG flow")
    app = FastAPI()
    app.include_router(traffic.router)
    c = TestClient(app)
    c.asked = asked
    return c


def test_without_a_key_nothing_is_asked(client, monkeypatch) -> None:
    monkeypatch.delenv("TOMTOM_API_KEY", raising=False)
    assert client.get("/traffic/status").json()["flow"] is False
    assert client.get("/traffic/flow/10/290/391.png").status_code == 503 and client.asked == []


def test_the_key_stays_on_the_server(client, monkeypatch) -> None:
    monkeypatch.setenv("TOMTOM_API_KEY", KEY)
    s = client.get("/traffic/status")
    assert s.json()["flow"] is True and KEY not in s.text
    r = client.get("/traffic/flow/10/290/391.png")
    assert r.status_code == 200 and KEY not in str(r.headers)
    assert client.asked and KEY in client.asked[0] and "/flow/relative0/10/290/391.png" in client.asked[0]
    client.get("/traffic/flow/10/290/391.png")
    assert len(client.asked) == 1
    assert client.get("/traffic/flow/30/0/0.png").status_code == 404
    assert "no vehicle is identified" in s.json()["means"]
