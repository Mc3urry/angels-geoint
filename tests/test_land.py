"""The Land tab's reads, against archives built in a temporary directory.

What is defended:

  * a vehicle on the map is its NEWEST stored fix, aged from its own clock
    when it has one, and the payload says what "recent" means;
  * a selected feed with no ledger row is shown as such, the third footprint,
    and not silently left off the status list;
  * work zones are read from the body each sidecar names, so the map shows
    what the collector last stored and nothing it did not;
  * every payload says it is cooperative.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone

import pytest

from angels.core import land

NOW = datetime(2026, 10, 27, 20, 0, tzinfo=timezone.utc).timestamp()
INDEX = {"mdb-1850": {"provider": "WMATA", "state_names": ["DC", "MD", "VA"]},
         "mdb-1790": {"provider": "Arlington Transit", "state_names": ["VA"]}}


def write_positions(root, rows, hour="2026102719"):
    pq = pytest.importorskip("pyarrow.parquet")
    import pyarrow as pa
    d = root / f"hour={hour}"
    d.mkdir(parents=True, exist_ok=True)
    full = [{"feed_id": "mdb-1850", "vehicle_key": "v1", "label": "", "route_id": "",
             "lat": 38.9, "lon": -77.0, "bearing": None, "speed": None,
             "vehicle_ts": None, "fetched_at": NOW - 60, **r} for r in rows]
    pq.write_table(pa.Table.from_pylist(full), d / f"positions_{hour}0000.parquet")


def test_the_newest_fix_per_vehicle_is_shown(tmp_path) -> None:
    write_positions(tmp_path, [
        {"vehicle_key": "v1", "vehicle_ts": int(NOW) - 300, "lon": -77.10},
        {"vehicle_key": "v1", "vehicle_ts": int(NOW) - 30, "lon": -77.05},
        {"vehicle_key": "v2", "vehicle_ts": None, "fetched_at": NOW - 90},
    ])
    fc = land.recent_vehicles(tmp_path, now_s=NOW, window_s=900, index=INDEX)
    by = {f["properties"]["age_from"]: f for f in fc["features"]}
    assert fc["properties"]["n"] == 2
    assert by["vehicle"]["geometry"]["coordinates"][0] == -77.05
    assert by["vehicle"]["properties"]["age_s"] == 30
    assert by["poll"]["properties"]["age_s"] == 90, "aged from our poll, and says so"
    assert fc["properties"]["evidence"] == "cooperative"
    assert "CHANGED" in fc["properties"]["means"]


def test_fixes_outside_the_window_are_left_out(tmp_path) -> None:
    write_positions(tmp_path, [{"fetched_at": NOW - 3600}])
    fc = land.recent_vehicles(tmp_path, now_s=NOW, window_s=900, index=INDEX)
    assert fc["features"] == [] and fc["properties"]["archive_lag_s"] is None


def test_the_archive_lag_is_reported(tmp_path) -> None:
    write_positions(tmp_path, [{"fetched_at": NOW - 240}])
    fc = land.recent_vehicles(tmp_path, now_s=NOW, window_s=900, index=INDEX)
    assert fc["properties"]["archive_lag_s"] == 240


def test_a_feed_with_no_ledger_row_is_listed_as_such(tmp_path) -> None:
    (tmp_path / "attempts-2026-10-27.jsonl").write_text(
        json.dumps({"t": NOW - 20, "feed": "mdb-1850", "outcome": "ok",
                    "n_vehicles": 812}) + "\n")
    rows = {r["feed"]: r for r in land.vehicle_status(tmp_path, now_s=NOW,
                                                     index=INDEX)}
    assert rows["mdb-1850"]["outcome"] == "ok" and rows["mdb-1850"]["age_s"] == 20
    assert rows["mdb-1790"]["outcome"] == "no ledger row"


def test_the_ledger_tail_survives_a_cut_first_line(tmp_path) -> None:
    lines = [json.dumps({"t": NOW, "feed": f"f{i}", "outcome": "ok"})
             for i in range(200)]
    p = tmp_path / "attempts-2026-10-27.jsonl"
    p.write_text("\n".join(lines) + "\n")
    rows = land.ledger_tail(tmp_path, tail_bytes=1000)
    assert rows and rows[-1]["feed"] == "f199"


def wzdx_archive(root, feed, features):
    name = f"{feed}_20261027T190000_abc.json.gz"
    d = root / "hour=2026102719"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(gzip.compress(json.dumps(
        {"type": "FeatureCollection", "features": features}).encode()))
    (root / "_last").mkdir(exist_ok=True)
    (root / "_last" / f"{feed}.json").write_text(json.dumps({"path": name}))


def zone(lon, lat, road="I-95"):
    return {"type": "Feature",
            "geometry": {"type": "LineString",
                         "coordinates": [[lon, lat], [lon + 0.01, lat]]},
            "properties": {"core_details": {"road_names": [road],
                                            "direction": "northbound"},
                           "vehicle_impact": "some-lanes-closed",
                           "start_date": "2026-10-27T00:00:00Z"}}


def test_work_zones_come_from_the_body_the_sidecar_names(tmp_path) -> None:
    land._ZONE_CACHE.clear()
    wzdx_archive(tmp_path, "mdot", [zone(-76.6, 39.3), zone(-90.0, 45.0)])
    fc = land.workzones(tmp_path, bbox=(-77.7, 38.3, -76.0, 39.6))
    assert fc["properties"]["n_in_view"] == 1
    p = fc["features"][0]["properties"]
    assert p == {"feed": "mdot", "road": "I-95", "direction": "northbound",
                 "impact": "some-lanes-closed",
                 "start": "2026-10-27T00:00:00Z", "end": ""}


def test_work_zones_over_the_limit_say_they_were_cut(tmp_path) -> None:
    land._ZONE_CACHE.clear()
    wzdx_archive(tmp_path, "big", [zone(-76.6 + i * 0.001, 39.3) for i in range(5)])
    fc = land.workzones(tmp_path, bbox=(-77.7, 38.3, -76.0, 39.6), limit=2)
    assert fc["properties"]["truncated"] is True
    assert (fc["properties"]["n_shown"], fc["properties"]["n_in_view"]) == (2, 5)


@pytest.mark.parametrize("text", ["1,2,3", "a,b,c,d", "5,0,1,1", "0,5,1,1",
                                  "-200,0,0,1"])
def test_a_bad_bbox_is_refused(text) -> None:
    with pytest.raises(ValueError):
        land.parse_bbox(text)


def test_states_are_read_from_the_g2b_set_and_filtered(tmp_path) -> None:
    sq = lambda x0: {"type": "Polygon", "coordinates": [[[x0, 38], [x0 + 1, 38],
                     [x0 + 1, 39], [x0, 39], [x0, 38]]]}
    for fips, x0 in (("24", -77.0), ("06", -120.0)):
        (tmp_path / f"{fips}.geojson").write_text(json.dumps(
            {"features": [{"geometry": sq(x0), "properties": {}}]}))
    (tmp_path / "manifest.json").write_text(json.dumps({"states": {
        "24": {"name": "Maryland", "file": "24.geojson"},
        "06": {"name": "California", "file": "06.geojson"}}}))
    fc = land.states_in(tmp_path, bbox=(-77.7, 38.3, -76.0, 39.6))
    assert [f["properties"]["state"] for f in fc["features"]] == ["24"]


def test_the_routes_answer_and_say_cooperative(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from angels.api.routes import land as route

    monkeypatch.setattr(land, "VEHICLES", tmp_path / "v")
    monkeypatch.setattr(land, "WZDX", tmp_path / "w")
    app = FastAPI()
    app.include_router(route.router)
    c = TestClient(app)
    assert c.get("/land/vehicles").status_code == 503, "nothing written yet"
    assert c.get("/land/workzones", params={"bbox": "1,2,3"}).status_code == 422
    s = c.get("/land/status").json()
    assert s["evidence"] == "cooperative" and "no cause" in s["note"]
