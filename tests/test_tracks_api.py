"""Tests for the /tracks endpoint.

Builds a throwaway Parquet archive in a temp directory, points an adapter at
it, and exercises the route end to end. Nothing touches the real data/raw and
nothing needs credentials or a network.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from angels.adapters.aviation.opensky import (
    AviationAdapter, archive_row_to_report, row_to_report,
)
from angels.api import routes
from angels.api.main import app
from angels.api.routes.tracks import decimate
from angels.config import AOI_AIR
from angels.core.models import Position, Report, Track
from scripts.ingest_aviation import SCHEMA

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _project(lat, lon, brg, dist_m):
    d = dist_m / 6371008.8
    b, p1, l1 = math.radians(brg), math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """A small Parquet archive of three aircraft inside AOI_AIR."""
    cols = {f.name: [] for f in SCHEMA}
    planes = [("a1b2c3", "SWA100 ", 38.7, -77.3, 60.0, 3000.0),
              ("d4e5f6", "AAL200 ", 39.2, -76.5, 240.0, 6000.0),
              ("789abc", "UAL300 ", 38.9, -77.0, 90.0, 9000.0)]

    for icao, cs, lat0, lon0, brg, alt in planes:
        lat, lon = lat0, lon0
        for i in range(40):
            t = NOW - timedelta(minutes=20) + timedelta(seconds=30 * i)
            cols["fetched_at"].append(t)
            cols["icao24"].append(icao)
            cols["callsign"].append(cs)
            cols["origin_country"].append("United States")
            cols["time_position"].append(int(t.timestamp()))
            cols["last_contact"].append(int(t.timestamp()))
            cols["longitude"].append(lon)
            cols["latitude"].append(lat)
            cols["baro_altitude"].append(alt)
            cols["on_ground"].append(False)
            cols["velocity"].append(120.0)
            cols["true_track"].append(brg)
            cols["vertical_rate"].append(0.0)
            cols["geo_altitude"].append(alt + 100)
            cols["squawk"].append("1200")
            cols["spi"].append(False)
            cols["position_source"].append(0)
            lat, lon = _project(lat, lon, brg, 120.0 * 30)

    part = tmp_path / "aviation" / f"hour={NOW:%Y%m%d%H}"
    part.mkdir(parents=True)
    pq.write_table(pa.Table.from_pydict(cols, schema=SCHEMA), part / "s.parquet")

    monkeypatch.setitem(routes.tracks._ADAPTERS, "air",
                        AviationAdapter(archive_root=tmp_path))
    return tmp_path


@pytest.fixture
def client():
    return TestClient(app)


def window():
    return {"start": (NOW - timedelta(hours=2)).isoformat(), "end": NOW.isoformat()}


# -- health ----------------------------------------------------------------

def test_health_reports_all_three_boxes(client) -> None:
    """The front end draws its initial view from hardcoded bounds. This echo
    is how you catch the two drifting apart."""
    h = client.get("/health").json()
    assert h["status"] == "ok"
    assert h["region"] == "chesapeake-potomac"
    assert set(h["bbox"]) == {"region", "air", "sea"}
    assert h["bbox"]["air"] == list(AOI_AIR)


# -- shape -----------------------------------------------------------------

def test_returns_valid_geojson(client, archive) -> None:
    g = client.get("/tracks", params=window()).json()
    assert g["type"] == "FeatureCollection"
    assert len(g["features"]) == 3
    for f in g["features"]:
        assert f["type"] == "Feature"
        assert f["geometry"]["type"] == "LineString"
        assert len(f["geometry"]["coordinates"]) >= 2


def test_coordinates_are_lon_lat_not_lat_lon(client, archive) -> None:
    """GeoJSON is lon-first. Get it backwards and everything renders in the
    Indian Ocean, which is obvious on a map and invisible in a test that only
    checks the shape."""
    g = client.get("/tracks", params=window()).json()
    for f in g["features"]:
        for lon, lat in f["geometry"]["coordinates"]:
            assert AOI_AIR[0] <= lon <= AOI_AIR[2], f"lon {lon} outside box"
            assert AOI_AIR[1] <= lat <= AOI_AIR[3], f"lat {lat} outside box"


def test_properties_carry_what_the_map_needs(client, archive) -> None:
    p = client.get("/tracks", params=window()).json()["features"][0]["properties"]
    for key in ("platform_id", "domain", "t_start", "t_end", "duration_s",
                "n_reports", "n_vertices", "path_km", "net_km", "max_alt_m"):
        assert key in p, f"missing {key}"
    assert p["domain"] == "air"
    assert p["path_km"] > 0


def test_straight_flights_have_straightness_near_one(client, archive) -> None:
    """net/path is the quantity orbits.py will key on in Phase 2. Synthetic
    tracks fly straight, so it must come out near 1.0 -- if it does not, the
    geometry is wrong somewhere."""
    for f in client.get("/tracks", params=window()).json()["features"]:
        p = f["properties"]
        assert p["net_km"] / p["path_km"] == pytest.approx(1.0, abs=0.02)


# -- filtering and limits --------------------------------------------------

def test_empty_window_is_empty_not_an_error(client, archive) -> None:
    """No data is a normal state -- the poller may not have run yet."""
    r = client.get("/tracks", params={"start": "2020-01-01T00:00:00Z",
                                      "end": "2020-01-02T00:00:00Z"})
    assert r.status_code == 200
    assert r.json()["features"] == []


def test_reversed_range_is_rejected(client, archive) -> None:
    r = client.get("/tracks", params={"start": NOW.isoformat(),
                                      "end": (NOW - timedelta(hours=1)).isoformat()})
    assert r.status_code == 400


def test_limit_truncates_and_says_so(client, archive) -> None:
    g = client.get("/tracks", params={**window(), "limit": 1}).json()
    assert len(g["features"]) == 1
    assert g["properties"]["truncated"] is True
    assert g["properties"]["n_tracks_total"] == 3


def test_min_points_drops_short_tracks(client, archive) -> None:
    g = client.get("/tracks", params={**window(), "min_points": 10_000}).json()
    assert g["features"] == []


def test_unknown_domain_is_not_implemented(client, archive) -> None:
    assert client.get("/tracks", params={**window(), "domain": "sea"}).status_code == 501


# -- decimation ------------------------------------------------------------

def _track(n: int) -> Track:
    reports = [Report("x", Position(39.0 + i * 0.001, -77.0, NOW + timedelta(seconds=i), 10.0), "adsb")
               for i in range(n)]
    return Track("x", "air", reports)


def test_short_tracks_are_not_decimated() -> None:
    t = _track(50)
    assert len(decimate(t, limit=250)) == 50


def test_long_tracks_are_thinned_to_the_limit() -> None:
    out = decimate(_track(5000), limit=250)
    assert 100 < len(out) <= 251


def test_decimation_keeps_both_endpoints() -> None:
    """Losing where a track started or stopped would be losing the
    interesting part."""
    t = _track(5000)
    out = decimate(t, limit=250)
    assert out[0] is t.reports[0]
    assert out[-1] is t.reports[-1]


def test_decimation_preserves_time_order() -> None:
    out = decimate(_track(5000), limit=250)
    times = [r.position.t for r in out]
    assert times == sorted(times)


# -- the two parsers must not drift ---------------------------------------

def test_live_and_archive_parsers_agree() -> None:
    """row_to_report reads the API's bare array; archive_row_to_report reads
    named Parquet columns. If these ever disagree, live data and stored data
    mean different things and nothing downstream would tell you.
    """
    t = int(NOW.timestamp())
    arr = ["abc123", "TEST123 ", "United States", t, t, -76.61, 39.29,
           9000.0, False, 180.0, 270.0, 0.0, None, 9100.0, "1200", False, 0, 0]
    dct = {
        "icao24": "abc123", "callsign": "TEST123 ", "time_position": t,
        "last_contact": t, "longitude": -76.61, "latitude": 39.29,
        "baro_altitude": 9000.0, "on_ground": False, "velocity": 180.0,
        "true_track": 270.0, "geo_altitude": 9100.0,
    }
    a, b = row_to_report(arr), archive_row_to_report(dct)
    assert a == b


def test_both_parsers_fall_back_to_baro_altitude() -> None:
    t = int(NOW.timestamp())
    arr = ["abc123", "T", "US", t, t, -76.6, 39.3, 8000.0, False,
           180.0, 270.0, 0.0, None, None, "1200", False, 0, 0]
    dct = {"icao24": "abc123", "time_position": t, "last_contact": t,
           "longitude": -76.6, "latitude": 39.3, "baro_altitude": 8000.0,
           "geo_altitude": None, "velocity": 180.0, "true_track": 270.0}
    assert row_to_report(arr).position.alt_m == 8000.0
    assert archive_row_to_report(dct).position.alt_m == 8000.0
