"""Tests for the observed layer's routes.

The live routes serve cooperative reporting; these serve the other half.
Drawn on one map without labels the two become a single picture of "what is
out there", which is the confusion this project exists to refuse -- so the
tests are mostly about what the payloads are obliged to say, and about the
difference between "not searched" and "searched and empty".

Nothing here reaches the network or the real data directory.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from angels.adapters.maritime.searched import LEVELS, SearchedArea
from angels.api.main import app
from angels.api.routes import coverage as cov
from angels.api.routes import events as ev


def scene_doc(acquired, cells, *, scene="S1A_TEST", n_features=1):
    area = SearchedArea(0.01, -75.0, 37.0, cells)
    return {
        "type": "FeatureCollection",
        "properties": {"scene": scene, "acquired": acquired,
                       "searched_km2": 1234.5, "mask_source": "own histogram",
                       "searched_grid": area.to_json()},
        "features": [{"type": "Feature",
                      "geometry": {"type": "Point", "coordinates": [-75.0, 37.0]},
                      "properties": {}} for _ in range(n_features)],
    }


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(cov, "EVENTS", tmp_path)
    monkeypatch.setattr(ev, "EVENTS", tmp_path)
    cov.load_passes(force=True)
    yield TestClient(app), tmp_path
    monkeypatch.setattr(cov, "_passes", None)


# -- coverage ---------------------------------------------------------------

def test_a_searched_point_reports_when_it_was_last_observed(api) -> None:
    client, tmp = api
    (tmp / "sar-A.geojson").write_text(json.dumps(
        scene_doc("2024-12-30T22:59:07+00:00", {(0, 0): LEVELS})))
    cov.load_passes(force=True)

    r = client.get("/coverage?lon=-74.995&lat=37.005")
    assert r.status_code == 200
    body = r.json()
    assert body["last"]["status"] == "searched"
    assert body["last"]["acquired"].startswith("2024-12-30")
    assert body["evidence"] == "observed"


def test_water_no_pass_searched_says_so_rather_than_nothing(api) -> None:
    """THE SENTENCE THAT MATTERS. 'Not searched' must never render as
    'searched and empty'."""
    client, tmp = api
    (tmp / "sar-A.geojson").write_text(json.dumps(
        scene_doc("2024-12-30T22:59:07+00:00", {(0, 0): LEVELS})))
    cov.load_passes(force=True)

    body = client.get("/coverage?lon=-71.5&lat=36.2").json()
    assert body["last"] is None
    assert "never been observed" in body["note"]


def test_a_part_searched_shoreline_cell_is_labelled_as_such(api) -> None:
    client, tmp = api
    (tmp / "sar-A.geojson").write_text(json.dumps(
        scene_doc("2024-12-30T22:59:07+00:00", {(0, 0): 2})))
    cov.load_passes(force=True)
    body = client.get("/coverage?lon=-74.995&lat=37.005").json()
    assert body["last"]["status"] == "part-searched"


def test_the_newest_pass_comes_first(api) -> None:
    client, tmp = api
    (tmp / "sar-A.geojson").write_text(json.dumps(
        scene_doc("2024-06-21T22:59:07+00:00", {(0, 0): LEVELS}, scene="OLD")))
    (tmp / "sar-B.geojson").write_text(json.dumps(
        scene_doc("2024-12-30T22:59:07+00:00", {(0, 0): LEVELS}, scene="NEW")))
    cov.load_passes(force=True)
    body = client.get("/coverage?lon=-74.995&lat=37.005").json()
    assert body["last"]["scene"] == "NEW"
    assert len(body["history"]) == 2


def test_a_scene_without_a_grid_is_skipped_not_counted_as_empty(api) -> None:
    client, tmp = api
    doc = scene_doc("2024-12-30T22:59:07+00:00", {(0, 0): LEVELS})
    doc["properties"].pop("searched_grid")
    (tmp / "sar-A.geojson").write_text(json.dumps(doc))
    cov.load_passes(force=True)
    assert client.get("/coverage/summary").json()["n_passes"] == 0


def test_the_summary_states_the_revisit(api) -> None:
    client, tmp = api
    (tmp / "sar-A.geojson").write_text(json.dumps(
        scene_doc("2024-12-30T22:59:07+00:00", {(0, 0): LEVELS})))
    cov.load_passes(force=True)
    body = client.get("/coverage/summary").json()
    assert body["revisit_days"] == 12
    assert body["n_passes"] == 1


# -- events -----------------------------------------------------------------

def candidates_doc(feats):
    return {"type": "FeatureCollection",
            "properties": {"gate_min_snr": 15, "gate_min_pixels": 6},
            "features": feats}


def cand(lon, lat, date="2024-12-30", reception="heard"):
    return {"type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"date": date, "reception": reception, "snr": 40}}


def test_candidates_carry_the_caveat_and_the_evidence_class(api) -> None:
    client, tmp = api
    (tmp / "candidates-scored.geojson").write_text(json.dumps(
        candidates_doc([cand(-75.0, 37.0)])))
    body = client.get("/events/candidates").json()
    assert body["properties"]["evidence"] == "observed"
    assert body["properties"]["retrospective"] is True
    assert "not confirmed" in body["properties"]["caveat"]


def test_only_heard_water_by_default(api) -> None:
    client, tmp = api
    (tmp / "candidates-scored.geojson").write_text(json.dumps(candidates_doc([
        cand(-75.0, 37.0), cand(-74.0, 37.0, reception="unheard")])))
    assert client.get("/events/candidates").json()["properties"]["n"] == 1
    assert client.get("/events/candidates?reception=all"
                      ).json()["properties"]["n"] == 2


def test_bbox_and_date_filter(api) -> None:
    client, tmp = api
    (tmp / "candidates-scored.geojson").write_text(json.dumps(candidates_doc([
        cand(-75.0, 37.0, date="2024-12-30"),
        cand(-72.0, 38.0, date="2024-06-21")])))
    assert client.get("/events/candidates?bbox=-76,36,-74,38"
                      ).json()["properties"]["n"] == 1
    assert client.get("/events/candidates?date=2024-06-21"
                      ).json()["properties"]["n"] == 1


def test_a_missing_candidate_file_is_404_with_the_reason(api) -> None:
    """An empty layer would read as 'no dark vessels'. It is a product that
    has not been built."""
    client, _ = api
    r = client.get("/events/candidates")
    assert r.status_code == 404
    assert "missing product" in r.json()["detail"]


def test_sites_default_to_the_fixed_ones_and_say_what_they_are(api) -> None:
    client, tmp = api
    (tmp / "persistent-sites.geojson").write_text(json.dumps({
        "type": "FeatureCollection", "properties": {"n_fixed": 1},
        "features": [
            {"type": "Feature",
             "geometry": {"type": "Point", "coordinates": [-75.4, 36.9]},
             "properties": {"fixed": True}},
            {"type": "Feature",
             "geometry": {"type": "Point", "coordinates": [-75.5, 36.9]},
             "properties": {"fixed": False}}]}))
    body = client.get("/events/sites").json()
    assert body["properties"]["n"] == 1
    assert "wind turbines" in body["properties"]["what"]
    assert client.get("/events/sites?fixed_only=false"
                      ).json()["properties"]["n"] == 2


# -- the sampling rate ------------------------------------------------------
#
# A candidate count served without its detection rate is the misreading this
# whole project is about: a sample of unexplained returns, read as a census of
# dark vessels. These tests are about that number never travelling alone.

def dark_doc(cal):
    return {"type": "FeatureCollection",
            "properties": {"scene": "S1A_TEST", "calibration": cal},
            "features": []}


def cal_doc(strata, curve=None):
    def r(found, scored):
        return {"found": found, "scored": scored,
                "rate": None if not scored else found / scored,
                "ci95": [None, None]}
    return {
        "detectable_length_m": 25.0,
        "max_chance_match": 0.05,
        "expected_chance_matches": 1.1,
        "n_unscorable": 2,
        "curve_edges_m": [0.0, 15.0, 25.0, 50.0, 100.0],
        "strata": {k: r(*v) for k, v in strata.items()},
        "curve": {k: r(*v) for k, v in (curve or {}).items()},
    }


def test_the_rate_pools_by_adding_counts_across_passes(api) -> None:
    client, tmp = api
    (tmp / "dark-A.geojson").write_text(json.dumps(dark_doc(
        cal_doc({">=25 m": (3, 5)}, {"25-50 m": (3, 5)}))))
    (tmp / "dark-B.geojson").write_text(json.dumps(dark_doc(
        cal_doc({">=25 m": (4, 6)}, {"25-50 m": (4, 6)}))))

    dr = ev.pooled_calibration()
    assert dr["n_passes"] == 2
    assert (dr["headline"]["found"], dr["headline"]["scored"]) == (7, 11)
    assert dr["curve"]["25-50 m"]["scored"] == 11
    # Rates are recomputed from the pooled counts, never averaged: 7/11 is
    # not the mean of 3/5 and 4/6.
    assert dr["headline"]["rate"] == pytest.approx(7 / 11, abs=1e-4)
    assert dr["expected_chance_matches"] == pytest.approx(2.2, abs=1e-6)
    assert dr["n_unscorable"] == 4


def test_the_interval_is_wilson_not_a_bare_ratio(api) -> None:
    """At these counts the normal approximation runs off the end of the
    scale, and the front end draws whatever it is given."""
    client, tmp = api
    (tmp / "dark-A.geojson").write_text(json.dumps(dark_doc(
        cal_doc({">=25 m": (3, 3)}))))
    lo, hi = ev.pooled_calibration()["headline"]["ci95"]
    assert 0 < lo < 1 and hi == 1.0


def test_the_candidate_payload_carries_its_own_sampling_rate(api) -> None:
    client, tmp = api
    (tmp / "candidates-scored.geojson").write_text(json.dumps(
        candidates_doc([cand(-75.0, 37.0)])))
    (tmp / "dark-A.geojson").write_text(json.dumps(dark_doc(
        cal_doc({">=25 m": (8, 10)}, {"0-15 m": (1, 25)}))))

    props = client.get("/events/candidates").json()["properties"]
    dr = props["detection_rate"]
    assert dr["headline"]["rate"] == pytest.approx(0.8)
    # The class where absence means nothing must be in the payload, not
    # summarised away into the headline.
    assert dr["curve"]["0-15 m"]["rate"] == pytest.approx(0.04)
    assert "not evidence of absence" in dr["what"]


def test_no_calibration_on_disk_is_no_rate_not_a_perfect_one(api) -> None:
    """A missing measurement must reach the front end as null, so the panel
    prints nothing rather than an implied 100%."""
    client, tmp = api
    (tmp / "candidates-scored.geojson").write_text(json.dumps(
        candidates_doc([cand(-75.0, 37.0)])))
    assert ev.pooled_calibration() is None
    assert client.get("/events/candidates").json()[
        "properties"]["detection_rate"] is None
    assert client.get("/events/summary").json()["detection_rate"] is None


def test_the_summary_spans_the_passes_it_has(api) -> None:
    """The panel prints 'x to y days old' from these two, so a missing
    oldest would silently make the layer look current."""
    client, tmp = api
    (tmp / "sar-A.geojson").write_text(json.dumps(
        scene_doc("2024-12-30T22:59:07+00:00", {(0, 0): LEVELS})))
    (tmp / "sar-B.geojson").write_text(json.dumps(
        scene_doc("2024-06-21T22:59:07+00:00", {(0, 0): LEVELS})))
    cov.load_passes(force=True)

    body = client.get("/coverage/summary").json()
    assert body["n_passes"] == 2
    assert body["oldest_age_days"] > body["newest_age_days"]
    assert body["newest"].startswith("2024-12-30")
