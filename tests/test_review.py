"""A review session must not be able to touch the first reader's verdicts.

The 147 labels were made by one reader. Validating them means a second reader
on a blind subset and a published agreement rate -- which is worth nothing if
the second read can overwrite the first, see it, or be scored before it is
finished. Each of those is a way to get a good-looking number that means
nothing, so each is closed here rather than left to discipline.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from scripts.review_agreement import kappa, last_per_key


@pytest.fixture
def api(tmp_path, monkeypatch):
    """The labels route pointed at a throwaway tree with one open session."""
    import angels.api.routes.labels as L
    from angels.api.main import app

    ev = tmp_path / "events"
    ev.mkdir()
    (ev / "labels.jsonl").write_text("".join(
        json.dumps({"key": f"s/c{i}.png", "verdict": "vessel",
                    "lon": -75.0, "lat": 37.0}) + "\n" for i in range(6)),
        encoding="utf-8")
    (ev / "review-session.json").write_text(json.dumps({
        "who": "second", "n": 3, "seed": 1, "population": 6,
        "out": "labels-review-second.jsonl",
        "keys": ["s/c0.png", "s/c1.png", "s/c2.png"],
    }), encoding="utf-8")
    monkeypatch.setattr(L, "EVENTS", ev)
    monkeypatch.setattr(L, "LABELS", ev / "labels.jsonl")
    monkeypatch.setattr(L, "SESSION", ev / "review-session.json")
    monkeypatch.setattr(L, "_queue", lambda: [
        {"key": f"s/c{i}.png", "lon": -75.0, "lat": 37.0} for i in range(6)])
    return TestClient(app), ev


def test_a_verdict_never_reaches_the_first_readers_file(api):
    """The property everything else rests on."""
    client, ev = api
    before = (ev / "labels.jsonl").read_bytes()
    r = client.post("/labels", json={"key": "s/c0.png", "verdict": "clutter"})
    assert r.status_code == 200, r.text
    assert r.json()["written_to"] == "labels-review-second.jsonl"
    assert (ev / "labels.jsonl").read_bytes() == before, \
        "the review wrote into the first reader's file"


def test_the_queue_serves_only_the_subset(api):
    client, _ = api
    q = client.get("/labels/queue").json()
    assert {i["key"] for i in q["items"]} == {"s/c0.png", "s/c1.png", "s/c2.png"}
    assert q["review"]["who"] == "second"


def test_the_queue_does_not_carry_the_first_verdict(api):
    """A second read that can see the first is not a second read."""
    client, _ = api
    body = client.get("/labels/queue").text
    assert "verdict" not in json.loads(body)["items"][0]


def test_a_chip_outside_the_session_is_refused(api):
    client, _ = api
    r = client.post("/labels", json={"key": "s/c5.png", "verdict": "vessel"})
    assert r.status_code == 400


def test_strata_are_withheld_until_the_session_is_done(api):
    """Progress keyed on the distance band is the hypothesis, mid-read."""
    client, _ = api
    client.post("/labels", json={"key": "s/c0.png", "verdict": "vessel"})
    p = client.get("/labels/progress").json()
    assert p["by_stratum"] is None
    # The key names the withholding; the value says until when. Asserting on
    # the word "withheld" in the value passed for the wrong reason until it
    # did not -- it was matching the key name in my head, not the response.
    assert "by_stratum_withheld" in p
    assert "complete" in p["by_stratum_withheld"]
    assert p["remaining"] == 2


def test_strata_return_once_the_session_is_complete(api):
    """Withheld during, available after: the join was always meant to happen
    after the fact, which is the only place it can happen without the strata
    reaching the person making the judgements."""
    client, _ = api
    for k in ("s/c0.png", "s/c1.png", "s/c2.png"):
        client.post("/labels", json={"key": k, "verdict": "vessel"})
    p = client.get("/labels/progress").json()
    assert p["by_stratum"] is not None
    assert "by_stratum_withheld" not in p


def test_the_queue_skips_what_the_reviewer_read_not_what_the_first_read(api):
    client, _ = api
    client.post("/labels", json={"key": "s/c1.png", "verdict": "clutter"})
    q = client.get("/labels/queue").json()
    assert {i["key"] for i in q["items"]} == {"s/c0.png", "s/c2.png"}


# -- the arithmetic ----------------------------------------------------------

def test_kappa_is_zero_when_agreement_is_chance():
    pairs = [("vessel", "vessel")] * 25 + [("vessel", "clutter")] * 25 \
        + [("clutter", "vessel")] * 25 + [("clutter", "clutter")] * 25
    _po, _pe, k, _se = kappa(pairs)
    assert abs(k) < 1e-9


def test_kappa_is_one_on_perfect_agreement():
    pairs = [("vessel", "vessel")] * 30 + [("clutter", "clutter")] * 10
    _po, _pe, k, _se = kappa(pairs)
    assert k == pytest.approx(1.0)


def test_kappa_is_undefined_when_one_category_carries_everything():
    """Not 0.0, and not a crash: there is no chance baseline to subtract."""
    _po, pe, k, _se = kappa([("vessel", "vessel")] * 40)
    assert pe == pytest.approx(1.0)
    assert k != k


def test_raw_agreement_can_flatter_a_poor_kappa():
    """The reason kappa is reported at all."""
    pairs = [("vessel", "vessel")] * 36 + [("clutter", "vessel")] * 4
    po, _pe, k, _se = kappa(pairs)
    assert po == pytest.approx(0.90)
    assert k < 0.15, "90% agreement on a lopsided marginal is near chance"


def test_last_write_per_key_wins(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text(json.dumps({"key": "a", "verdict": "vessel"}) + "\n"
                 + json.dumps({"key": "a", "verdict": "clutter"}) + "\n",
                 encoding="utf-8")
    assert last_per_key(p)["a"]["verdict"] == "clutter"
