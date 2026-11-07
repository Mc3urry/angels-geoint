"""The CARS hub road events: parsing (ICD examples), link discovery, and the
login staying on the server. No test reaches the network."""

from __future__ import annotations

import gzip
import urllib.error
import urllib.request
from datetime import datetime, timezone
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from angels.api.main import app
from angels.api.routes import roads
from angels.core import cars_hub as ch

NOW = datetime(2026, 11, 7, 18, 0, tzinfo=timezone.utc)
USER, PASSWORD = "test.user", "pw-Zq81-not-real"


def feu(eid="IACARS-1", *, headline="<incident>crash</incident>", status="", link=True,
        secondary=True, end="<end-time><date>20261108</date><time>120000</time><utc-offset>-0600</utc-offset></end-time>",
        start="", comments="<operator-comments>internal only</operator-comments>"):
    loc = ("""<location><location-on-link><link-ownership>IADOT</link-ownership>
        <route-designator>I-80</route-designator>
        <primary-location><geo-location><latitude>41600000</latitude><longitude>-93600000</longitude></geo-location></primary-location>"""
           + ("""<secondary-location><geo-location><latitude>41610000</latitude><longitude>-93500000</longitude></geo-location></secondary-location>"""
              if secondary else "")
           + "<link-direction>positive direction</link-direction></location-on-link></location>"
           if link else "<location><area-location><area-id>19153</area-id></area-location></location>")
    ind = f"<event-indicators><event-indicator><status>{status}</status></event-indicator></event-indicators>" if status else ""
    return f"""<full-event-update xmlns="http://www.northamericanhub.org">
      <message-header><sender><organization-id>IADOT</organization-id><center-id>IACARS</center-id></sender>
        <message-type-version>1</message-type-version><message-number>1</message-number>
        <message-time-stamp><date>20261107</date><time>110000</time><utc-offset>-0600</utc-offset></message-time-stamp></message-header>
      <event-reference><event-id>{eid}</event-id><update>3</update></event-reference>{ind}
      <headline><headline>{headline}</headline></headline>
      <details><detail>
        <descriptions>
          <description><phrase>{headline}</phrase></description>
          <description><qualifier><lane-qualifier>in the left lane</lane-qualifier></qualifier></description>
          <description><additional-text><description>Expect delays.</description></additional-text></description>
        </descriptions>
        <locations>{loc}</locations>
        <times><update-time><date>20261107</date><time>113000</time><utc-offset>-0600</utc-offset></update-time>
          <valid-period>{end}</valid-period>{start}</times>
      </detail></details>{comments}
    </full-event-update>"""


def doc(*events: str) -> bytes:
    return ("<FEUMessages>" + "".join(events) + "</FEUMessages>").encode()


def test_one_event_is_a_line_with_its_public_words():
    feats, counts = ch.parse_feu(doc(feu()), "IA", NOW)
    assert counts == {"messages": 1, "ended_or_expired_or_no_road": 0}
    f = feats[0]
    assert f["geometry"] == {"type": "LineString", "coordinates": [[-93.6, 41.6], [-93.5, 41.61]]}
    p = f["properties"]
    assert (p["id"], p["state"], p["kind"], p["type"], p["headline"]) == ("IACARS-1", "IA", "incident", "incident", "crash")
    assert p["road"] == "I-80" and p["direction"] == "positive direction"
    assert p["text"] == "in the left lane; Expect delays."
    assert p["updated"] == "2026-11-07T17:30:00Z"       # local -0600 to UTC
    assert p["end"] == "2026-11-08T18:00:00Z"
    assert p["planned"] is False


def test_operator_comments_are_never_read():
    feats, _ = ch.parse_feu(doc(feu()), "IA", NOW)
    assert "internal" not in repr(feats)


def test_ended_expired_and_area_only_events_are_left_out_and_counted():
    expired = "<end-time><date>20261107</date><time>080000</time><utc-offset>-0600</utc-offset></end-time>"
    feats, counts = ch.parse_feu(doc(feu("A-1", status="ended"), feu("A-2", end=expired),
                                     feu("A-3", link=False), feu("A-4")), "IA", NOW)
    assert [f["properties"]["id"] for f in feats] == ["A-4"]
    assert counts == {"messages": 4, "ended_or_expired_or_no_road": 3}


def test_duration_counts_from_a_later_start_and_a_future_start_is_planned():
    start = "<start-time><date>20261109</date><time>080000</time><utc-offset>-0600</utc-offset></start-time>"
    feats, _ = ch.parse_feu(doc(feu(end="<duration>90</duration>", start=start, secondary=False)), "IA", NOW)
    p = feats[0]["properties"]
    assert feats[0]["geometry"]["type"] == "Point"
    assert (p["start"], p["end"], p["planned"]) == ("2026-11-09T14:00:00Z", "2026-11-09T15:30:00Z", True)


@pytest.mark.parametrize("hl, kind", [("<roadwork>road construction</roadwork>", "roadwork"),
                                      ("<closure>road closed</closure>", "closure"),
                                      ("<pavement-condition>ice</pavement-condition>", "weather"),
                                      ("<device-status>sign out</device-status>", "other")])
def test_kinds(hl, kind):
    feats, _ = ch.parse_feu(doc(feu(headline=hl)), "IA", NOW)
    assert feats[0]["properties"]["kind"] == kind


def test_gzip_and_duplicates():
    feats, counts = ch.parse_feu(gzip.compress(doc(feu("D-1"), feu("D-1"))), "IA", NOW)
    assert len(feats) == 1 and counts["messages"] == 2


def test_feed_links_stay_on_the_hub_and_on_https():
    html = """<a href="data/FEU-g.xml">FEU-g</a><a href="https://evil.example/feu-g.xml">FEU-g mirror</a>
              <a href="/hub/data/cctv.xml">CCTV</a><a href="javascript:void(0)">feu-g</a>"""
    got = ch.feed_links(html, "https://ia.carsprogram.org/hub/index.jsf")
    assert got == {"feu_g": ["https://ia.carsprogram.org/hub/data/FEU-g.xml"],
                   "cctv": ["https://ia.carsprogram.org/hub/data/cctv.xml"]}
    assert ch.looks_like_sign_in('<input type="password" name="j_password">')


def test_every_hub_is_https():
    assert all(url.startswith("https://") for _, url in ch.HUBS.values())


def test_the_login_is_offered_only_to_its_own_hub_origin():
    op = roads.opener("https://ia.carsprogram.org/hub/index.jsf", USER, PASSWORD)
    mgr = next(h for h in op.handlers if isinstance(h, urllib.request.HTTPBasicAuthHandler)).passwd
    assert mgr.find_user_password(None, "https://ia.carsprogram.org/hub/data/x.xml") == (USER, PASSWORD)
    assert mgr.find_user_password(None, "https://evil.example/") == (None, None)
    # Not asserted: that http://ia.carsprogram.org/ gets nothing. Windows'
    # Python matched it (2026-11-07); the redirect guard below is what keeps
    # the login off plain http.


@pytest.mark.parametrize("to", ["http://ia.carsprogram.org/hub/data/x.xml",
                                "https://evil.example/hub/data/x.xml",
                                "https://mn.carsprogram.org/hub/data/x.xml"])
def test_a_redirect_off_the_hub_https_origin_is_refused(to):
    op = roads.opener("https://ia.carsprogram.org/hub/index.jsf", USER, PASSWORD)
    guard = next(h for h in op.handlers if isinstance(h, roads.StayOnHub))
    req = urllib.request.Request("https://ia.carsprogram.org/hub/index.jsf")
    with pytest.raises(ValueError, match="left the hub"):
        guard.redirect_request(req, None, 302, "Found", {}, to)


def test_a_redirect_within_the_hub_is_followed():
    op = roads.opener("https://ia.carsprogram.org/hub/index.jsf", USER, PASSWORD)
    guard = next(h for h in op.handlers if isinstance(h, roads.StayOnHub))
    req = urllib.request.Request("https://ia.carsprogram.org/hub/index.jsf")
    new = guard.redirect_request(req, None, 302, "Found", {}, "https://ia.carsprogram.org/hub/login.jsf")
    assert new.full_url == "https://ia.carsprogram.org/hub/login.jsf"


def test_plain_http_is_refused_before_any_request():
    with pytest.raises(ValueError, match="not https"):
        roads._get(object(), "http://ia.carsprogram.org/hub/index.jsf", "text/html")


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.setattr(roads, "_state", {})
    monkeypatch.setattr(roads, "_links", {})
    monkeypatch.setattr(roads, "_refreshing", False)
    return TestClient(app)


def test_no_login_says_which_names_to_add(fresh, monkeypatch):
    monkeypatch.delenv(ch.USER_ENV, raising=False)
    monkeypatch.delenv(ch.PASSWORD_ENV, raising=False)
    r = fresh.get("/roads/events")
    assert r.status_code == 503 and ch.USER_ENV in r.json()["detail"]


def test_events_in_a_box_and_errors_never_carry_the_login(fresh, monkeypatch):
    monkeypatch.setenv(ch.USER_ENV, USER)
    monkeypatch.setenv(ch.PASSWORD_ENV, PASSWORD)

    def fake(st):
        if st == "IN":
            feats, counts = ch.parse_feu(doc(feu()), "IN", NOW)
            return feats, counts, {"feu_g": ["x"], "cctv": ["y"]}
        if st == "KS":
            raise urllib.error.HTTPError("https://ks.carsprogram.org/hub/index.jsf", 401, "no", {}, BytesIO(b""))
        if st == "MN":
            raise urllib.error.HTTPError("u", 500, "x", {}, BytesIO(f"bad user {USER} pw {PASSWORD}".encode()))
        raise ValueError("no FEU-g link on the hub page")

    monkeypatch.setattr(roads, "fetch_state", fake)
    monkeypatch.setattr(ch, "parse_feu", ch.parse_feu)
    r = fresh.get("/roads/events?bbox=-94,41,-93,42&background=false")
    assert r.status_code == 200
    body = r.text
    assert USER not in body and PASSWORD not in body
    d = r.json()
    src = d["properties"]["sources"]
    assert src["IN"]["n"] == 1 and src["IN"]["cctv_links"] == 1 and src["IN"]["error"] is None
    assert "IA" not in src and "IA" in d["properties"]["gaps"]
    assert src["KS"]["error"] == "HTTP 401 (the hub refused the login)"
    assert src["MN"]["error"] == "HTTP 500"
    assert src["NE"]["error"] == "no FEU-g link on the hub page"
    assert d["properties"]["n_in_view"] == 1 and d["properties"]["evidence"] == "declared"
    r2 = fresh.get("/roads/events?bbox=-80,30,-79,31&background=false")
    assert r2.json()["properties"]["n_in_view"] == 0


def test_a_refused_redirect_says_where_it_pointed_by_scheme_and_host_only():
    e = roads.RedirectRefused(f"http://ia.carsprogram.org/hub/login.jsf?user={USER}&token=abc")
    msg = roads.error_text(e)
    assert msg == "refused: a redirect left the hub, to http://ia.carsprogram.org"
    assert USER not in msg and "token" not in msg
    assert roads.RedirectRefused("ftp://x/").target == "elsewhere"


def test_kansas_may_follow_its_hub_to_kandrive_and_nowhere_else():
    also = ch.TRUSTED_REDIRECTS["KS"]
    assert also == ("kscars.kandrive.gov",)
    op = roads.opener("https://ks.carsprogram.org/hub/index.jsf", USER, PASSWORD, also=also)
    guard = next(h for h in op.handlers if isinstance(h, roads.StayOnHub))
    req = urllib.request.Request("https://ks.carsprogram.org/hub/index.jsf")
    assert guard.redirect_request(req, None, 302, "Found", {}, "https://kscars.kandrive.gov/hub/index.jsf")
    for bad in ("http://kscars.kandrive.gov/hub/", "https://www.kandrive.gov/", "https://iowadot.gov/"):
        with pytest.raises(ValueError, match="left the hub"):
            guard.redirect_request(req, None, 302, "Found", {}, bad)
    mgr = next(h for h in op.handlers if isinstance(h, urllib.request.HTTPBasicAuthHandler)).passwd
    assert mgr.find_user_password(None, "https://kscars.kandrive.gov/hub/x.xml") == (USER, PASSWORD)
    assert mgr.find_user_password(None, "https://www.kandrive.gov/") == (None, None)


def test_only_kansas_has_a_trusted_redirect():
    assert set(ch.TRUSTED_REDIRECTS) == {"KS"}
    assert "IA" not in ch.HUBS


def test_feed_links_after_a_redirect_read_against_the_final_page():
    got = ch.feed_links('<a href="data/feu-g.xml">FEU-g</a><a href="https://ks.carsprogram.org/x/feu-g.xml">FEU-g</a>',
                        "https://kscars.kandrive.gov/hub/index.jsf",
                        {"ks.carsprogram.org", "kscars.kandrive.gov"})
    assert got["feu_g"] == ["https://kscars.kandrive.gov/hub/data/feu-g.xml", "https://ks.carsprogram.org/x/feu-g.xml"]

