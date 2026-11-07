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


# -- sharper, wider lines (2026-11-07) -------------------------------------------

def test_a_line_is_widened_and_keeps_its_colour() -> None:
    Image = pytest.importorskip("PIL.Image")
    import io
    im = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    for x in range(32):
        im.putpixel((x, 16), (43, 200, 43, 255))
    buf = io.BytesIO(); im.save(buf, "PNG")
    out = Image.open(io.BytesIO(traffic.widen(buf.getvalue(), px=2))).convert("RGBA")
    rows = [y for y in range(32) if out.getpixel((10, y))[3] > 0]
    assert rows == [14, 15, 16, 17, 18]                     # 1 px became 5
    assert {out.getpixel((10, y))[:3] for y in rows} == {(43, 200, 43)}
    assert out.getpixel((10, 10))[3] == 0                   # nothing else painted


def test_not_an_image_is_passed_through() -> None:
    assert traffic.widen(b"\x89PNG flow") is None


def test_with_pillow_the_tile_is_asked_at_512(client, monkeypatch) -> None:
    pytest.importorskip("PIL")
    monkeypatch.setenv("TOMTOM_API_KEY", KEY)
    client.get("/traffic/flow/10/290/391.png")
    assert "tileSize=512" in client.asked[0]
    assert "widened" in client.get("/traffic/status").json()["tiles"]


def test_without_pillow_the_tile_is_asked_at_256_as_before(client, monkeypatch) -> None:
    monkeypatch.setenv("TOMTOM_API_KEY", KEY)
    monkeypatch.setattr(traffic, "_pillow", lambda: None)
    r = client.get("/traffic/flow/10/290/391.png")
    assert r.status_code == 200 and r.content == b"\x89PNG flow"
    assert "tileSize=256" in client.asked[0]
    assert "install Pillow" in client.get("/traffic/status").json()["tiles"]
