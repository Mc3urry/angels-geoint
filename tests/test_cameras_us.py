"""US traffic cameras for the 3D view, parsed offline. No network.

Each publisher's shape is read as God's Eye View reads it; a picture on any
host but the publisher's own is dropped; nothing but a location, a name and
a link survives parsing.
"""

from __future__ import annotations

import base64

import pytest

from angels.core import cameras_us as cu

COL = {c: i for i, c in enumerate(cu.COLS)}


def test_caltrans_keeps_in_service_cameras_with_their_own_frames() -> None:
    doc = {"data": [
        {"cctv": {"inService": "true", "location": {
            "latitude": "37.8", "longitude": "-122.27", "locationName": "TV102 -- I-580 : Grand Ave",
            "nearbyPlace": "Oakland"},
            "imageData": {"static": {"currentImageURL": "https://cwwp2.dot.ca.gov/data/d4/cctv/image/tv102/tv102.jpg"}}}},
        {"cctv": {"inService": "false", "location": {"latitude": "37", "longitude": "-122"}}},
        {"cctv": {"inService": "true", "location": {"latitude": "37", "longitude": "-122"},
                  "imageData": {"static": {"currentImageURL": "https://evil.example/x.jpg"}}}},
    ]}
    rows = cu.parse_caltrans(doc, 4)
    assert len(rows) == 1
    r = rows[0]
    assert r[COL["id"]] == "ca-d4-tv102" and r[COL["name"]] == "I-580 : Grand Ave (Oakland)"
    assert r[COL["kind"]] == cu.IMAGE and r[COL["state"]] == "CA"


def test_wsdot_coordinates_come_out_of_web_mercator() -> None:
    doc = {"features": [{"attributes": {"CameraID": 9495, "CameraTitle": "SR 20 at MP 32.9",
                                        "ImageURL": "https://images.wsdot.wa.gov/nw/020vc03290.jpg"},
                         "geometry": {"x": -13652000.0, "y": 6159000.0}}]}
    r = cu.parse_wsdot(doc)[0]
    assert abs(r[COL["lat"]] - 48.3) < 0.1 and abs(r[COL["lon"]] - (-122.64)) < 0.1


def test_txdot_frames_go_through_our_api_and_decode() -> None:
    doc = {"roadwayCctvStatuses": {"IH 35": [
        {"statusDescription": "Device Online", "hasSnapshot": True, "latitude": 30.27,
         "longitude": -97.74, "icd_Id": "IH35 @ 6th", "name": "IH 35 at 6th St"},
        {"statusDescription": "Device Offline", "latitude": 30.2, "longitude": -97.7, "icd_Id": "X"}]}}
    rows = cu.parse_txdot(doc, "AUS")
    assert len(rows) == 1 and rows[0][COL["media"]].startswith("/cameras/txdot-frame?")
    assert "districtCode=AUS" in cu.txdot_frame_url("IH35 @ 6th", "AUS")
    with pytest.raises(ValueError):
        cu.txdot_frame_url("x", "NOPE")
    jpeg = b"\xff\xd8\xff\xe0fake"
    assert cu.txdot_frame_jpeg({"snippet": base64.b64encode(jpeg).decode()}) == jpeg
    with pytest.raises(ValueError):
        cu.txdot_frame_jpeg({"snippet": base64.b64encode(b"<html>").decode()})


def test_deldot_streams_only_from_deldot() -> None:
    good = {"id": "CAM101", "title": "I-95 @ SR 141", "lat": 39.7, "lon": -75.6, "status": "Active",
            "county": "New Castle", "urls": {"m3u8s": "https://video.deldot.gov/live/CAM101.stream/playlist.m3u8"}}
    bad = {**good, "id": "CAM102", "urls": {"m3u8s": "https://elsewhere.example/live/x/playlist.m3u8"}}
    rows = cu.parse_deldot({"videoCameras": [good, bad]})
    assert [r[COL["id"]] for r in rows] == ["deldot-cam101"] and rows[0][COL["kind"]] == cu.HLS


def test_austin_and_king_county() -> None:
    a = cu.parse_austin([{"camera_id": "12", "location_name": "Lamar / 5th", "camera_status": "TURNED_ON",
                          "location": {"type": "Point", "coordinates": [-97.75, 30.27]}},
                         {"camera_id": "13", "camera_status": "REMOVED",
                          "location": {"coordinates": [-97.7, 30.2]}}])
    assert [r[COL["id"]] for r in a] == ["austin-12"]
    k = cu.parse_king({"features": [
        {"properties": {"AssetID": 7, "Location": "Novelty Hill", "Live": 1, "CurrentStatus": "Active",
                        "ImageURL": "https://info.kingcounty.gov/transportation/kcdot/roads/x.jpg"},
         "geometry": {"coordinates": [-122.0, 47.7]}},
        {"properties": {"AssetID": 8, "Live": 0, "CurrentStatus": "Active",
                        "ImageURL": "https://info.kingcounty.gov/y.jpg"},
         "geometry": {"coordinates": [-122.0, 47.7]}}]})
    assert [r[COL["id"]] for r in k] == ["king-7"]


def test_livetrafficcam_live_cameras_only_and_absolute_frames() -> None:
    doc = {"cams": [
        {"id": 1, "name": "I-80 Donner", "lat": 39.3, "lng": -120.3, "route": "I-80", "state": "NV",
         "live_status": "live", "image": "/img/1.jpg", "official_url": "https://www.nvroads.com/x",
         "attribution": "Camera: NDOT"},
        {"id": 2, "name": "dead", "lat": 39.3, "lng": -120.3, "live_status": "dead", "image": "/img/2.jpg"}]}
    rows = cu.parse_ltc(doc, "NV")
    assert len(rows) == 1
    r = rows[0]
    assert r[COL["media"]] == "https://livetrafficcam.com/img/1.jpg"
    assert r[COL["src"]] == "NDOT" and r[COL["page"]] == "https://www.nvroads.com/x"


def test_the_aggregator_is_not_asked_for_states_read_directly() -> None:
    assert not cu.DIRECT & set(cu.LTC_STATES)
    assert len(cu.LTC_STATES) + len(cu.DIRECT) == 51


def test_the_route_answers_while_loading_and_names_failures(monkeypatch) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from angels.api.routes import cameras as route

    route._state.clear()
    monkeypatch.setattr(route, "sources", lambda: {
        "wsdot": ("WSDOT", lambda: [cu._row("wsdot-1", -122.0, 47.0, "x", "WSDOT", cu.IMAGE,
                                            "https://images.wsdot.wa.gov/1.jpg", None, "WA")]),
        "deldot": ("DelDOT", lambda: (_ for _ in ()).throw(route.urllib.error.URLError("offline"))),
    })
    app = FastAPI()
    app.include_router(route.router)
    doc = TestClient(app).get("/cameras/us", params={"background": "false"}).json()
    assert doc["n"] == 1 and doc["loading"] is False
    assert doc["sources"]["DelDOT"]["failed"] == ["deldot: URLError"]
    assert "never stored" in doc["kept"]


def test_a_511_platform_list_gives_one_row_per_camera_video_first() -> None:
    doc = [{"Id": 1, "Source": "LADOTD", "Roadway": "I-20", "Direction": "Unknown",
            "Latitude": 32.53, "Longitude": -93.63, "Location": "I-20 at I-220",
            "Views": [{"Id": 2205, "Url": "https://lu.stage.traveliq.co/map/Cctv/2205", "Status": "Enabled",
                       "VideoUrl": "https://ITSStreamingBR2.dotd.la.gov/public/shr-cam-030.streams/playlist.m3u8"}]},
           {"Id": 2, "Latitude": 30.4, "Longitude": -91.1, "Location": "still only",
            "Views": [{"Url": "https://511la.org/map/Cctv/9", "Status": "Enabled"}]},
           {"Id": 3, "Latitude": 30.4, "Longitude": -91.1,
            "Views": [{"Url": "https://511la.org/map/Cctv/10", "Status": "Disabled"}]}]
    rows = cu.parse_iteris(doc, "LA", "511LA")
    assert [r[COL["id"]] for r in rows] == ["la511-1", "la511-2"]
    assert rows[0][COL["kind"]] == cu.HLS and rows[1][COL["kind"]] == cu.IMAGE
    assert rows[0][COL["note"]] == "I-20", "an Unknown direction is not a note"


def test_ohgo_results_either_casing() -> None:
    for c in ({"Id": "x1", "Latitude": 39.96, "Longitude": -83.0, "Location": "I-70 at I-71",
               "CameraViews": [{"Direction": "E", "LargeUrl": "https://itscameras.dot.state.oh.us/images/x1.jpg"}]},
              {"id": "x1", "latitude": 39.96, "longitude": -83.0, "location": "I-70 at I-71",
               "cameraViews": [{"largeUrl": "https://itscameras.dot.state.oh.us/images/x1.jpg"}]}):
        rows = cu.parse_ohgo({"Results": [c]})
        assert rows and rows[0][COL["id"]] == "ohgo-x1" and rows[0][COL["state"]] == "OH"


def test_a_key_moves_a_state_off_the_aggregator_and_never_into_a_response(monkeypatch) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from angels.api.routes import cameras as route

    for st in cu.ITERIS:
        monkeypatch.delenv(cu.iteris_env(st), raising=False)
    monkeypatch.delenv(cu.OHGO_ENV, raising=False)
    assert "ltc-NY" in route.sources() and "511-NY" not in route.sources()

    secret = "ny-key-not-real-0123456789"
    monkeypatch.setenv("CAM_KEY_NY", secret)
    table = route.sources()
    assert "511-NY" in table and "ltc-NY" not in table

    asked = []

    def fake_json(url, encoding="utf-8", headers=None):
        asked.append(url)
        if "511ny.org" in url:
            return [{"Id": 7, "Latitude": 40.7, "Longitude": -74.0, "Location": "FDR at 23rd",
                     "Views": [{"Url": "https://511ny.org/map/Cctv/7", "Status": "Enabled"}]}]
        raise route.urllib.error.URLError("offline")

    monkeypatch.setattr(route, "_json", fake_json)
    monkeypatch.setattr(route, "sources", lambda: {"511-NY": table["511-NY"]})
    route._state.clear()
    app = FastAPI()
    app.include_router(route.router)
    r = TestClient(app).get("/cameras/us", params={"background": "false"})
    assert r.json()["n"] == 1 and r.json()["keys"]["NY"] is True
    assert any(secret in u for u in asked), "the key reaches the state"
    assert secret not in r.text, "and never comes back"
