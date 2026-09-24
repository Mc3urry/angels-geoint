"""Tests for the OpenSky credit budget: the viewer must not starve the archive.

On 2026-09-19 a viewer tab left open overnight asked OpenSky for the
DC-Baltimore box every eight seconds -- from the same 4,000-credit daily
budget the two collectors live on. The budget ran out at 04:20 local time and
both collectors were refused on every poll until 13:08 -- 8 h 48 min of
archive that cannot be backfilled. Meanwhile the viewer told the user
"OpenSky is not responding", which was false; OpenSky was answering clearly
that the credits were gone.

Three fixes, three groups of tests below:

  - a 429 is its own error, never an outage and never "missing credentials";
  - the collector publishes each poll and the viewer reads it, costing zero;
  - once refused, the viewer stops asking until OpenSky's stated retry time.

Nothing here reaches the network.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from angels.adapters.aviation import opensky
from angels.api.main import app
from angels.api.routes import live as live_route
from angels.config import AOI_AIR, AOI_CONUS

ROOT = Path(__file__).resolve().parents[1]
T = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)

ROW = ["abc123", "TEST123 ", "United States", 1, 1, -76.9, 38.9, 3000.0,
       False, 120.0, 90.0, 0.0, None, 3050.0, "1200", False, 0]


class FakeTokens:
    def headers(self):
        return {"Authorization": "Bearer x"}


def client_returning(status, *, headers=None, body=None):
    def handler(request):
        return httpx.Response(status, headers=headers or {},
                              json=body if body is not None else {})
    return httpx.Client(transport=httpx.MockTransport(handler))


# -- a 429 is a budget, not an outage -------------------------------------

def test_a_429_raises_rate_limited_with_the_retry_time() -> None:
    c = client_returning(429, headers={"X-Rate-Limit-Retry-After-Seconds": "7200"})
    with pytest.raises(opensky.RateLimited) as exc:
        opensky.fetch_states(AOI_AIR, FakeTokens(), client=c)
    assert exc.value.retry_after_s == 7200.0
    assert "credits" in str(exc.value)


def test_a_429_without_the_header_still_raises_rate_limited() -> None:
    c = client_returning(429)
    with pytest.raises(opensky.RateLimited) as exc:
        opensky.fetch_states(AOI_AIR, FakeTokens(), client=c)
    assert exc.value.retry_after_s is None


def test_the_remaining_budget_is_recorded_on_success() -> None:
    """OpenSky reports the day's remaining credits on every good response.
    Keeping it lets the budget be SEEN draining instead of discovered from
    a 429."""
    c = client_returning(200, headers={"X-Rate-Limit-Remaining": "1234"},
                         body={"time": 1_700_000_000, "states": [ROW]})
    t, rows = opensky.fetch_states(AOI_AIR, FakeTokens(), client=c)
    assert len(rows) == 1
    assert opensky.LAST_BUDGET["remaining"] == 1234.0


# -- the shared snapshot --------------------------------------------------

def test_a_snapshot_round_trips(tmp_path) -> None:
    p = tmp_path / "live" / "aviation.json"
    assert opensky.write_snapshot(p, T, [ROW], AOI_AIR)
    doc = opensky.read_snapshot(p, max_age_s=90, bbox=AOI_AIR)
    assert doc is not None
    assert doc["states"] == [ROW]
    assert doc["time"] == int(T.timestamp())
    assert not p.with_suffix(".tmp").exists(), "the temp file must be swapped in"


def test_a_stale_snapshot_is_refused(tmp_path) -> None:
    """A snapshot that has stopped updating means the collector stopped or is
    being refused. Serving it would show a sky frozen at the moment the
    archive broke, which is worse than showing nothing."""
    p = tmp_path / "aviation.json"
    opensky.write_snapshot(p, T, [ROW], AOI_AIR)
    doc = json.loads(p.read_text())
    doc["written"] = time.time() - 600
    p.write_text(json.dumps(doc))
    assert opensky.read_snapshot(p, max_age_s=90, bbox=AOI_AIR) is None


def test_a_snapshot_for_a_different_box_is_refused(tmp_path) -> None:
    p = tmp_path / "aviation.json"
    opensky.write_snapshot(p, T, [ROW], (-125.0, 24.0, -66.5, 49.5))
    assert opensky.read_snapshot(p, max_age_s=90, bbox=AOI_AIR) is None


@pytest.mark.parametrize("content", ["", "{not json", "[]"])
def test_a_corrupt_or_missing_snapshot_is_none_not_an_error(tmp_path, content) -> None:
    p = tmp_path / "aviation.json"
    if content:
        p.write_text(content)
    assert opensky.read_snapshot(p, max_age_s=90, bbox=AOI_AIR) is None


def test_an_empty_sky_that_was_asked_about_is_still_a_snapshot(tmp_path) -> None:
    """Zero aircraft from a poll that happened is a result. It must not be
    confused with a collector that stopped asking."""
    p = tmp_path / "aviation.json"
    opensky.write_snapshot(p, T, [], AOI_AIR)
    doc = opensky.read_snapshot(p, max_age_s=90, bbox=AOI_AIR)
    assert doc is not None and doc["states"] == []


# -- the collector publishes ----------------------------------------------

def _load_ingest():
    spec = importlib.util.spec_from_file_location(
        "ingest_aviation", ROOT / "scripts" / "ingest_aviation.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ingest_aviation"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_collector_writes_a_snapshot_every_poll(tmp_path, monkeypatch) -> None:
    ing_mod = _load_ingest()
    monkeypatch.setattr(ing_mod, "TokenManager", lambda: FakeTokens())
    monkeypatch.setattr(ing_mod, "fetch_states", lambda bbox, tokens: (T, [ROW]))
    snap = tmp_path / "live" / "aviation.json"
    ing = ing_mod.Ingester(AOI_AIR, tmp_path / "raw", 30, 30, snapshot=snap)
    assert ing.poll_once() == 1
    doc = opensky.read_snapshot(snap, max_age_s=90, bbox=AOI_AIR)
    assert doc is not None and doc["states"] == [ROW]


def test_without_a_snapshot_path_the_collector_writes_nothing_extra(
        tmp_path, monkeypatch) -> None:
    ing_mod = _load_ingest()
    monkeypatch.setattr(ing_mod, "TokenManager", lambda: FakeTokens())
    monkeypatch.setattr(ing_mod, "fetch_states", lambda bbox, tokens: (T, [ROW]))
    ing = ing_mod.Ingester(AOI_AIR, tmp_path / "raw", 30, 30)
    ing.poll_once()
    assert not list(tmp_path.rglob("*.json"))


# -- the viewer reads, and stops asking when refused -----------------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(live_route, "LIVE", tmp_path)
    monkeypatch.setattr(live_route, "_blocked_until", 0.0)
    monkeypatch.setattr(live_route, "_cache", {})
    monkeypatch.setattr(live_route, "_token_manager", lambda: FakeTokens())
    return TestClient(app), tmp_path


def test_the_viewer_serves_the_collector_and_spends_nothing(api, monkeypatch) -> None:
    """THE POINT OF THE FILE. While the collector runs, the viewer must not
    make a single OpenSky request."""
    client, live_dir = api
    opensky.write_snapshot(live_dir / "aviation.json", T, [ROW], AOI_AIR)

    def forbidden(*a, **k):
        raise AssertionError("the viewer asked OpenSky while a fresh snapshot existed")

    monkeypatch.setattr(opensky, "fetch_states", forbidden)
    r = client.get("/live?domain=air")
    assert r.status_code == 200
    props = r.json()["properties"]
    assert props["source"] == "collector"
    assert props["n"] == 1


def test_without_a_collector_the_viewer_falls_back_to_opensky(api, monkeypatch) -> None:
    client, _ = api
    calls = []
    monkeypatch.setattr(opensky, "fetch_states",
                        lambda bbox, tokens: calls.append(1) or (T, [ROW]))
    r = client.get("/live?domain=air")
    assert r.status_code == 200
    assert r.json()["properties"]["source"] == "direct"
    client.get("/live?domain=air")
    assert len(calls) == 1, "a second request inside CACHE_S must not reach upstream"


def test_a_429_is_reported_as_429_not_as_an_outage(api, monkeypatch) -> None:
    """The panel said "OpenSky is not responding" for hours. It was
    responding. And RateLimited subclasses RuntimeError, which this route
    otherwise maps to "credentials missing" -- a second wrong diagnosis
    waiting to happen."""
    client, _ = api

    def refused(bbox, tokens):
        raise opensky.RateLimited(3600.0, "spent")

    monkeypatch.setattr(opensky, "fetch_states", refused)
    r = client.get("/live?domain=air")
    assert r.status_code == 429
    detail = r.json()["detail"]
    assert "not an outage" in detail
    assert "archive" in detail, "the user must be told the collectors are affected"


def test_after_a_429_the_viewer_stops_asking(api, monkeypatch) -> None:
    """Asking again only confirms the budget is gone."""
    client, _ = api
    calls = []

    def refused(bbox, tokens):
        calls.append(1)
        raise opensky.RateLimited(3600.0, "spent")

    monkeypatch.setattr(opensky, "fetch_states", refused)
    for _ in range(5):
        assert client.get("/live?domain=air").status_code == 429
    assert len(calls) == 1


def test_a_stale_collector_does_not_freeze_the_sky(api, monkeypatch) -> None:
    """A collector that stopped 10 minutes ago must not be served as live."""
    client, live_dir = api
    p = live_dir / "aviation.json"
    opensky.write_snapshot(p, T, [ROW], AOI_AIR)
    doc = json.loads(p.read_text())
    doc["written"] = time.time() - 600
    p.write_text(json.dumps(doc))
    monkeypatch.setattr(opensky, "fetch_states", lambda bbox, tokens: (T, []))
    assert client.get("/live?domain=air").json()["properties"]["source"] == "direct"


# -- the national box: four credits a request, so it is never bought --------
#
# The DC box costs one credit and may be fetched directly when the collector
# is down. The country costs FOUR, from the same 4,000 a day, which would
# reproduce the 2026-09-19 incident four times faster. These tests are the
# guard on that, and on the second half of the same problem: a ten-minute-old
# fix drawn as though it were a current one.

CONUS_ROW = ["def456", "UAL1 ", "United States", 1, 1, -100.0, 40.0, 10000.0,
             False, 250.0, 90.0, 0.0, None, 10050.0, "1200", False, 0]


def test_the_national_box_reads_its_own_snapshot(api, monkeypatch) -> None:
    client, live_dir = api
    opensky.write_snapshot(live_dir / "aviation-conus.json", T, [CONUS_ROW],
                           AOI_CONUS)

    def forbidden(*a, **k):
        raise AssertionError("the viewer asked OpenSky for the whole country")

    monkeypatch.setattr(opensky, "fetch_states", forbidden)
    r = client.get("/live?domain=air&aoi=conus")
    assert r.status_code == 200
    props = r.json()["properties"]
    assert props["source"] == "collector"
    assert props["aoi"] == "conus"
    assert props["bbox"] == list(AOI_CONUS)
    assert r.json()["features"][0]["properties"]["icao24"] == "def456"


def test_a_stale_national_snapshot_is_never_bought_directly(api, monkeypatch) -> None:
    """THE GUARD. With no snapshot the DC box falls back to a 1-credit fetch.
    The country must refuse instead: at 4 credits a request it would empty
    the budget the archive lives on four times faster than the incident that
    made this route read a file at all."""
    client, _ = api

    def forbidden(*a, **k):
        raise AssertionError("the viewer bought a national fetch")

    monkeypatch.setattr(opensky, "fetch_states", forbidden)
    r = client.get("/live?domain=air&aoi=conus")
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert "four credits" in detail
    assert "collector.ps1 start -Aoi conus" in detail


def test_the_national_snapshot_is_not_called_stale_at_the_dc_threshold(api) -> None:
    """90 s is three missed polls on a 30 s box and a sixth of one on a
    10 min box. A fixed threshold would call every national snapshot stale
    on arrival, and the route would then refuse a working feed."""
    client, live_dir = api
    path = live_dir / "aviation-conus.json"
    opensky.write_snapshot(path, T, [CONUS_ROW], AOI_CONUS)
    # Age is measured from `written`, so backdate that rather than the poll
    # time: 400 s is stale for the DC box and fresh for the country.
    doc = json.loads(path.read_text())
    doc["written"] = time.time() - 400
    path.write_text(json.dumps(doc))

    r = client.get("/live?domain=air&aoi=conus")
    assert r.status_code == 200
    assert r.json()["properties"]["snapshot_age_s"] > live_route.SNAPSHOT_MAX_AGE_S

    # The same file, at the DC box's threshold, would be refused.
    assert opensky.read_snapshot(
        path, max_age_s=live_route.SNAPSHOT_MAX_AGE_S, bbox=AOI_CONUS) is None


def test_the_two_boxes_do_not_share_a_cache_entry(api, monkeypatch) -> None:
    """Different boxes, different answers. One cache key for both would serve
    the DC box's hundred aircraft as the nation's seven thousand."""
    client, live_dir = api
    opensky.write_snapshot(live_dir / "aviation.json", T, [ROW], AOI_AIR)
    opensky.write_snapshot(live_dir / "aviation-conus.json", T, [CONUS_ROW],
                           AOI_CONUS)
    a = client.get("/live?domain=air").json()
    c = client.get("/live?domain=air&aoi=conus").json()
    assert a["features"][0]["properties"]["icao24"] == "abc123"
    assert c["features"][0]["properties"]["icao24"] == "def456"


# -- what the client is allowed to draw ------------------------------------

def test_the_dc_box_may_be_dead_reckoned_and_the_country_may_not(api) -> None:
    """THE SECOND HALF OF GOING NATIONAL. At 30 s an airliner moves 2 km and
    projecting it forward is fair. At 10 min it moves 150 km, and the same
    projection is a confident drawing of a place the aircraft is not. The
    SERVER states the policy so the client cannot infer it wrongly."""
    client, live_dir = api
    opensky.write_snapshot(live_dir / "aviation.json", T, [ROW], AOI_AIR)
    opensky.write_snapshot(live_dir / "aviation-conus.json", T, [CONUS_ROW],
                           AOI_CONUS)

    air = client.get("/live?domain=air").json()["properties"]
    conus = client.get("/live?domain=air&aoi=conus").json()["properties"]

    assert air["dead_reckon"] is True
    assert air["poll_interval_s"] == 30.0
    assert conus["dead_reckon"] is False
    assert conus["poll_interval_s"] == 600.0
    assert "last HEARD" in conus["fix_age_note"]


def test_every_aircraft_carries_what_the_halo_needs(api) -> None:
    """Radius = speed x (server_time - last_contact). Both must be in the
    payload, or the client has to invent one of them."""
    client, live_dir = api
    opensky.write_snapshot(live_dir / "aviation-conus.json", T, [CONUS_ROW],
                           AOI_CONUS)
    body = client.get("/live?domain=air&aoi=conus").json()
    p = body["features"][0]["properties"]
    assert p["speed_mps"] == 250.0
    assert p["t"] == CONUS_ROW[4]                      # last_contact
    assert body["properties"]["server_time_unix"] == int(T.timestamp())
