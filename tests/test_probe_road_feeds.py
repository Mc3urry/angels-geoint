"""The road-feed probe, tested against what the servers actually sent.

Nothing here touches the network. Every body in `tests/fixtures/road/` is a
verbatim recording written by `scripts/probe_road_feeds.py --save-bodies`,
and the manifest beside them carries each one's URL, status, content type,
fetch time, byte counts and SHA-256.

WHY RECORDINGS AND NOT FIXTURES WRITTEN BY HAND

Sighting 28: the probe's own error-object guard labelled two valid ArcGIS
responses as errors, because an ArcGIS `FeatureCollection` carries
`exceededTransferLimit` beside `type` and `features` and the guard ran before
the body's shape was known. Section 4 of `docs/reporting-defects.md` records
that nothing automated caught it and nothing could have, because "the
fixtures were written from what the author expected servers to send, so the
test suite agreed with the bug."

Nobody hand-writing an ArcGIS fixture puts `exceededTransferLimit` in it.
That is the entire reason these files exist. `test_the_sighting_28_body_is_
clean` is the assertion that would have failed on the day the bug was
committed.

THE LINE THIS FILE HOLDS

A recording tests **what a server sends**. Synthetic rows test **selection
logic**. Conflating the two is how a suite ends up agreeing with a bug, so
where a fixture cannot answer a question -- and one of them cannot, see
`test_the_mobility_clip_holds_no_us_rows` -- that is asserted out loud rather
than worked around.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from pathlib import Path

import pytest

from scripts.probe_road_feeds import (FEED_INFO_KEYS, caller_capped,
                                      looks_credentialed, looks_like_error_object,
                                      summarise)

FIX = Path(__file__).parent / "fixtures" / "road"


def manifest() -> dict:
    return json.loads((FIX / "manifest.json").read_text(encoding="utf-8"))


def entry(name: str) -> dict:
    for b in manifest()["bodies"]:
        if b["name"] == name:
            return b
    raise AssertionError(f"no manifest entry for {name}")


def body(name: str) -> bytes:
    return (FIX / f"{name}.body").read_bytes()


def summarise_fixture(name: str) -> dict:
    """Run the real summariser over the real bytes, with the real headers.

    The content type and the requested-cap flag come from the manifest, not
    from a guess here, so the test reproduces the conditions of the fetch
    rather than conditions that happen to make it pass.
    """
    e = entry(name)
    return summarise(body(name), e["content_type"], e["clipped"],
                     caller_capped(e["url"]))


# -- the fixtures are what they claim to be ---------------------------------

def test_every_fixture_matches_its_recorded_digest() -> None:
    """A fixture edited by hand stops being a recording.

    This is the guard against the most tempting repair in the file: changing
    a byte so a failing test passes. The manifest holds the SHA-256 the
    server's response hashed to, and a mismatch means the recording was
    altered -- at which point every other test here is testing somebody's
    expectations again.
    """
    for b in manifest()["bodies"]:
        if not b.get("saved"):
            continue
        got = hashlib.sha256((FIX / b["file"]).read_bytes()).hexdigest()
        assert got == b["sha256_of_saved"], (
            f"{b['file']} no longer hashes to what the server sent. "
            f"Re-capture it with --save-bodies; do not edit a recording.")


def test_the_manifest_states_the_clip_rather_than_implying_it() -> None:
    """One fixture is clipped. A clipped body whose length is unstated is a
    stored measurement without the input that defines it, and any test
    counting rows in it would be measuring the clip."""
    clipped = [b for b in manifest()["bodies"] if b.get("clipped")]
    assert len(clipped) == 1
    m = clipped[0]
    assert m["name"] == "mobility-registry"
    assert m["bytes_saved"] == m["clip_limit"] < m["bytes_read"]


def test_no_fixture_was_saved_from_a_credentialed_url() -> None:
    for b in manifest()["bodies"]:
        if b.get("saved"):
            assert not looks_credentialed(b["url"]), b["name"]


# -- sighting 28 ------------------------------------------------------------

def test_the_sighting_28_body_is_clean(request) -> None:
    """THE ASSERTION THIS FILE EXISTS FOR.

    Two real ArcGIS GeoJSON responses, byte for byte as the service sent
    them, must produce **no flags at all**. Before 2026-10-05 both carried
    `ERROR OBJECT: the body is an error even though the transport succeeded`,
    four lines below five correctly parsed features.
    """
    for name in ("chart-cameras-arcgis", "md-aadt-arcgis-alt"):
        s = summarise_fixture(name)
        assert s["kind"] == "geojson", name
        assert s["features"] == 5, name
        assert s["flags"] == [], f"{name} flags: {s['flags']}"


def test_the_arcgis_fixtures_really_carry_the_trap() -> None:
    """A regression test is worthless if the fixture lacks the thing that
    caused the regression. Both bodies must actually contain
    `exceededTransferLimit`, set true, beside `type` and `features` -- which
    is what made three keys, one of them error-ish, look like an error."""
    for name in ("chart-cameras-arcgis", "md-aadt-arcgis-alt"):
        obj = json.loads(body(name).decode("utf-8"))
        assert obj["exceededTransferLimit"] is True, name
        assert set(obj) >= {"type", "features", "exceededTransferLimit"}, name
        # And the guard must say no when asked directly about this body.
        assert not looks_like_error_object(obj), name


def test_a_requested_cap_is_recorded_and_not_flagged() -> None:
    """Rule 16. Both ArcGIS queries pass `resultRecordCount`, so the
    truncation was asked for. Reporting it as a service decision was a flag
    firing on a condition the caller created."""
    for name in ("chart-cameras-arcgis", "md-aadt-arcgis-alt"):
        assert caller_capped(entry(name)["url"]), name
        s = summarise_fixture(name)
        assert s.get("pagination")
        assert not any("TRUNCATION" in f for f in s["flags"]), name


# -- the CHART pages --------------------------------------------------------

CF_EMAIL = re.compile(rb"/cdn-cgi/l/email-protection#[0-9a-f]+")


def normalise_cloudflare(b: bytes) -> bytes:
    """Blank Cloudflare's email-obfuscation token, which rotates per response.

    Needed because of an error worth keeping: the two CHART speed responses
    were reported as "byte-identical", on the evidence that both were 39,772
    bytes. They are not byte-identical. They differ in 56 characters, all of
    them inside a `/cdn-cgi/l/email-protection#` href whose XOR key changes
    every response, which is why the lengths matched exactly.

    The conclusion -- that these endpoints do not content-negotiate -- was
    right. The evidence given for it was a proxy for the thing, and a proxy
    that happened to agree. This function is the actual comparison.
    """
    return CF_EMAIL.sub(b"/cdn-cgi/l/email-protection#X", b)


def test_chart_answers_html_for_a_feed_url() -> None:
    for name in ("chart-speed", "chart-cameras", "chart-incidents"):
        s = summarise_fixture(name)
        assert s["kind"] == "html", name
        assert len(s["flags"]) == 1, name
        assert "HTML, NOT DATA" in s["flags"][0], name


def test_html_is_named_html_and_not_malformed_xml() -> None:
    """A page begins with `<`, so a naive sniff calls it XML and then reports
    a parse error -- which reads as broken data rather than as the wrong kind
    of thing entirely."""
    s = summarise_fixture("chart-speed")
    assert s["kind"] == "html"
    assert not any("XML" in f for f in s["flags"])


def test_chart_does_not_content_negotiate() -> None:
    """Same URL, `Accept: */*` against `Accept: application/json`. Identical
    once Cloudflare's rotating token is blanked, so the server returns the
    same page either way and content negotiation is not the explanation for
    the HTML."""
    for stem in ("chart-speed", "chart-incidents"):
        a, b = body(stem), body(f"{stem}-json")
        assert len(a) == len(b), stem
        assert a != b, (
            f"{stem}: the two bodies are byte-identical, so the Cloudflare "
            f"token no longer rotates and this test's premise has changed")
        assert normalise_cloudflare(a) == normalise_cloudflare(b), stem
    assert entry("chart-speed")["accept_sent"] == "*/*"
    assert entry("chart-speed-json")["accept_sent"] == "application/json"


# -- Socrata ----------------------------------------------------------------

def test_socrata_serves_every_value_as_a_string() -> None:
    """The `alt_baro` trap in a second domain. `crz_entries` is the count the
    whole Tier 3 test would rest on, and it arrives as text."""
    s = summarise_fixture("mta-crz")
    assert s["kind"] == "json"
    assert s["records"] == 5
    rec = s["first_record"]
    assert rec["crz_entries"] == "str(numeric-looking)"
    for field in ("hour_of_day", "minute_of_hour", "day_of_week_int",
                  "excluded_roadway_entries"):
        assert rec[field] == "str(numeric-looking)", field
    assert not any(t in ("int", "float") for t in rec.values()), (
        "a Socrata field came back typed; this fixture's premise has changed")


def test_a_count_envelope_shows_its_value() -> None:
    """A `returnCountOnly` answer is four keys of scalars at most, so the
    value is shown rather than its type. Withholding it would make the
    question unanswerable, which is the only reason the types-only rule is
    relaxed at all."""
    assert summarise_fixture("md-aadt-count-all")["scalars"] == {"count": 8773}
    assert summarise_fixture("md-aadt-count-car")["scalars"] == {"count": 2695}


# -- the WZDx registry ------------------------------------------------------

def test_the_registry_is_43_rows_of_13_string_columns() -> None:
    s = summarise_fixture("wzdx-registry")
    assert s["kind"] == "csv"
    assert s["rows"] == 43
    assert s["column_count"] == 13
    assert s["flags"] == []
    assert "needAPIKey" in s["columns"]


def test_thirty_feeds_are_keyless_not_twenty_two() -> None:
    """A number corrected by reading the file.

    "22 keyless" was written into the plan, the status note, the backlog and
    a commit message. It came from subtracting a count in a model's summary
    of a web page from 43. The registry itself says `needAPIKey` is "false"
    for **30** rows and "true" for 13.
    """
    rows = list(csv.DictReader(io.StringIO(
        body("wzdx-registry").decode("utf-8-sig"))))
    assert len(rows) == 43
    vals = [r["needAPIKey"].strip().lower() for r in rows]
    assert vals.count("false") == 30
    assert vals.count("true") == 13
    assert set(vals) == {"false", "true"}, (
        "a third value appeared in needAPIKey; a selection rule testing "
        "truthiness of this string would read 'false' as True")


def test_the_registry_version_column_is_not_a_number() -> None:
    """`version` holds '', '3.1', '4', '4.1', '4.2' and 'CWZ 1.0'. Anything
    comparing it numerically breaks on two of those six."""
    rows = list(csv.DictReader(io.StringIO(
        body("wzdx-registry").decode("utf-8-sig"))))
    versions = {r["version"].strip() for r in rows}
    assert "CWZ 1.0" in versions
    assert "" in versions
    for v in versions:
        if v in ("", "CWZ 1.0"):
            with pytest.raises(ValueError):
                float(v)


def test_one_registry_row_is_inactive() -> None:
    """`active` is a string too, and one feed is "false". A selection rule
    that filtered only on needAPIKey would poll a retired feed."""
    rows = list(csv.DictReader(io.StringIO(
        body("wzdx-registry").decode("utf-8-sig"))))
    assert [r["active"].strip().lower() for r in rows].count("false") == 1


def test_the_state_column_is_not_a_clean_key() -> None:
    """'arizona', 'North Dakota', 'New Hampshire, Vermont, Maine' and 'n/a'
    are all real values. Anything grouping by state normalises first, and
    'n/a' is a value rather than a gap."""
    rows = list(csv.DictReader(io.StringIO(
        body("wzdx-registry").decode("utf-8-sig"))))
    states = {r["state"] for r in rows}
    assert "arizona" in states and "North Dakota" in states
    assert "n/a" in states
    assert any("," in s for s in states)


# -- the Mobility Database clip ---------------------------------------------

def test_a_clipped_csv_reports_a_lower_bound_and_never_a_count() -> None:
    """The distinction that was load-bearing once already: a capped read
    reported `rows_at_least` 4,930 where the true figure was 6,479. Anyone
    quoting the capped run would have been short by 1,549."""
    s = summarise_fixture("mobility-registry")
    assert s["kind"] == "csv"
    assert "rows" not in s
    assert s["rows_at_least"] > 0
    assert any("TRUNCATED" in f for f in s["flags"])


def test_authentication_type_arrives_as_a_string() -> None:
    s = summarise_fixture("mobility-registry")
    assert s["first_row"]["urls.authentication_type"] == "str(numeric-looking)"


def test_the_mobility_clip_holds_no_us_rows() -> None:
    """A LIMITATION ASSERTED OUT LOUD, SO THAT CHANGING IT IS LOUD.

    This fixture is the first 256 kB of a 2.6 MB file and contains 450 rows,
    none of them US. So it cannot test the selection of keyless US
    vehicle-position feeds: a test asserting "the selector finds them" would
    assert zero, pass, and measure nothing -- which is the failure mode this
    whole file was written against, arrived at from the other direction.

    Selection logic is therefore tested against synthetic rows, in
    `tests/test_road_registries.py`, and this fixture tests parsing only. If
    somebody re-captures it with a larger clip, this test fails and says why,
    instead of the others quietly starting to mean something different.
    """
    text = body("mobility-registry").decode("utf-8-sig", errors="replace")
    text = text[:text.rfind("\n") + 1]
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 450
    assert all(r["location.country_code"] != "US" for r in rows)
    assert {r["data_type"] for r in rows} == {"gtfs", "gtfs_rt"}


# -- MDOT, and the key this code was not asking for -------------------------

def test_mdot_declares_version_4_1_under_feed_info() -> None:
    """Sighting 31.

    This lookup asked for `road_event_feed_info`, the name WZDx v3 uses. v4
    renamed it `feed_info`, every feed in the registry is v4 or later, and
    MDOT's is 4.1 -- so the probe recorded `"feed_info": null` and printed
    nothing, which reads as a feed that declares no version. It declares one.
    """
    s = summarise_fixture("mdot-wzdx")
    assert s["feed_info_key"] == "feed_info"
    assert s["declared_version"] == "4.1"
    assert s["feed_info"]["publisher"] == "str(len=16)"
    assert FEED_INFO_KEYS[0] == "feed_info", (
        "v4 must be tried first; v3's name is the fallback")


def test_the_registry_and_the_feed_agree_on_the_version() -> None:
    """The G1 guard, now that the right key is being read. The registry's
    claim about MDOT and the feed's claim about itself must match; a
    disagreement means one of them is stale and the fetcher must refuse
    rather than trust a filename, which is how 483 polygons became a legal
    boundary line.

    Also a correction. "Maryland DOT (mdot)" in a model's summary of the
    registry page read as an organisation called `mdot`; the registry has
    `issuingOrganization` "Maryland DOT" and `feedName` "mdot". Two columns,
    presented as one parenthetical, and matched on the wrong one.
    """
    rows = list(csv.DictReader(io.StringIO(
        body("wzdx-registry").decode("utf-8-sig"))))
    md = [r for r in rows if r["feedName"].strip().lower() == "mdot"]
    assert len(md) == 1
    assert md[0]["issuingOrganization"].strip() == "Maryland DOT"
    assert md[0]["state"].strip() == "maryland"
    assert md[0]["needAPIKey"].strip().lower() == "false"
    assert md[0]["version"].strip() == "4.1"
    assert summarise_fixture("mdot-wzdx")["declared_version"] == "4.1"


def test_mdot_carries_two_geometry_types() -> None:
    """206 features: 140 LineString and 66 MultiPoint. Three days earlier the
    same feed returned 65 features, LineString only. A fetcher written
    against the first observation would have met MultiPoint in production."""
    s = summarise_fixture("mdot-wzdx")
    assert s["features"] == 206
    assert set(s["geometry_types"]) == {"LineString", "MultiPoint"}
    assert s["flags"] == []


def test_mdot_properties_are_nested_and_properly_typed() -> None:
    """`core_details` is an object, `lanes` a list, and the four
    `is_*_verified` fields are real booleans rather than the strings every
    other feed in this file serves."""
    s = summarise_fixture("mdot-wzdx")
    p = s["first_feature_properties"]
    assert p["core_details"].startswith("dict(")
    assert p["lanes"].startswith("list(")
    for f in ("is_end_date_verified", "is_start_date_verified",
              "is_end_position_verified", "is_start_position_verified"):
        assert p[f] == "bool", f


def test_an_arcgis_body_declares_no_version_and_is_not_flagged_for_it() -> None:
    """Rule 16 again. An ArcGIS FeatureCollection has no `feed_info` under
    either name, legitimately. Recording that is right; flagging it would
    fire on every non-WZDx GeoJSON and train the reader to ignore flags."""
    for name in ("chart-cameras-arcgis", "md-aadt-arcgis-alt"):
        s = summarise_fixture(name)
        assert s["feed_info_key"] is None, name
        assert s["declared_version"] is None, name
        assert s["flags"] == [], name
