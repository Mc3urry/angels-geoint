"""Tests for /live?domain=sea.

Nothing here opens a socket. A stream is built by hand, fed messages, and
injected into the route, so the whole path from AIS frame to GeoJSON is
exercised without a network.

The route's job is not only to serve positions. It is to serve them with
enough context that a client cannot mistake a cold socket for an empty sea,
and most of what is checked below is that context.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from angels.adapters.maritime import aisstream
from angels.api.main import app
from angels.api.routes import live as live_route


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def warm(monkeypatch):
    """A stream that has been listening long enough AND stopped finding new
    vessels.

    Both, because the warm-up decision is no longer a stopwatch. Measuring
    the real box showed it was still discovering 123 vessels a minute at two
    minutes, so uptime alone proves nothing -- the stream is believed once
    its discovery rate has decayed away from its peak.
    """
    s = aisstream.Stream((-77.2, 36.0, -71.0, 39.6), api_key="test")
    s._started_at = time.time() - 600.0
    s._connected = True
    s._peak_rate = 120.0
    s._discovered = []
    monkeypatch.setattr(live_route, "sea_stream", lambda: s)

    async def _noop() -> None:
        return None

    monkeypatch.setattr(s, "start", _noop)
    return s


def position(mmsi, lat=37.0, lon=-76.0, sog=8.0, cog=180.0):
    return {"MessageType": "PositionReport", "MetaData": {"MMSI": mmsi},
            "Message": {"PositionReport": {
                "Latitude": lat, "Longitude": lon, "Sog": sog, "Cog": cog}}}


def static(mmsi, ship_type, name="SHIP"):
    return {"MessageType": "ShipStaticData", "MetaData": {"MMSI": mmsi},
            "Message": {"ShipStaticData": {"Name": name, "Type": ship_type,
                                           "Dimension": {"A": 100, "B": 100,
                                                         "C": 15, "D": 15}}}}


# -- the shape -------------------------------------------------------------

def test_sea_returns_geojson_with_vessels(client, warm) -> None:
    warm.ingest_message(position("366000001"))
    warm.ingest_message(static("366000001", 70, "MAERSK X"))
    r = client.get("/live?domain=sea")
    assert r.status_code == 200
    doc = r.json()
    assert doc["type"] == "FeatureCollection"
    assert len(doc["features"]) == 1
    p = doc["features"][0]["properties"]
    assert p["domain"] == "sea" and p["subtype"] == "cargo"
    assert p["label"] == "MAERSK X"


def test_air_is_still_the_default_domain(client) -> None:
    """Adding a domain must not change what an existing client gets."""
    r = client.get("/live")
    # 200 with data, or 503/502 without credentials -- but never a sea payload.
    if r.status_code == 200:
        assert r.json()["properties"]["domain"] == "air"


def test_an_unknown_domain_is_rejected_by_validation(client) -> None:
    assert client.get("/live?domain=orbit").status_code == 422


# -- the cold-start contract -----------------------------------------------

def test_a_warming_stream_says_so(client, monkeypatch) -> None:
    """THE POINT OF THE FILE. Four seconds after connecting, the table holds
    whatever happened to transmit in four seconds. A client that renders the
    count without seeing this flag is reporting a cold socket as an empty
    sea -- the error this project has a standing rule against."""
    s = aisstream.Stream((-77.2, 36.0, -71.0, 39.6), api_key="test")
    s._started_at = time.time() - 4.0
    s._connected = True
    monkeypatch.setattr(live_route, "sea_stream", lambda: s)

    async def _noop() -> None:
        return None

    monkeypatch.setattr(s, "start", _noop)

    props = client.get("/live?domain=sea").json()["properties"]
    assert props["warming"] is True
    assert props["listening_s"] < aisstream.WARMUP_S
    assert props["n"] == 0


def test_a_settled_stream_does_not_claim_to_be_warming(client, warm) -> None:
    props = client.get("/live?domain=sea").json()["properties"]
    assert props["warming"] is False
    assert props["connected"] is True


def test_every_sea_payload_carries_the_cold_start_fields(client, warm) -> None:
    """They are a contract, not diagnostics. A client is entitled to rely on
    them being present on every response, including a successful one."""
    props = client.get("/live?domain=sea").json()["properties"]
    for key in ("warming", "listening_s", "connected", "n_messages",
                "last_message_age_s", "stream_error"):
        assert key in props, key


# -- the evidence contract -------------------------------------------------

def test_both_domains_declare_themselves_cooperative(client, warm) -> None:
    """A map of self-reported positions is visually indistinguishable from a
    map of reality. The payload has to carry the distinction the pixels
    cannot."""
    assert client.get("/live?domain=sea").json()[
        "properties"]["evidence"] == "cooperative"
    r = client.get("/live?domain=air")
    if r.status_code == 200:
        assert r.json()["properties"]["evidence"] == "cooperative"


def test_sea_names_its_observation_channel_and_its_latency(client, warm) -> None:
    props = client.get("/live?domain=sea").json()["properties"]
    assert props["observation"] == "sentinel1-sar"
    assert "retrospective" in props["observation_note"]
    assert "not a dark vessel" in props["observation_note"].lower()


def test_air_declares_that_it_has_no_observation_channel(client) -> None:
    """Six sources were tested and none returned MLAT. That is a finding of
    this project, and it travels in the payload so the front end can state it
    rather than leaving a blank that reads as 'not built yet'."""
    r = client.get("/live?domain=air")
    if r.status_code == 200:
        props = r.json()["properties"]
        assert props["observation"] is None
        assert "MLAT" in props["observation_note"]


# -- filtering -------------------------------------------------------------

def test_subtypes_narrow_the_features_but_not_the_counts(client, warm) -> None:
    """A filter is a view. Narrowing it must not make traffic appear to
    vanish from the totals the panel reports."""
    warm.ingest_message(position("1"))
    warm.ingest_message(static("1", 70))
    warm.ingest_message(position("2"))
    warm.ingest_message(static("2", 80))
    warm.ingest_message(position("3"))
    warm.ingest_message(static("3", 30))

    doc = client.get("/live?domain=sea&subtypes=cargo,tanker").json()
    assert doc["properties"]["n"] == 2
    assert doc["properties"]["by_subtype"] == {"cargo": 1, "tanker": 1,
                                               "fishing": 1}


def test_the_available_subtypes_are_advertised(client, warm) -> None:
    """So the front end builds its chips from the server's vocabulary rather
    than a hardcoded list that drifts."""
    props = client.get("/live?domain=sea").json()["properties"]
    assert set(props["subtypes"]) == set(aisstream.SUBTYPES)


# -- credentials -----------------------------------------------------------

def test_a_missing_key_is_503_not_an_empty_collection(client, monkeypatch) -> None:
    """An empty FeatureCollection would render as a calm sea. The fix is a
    credential, and the client has to be told that rather than shown a map."""
    s = aisstream.Stream((-77.2, 36.0, -71.0, 39.6), api_key="")
    s._key = ""
    monkeypatch.setattr(live_route, "sea_stream", lambda: s)
    r = client.get("/live?domain=sea")
    assert r.status_code == 503
    assert "AISSTREAM_API_KEY" in r.json()["detail"]


def test_importing_the_app_opens_no_socket() -> None:
    """The stream is created on first use, never at import. A module-level
    socket would make the test suite itself reach the network, which the
    conftest guard forbids outright."""
    assert live_route._sea is None or live_route._sea._task is None
