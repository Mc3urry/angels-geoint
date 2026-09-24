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


async def _ready(stream):
    """sea_stream is a coroutine now -- it may have to widen the
    subscription and stop the old socket first. The stubs stay plain objects;
    only the awaiting changes."""
    return stream


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
    monkeypatch.setattr(live_route, "sea_stream",
                        lambda box=None: _ready(s))

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
    monkeypatch.setattr(live_route, "sea_stream",
                        lambda box=None: _ready(s))

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
    monkeypatch.setattr(live_route, "sea_stream",
                        lambda box=None: _ready(s))
    r = client.get("/live?domain=sea")
    assert r.status_code == 503
    assert "AISSTREAM_API_KEY" in r.json()["detail"]


def test_importing_the_app_opens_no_socket() -> None:
    """The stream is created on first use, never at import. A module-level
    socket would make the test suite itself reach the network, which the
    conftest guard forbids outright."""
    assert live_route._sea is None or live_route._sea._task is None


def test_the_front_end_is_served_with_no_cache() -> None:
    """A stale ES module reports itself as a SyntaxError about a missing
    export, which reads as a code bug and is a cache bug. `no-cache` keeps
    the file but forces a revalidation, so app.js and views/live.js can never
    be different vintages of each other."""
    from fastapi.testclient import TestClient

    from angels.api.main import app as the_app

    client = TestClient(the_app)
    for path in ("/index.html", "/app.js", "/views/live.js"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers.get("cache-control") == "no-cache", path
        # The ETag is what makes revalidation cheap; without it the header
        # above would turn every reload into a full re-download.
        assert r.headers.get("etag"), path


# -- the sea's national box -------------------------------------------------
#
# aisstream has no quota, so asking for the country costs nothing in money.
# It costs the TABLE: changing the subscription throws away everything heard
# for the old box, and an anchored vessel reports only every three minutes.
# These tests are about paying that once rather than on every click.

def test_the_sea_subscription_widens_but_never_narrows(monkeypatch) -> None:
    from angels.api.routes.live import SEA_BOXES, _contains

    made, stopped = [], []

    class FakeStream:
        def __init__(self, bbox):
            self.bbox = bbox
            made.append(bbox)

        async def stop(self):
            stopped.append(self.bbox)

    monkeypatch.setattr(live_route.aisstream, "Stream", FakeStream)
    monkeypatch.setattr(live_route, "_sea", None)

    import asyncio

    async def run():
        a = await live_route.sea_stream(SEA_BOXES["air"])
        b = await live_route.sea_stream(SEA_BOXES["conus"])   # widens
        c = await live_route.sea_stream(SEA_BOXES["air"])     # must NOT narrow
        return a, b, c

    a, b, c = asyncio.run(run())
    assert a.bbox == SEA_BOXES["air"]
    assert b.bbox == SEA_BOXES["conus"]
    assert c is b, "asking for the small box again must reuse the wide socket"
    assert stopped == [SEA_BOXES["air"]], "exactly one resubscription"
    assert _contains(SEA_BOXES["conus"], SEA_BOXES["air"])


def test_the_small_box_is_a_filter_over_the_wide_table(monkeypatch) -> None:
    """Once the socket is national, the Chesapeake view must be a filter --
    not a resubscription, and not the whole country mislabelled."""
    from angels.config import AOI_SEA, AOI_SEA_CONUS

    def at(lon, lat, mmsi):
        return {"type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {"mmsi": mmsi, "subtype": "cargo"}}

    inside = at(-76.0, 37.5, "111")            # Chesapeake mouth
    outside = at(-122.4, 37.8, "222")          # San Francisco

    class Wide:
        bbox = AOI_SEA_CONUS

        async def start(self):
            return None

        def snapshot(self, wanted=None):
            return aisstream.Snapshot(
                features=[inside, outside], listening_s=600.0, connected=True,
                warming=False, n_messages=10, last_message_age_s=1.0,
                error=None, bbox=AOI_SEA_CONUS, by_subtype={"cargo": 2},
                discovery_per_min=0.0, peak_discovery_per_min=100.0,
                n_typed=2, n_heard_static=2, static_parts={})

    monkeypatch.setattr(live_route, "sea_stream", lambda box=None: _ready(Wide()))
    client = TestClient(app)

    nat = client.get("/live?domain=sea&aoi=conus").json()
    assert nat["properties"]["n"] == 2
    assert nat["properties"]["bbox"] == list(AOI_SEA_CONUS)

    loc = client.get("/live?domain=sea").json()
    assert loc["properties"]["n"] == 1
    assert loc["properties"]["bbox"] == list(AOI_SEA)
    # The count in the subscription travels too, so the panel can say that
    # the rest of the country is heard but not drawn.
    assert loc["properties"]["n_in_subscription"] == 2
    assert loc["properties"]["subscribed_bbox"] == list(AOI_SEA_CONUS)
    assert loc["features"][0]["geometry"]["coordinates"][0] == -76.0
