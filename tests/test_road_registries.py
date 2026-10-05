"""Which road feeds get polled, and why each one that does not, does not.

Nothing here touches the network.

THE LINE THIS FILE HOLDS, STATED ONCE

Recordings test what servers send. Synthetic rows test selection logic.
`tests/test_probe_road_feeds.py` holds the first kind; this file is mostly
the second, because the committed Mobility recording is a 256 kB clip of a
2.6 MB file and holds **no US rows at all** -- so a test asserting "the
selector finds keyless US vehicle-position feeds" against it would assert
zero, pass, and measure nothing. That is the failure mode of the whole
fixture exercise, reached from the other direction, and it is asserted out
loud over there rather than worked around here.

The WZDx registry is 7,953 bytes and complete, so the parsing tests below
use the real recording.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from scripts.fetch_road_registries import (KNOWN_WZDX_VERSIONS,
                                           RegistryRefused, artefact,
                                           bounding_box,
                                           check_arithmetic,
                                           check_no_credentials,
                                           parse_registry_csv, safe_url,
                                           transit_selection, word_to_bool,
                                           wzdx_selection)

FIX = Path(__file__).parent / "fixtures" / "road"
NOW = __import__("datetime").datetime(2026, 10, 5,
                                      tzinfo=__import__("datetime").timezone.utc)


def wzdx_row(**over) -> dict:
    base = {"feedName": "x", "issuingOrganization": "X DOT", "state": "x",
            "version": "4.1", "active": "true", "needAPIKey": "false",
            "url": "https://example.test/wzdx.geojson", "apiKeyURL": ""}
    base.update(over)
    return base


def transit_row(**over) -> dict:
    base = {"id": "mdb-1", "provider": "X Transit", "data_type": "gtfs_rt",
            "entity_type": "vp", "location.country_code": "US",
            "location.subdivision_name": "Maryland",
            "location.municipality": "Baltimore",
            "urls.direct_download": "https://example.test/vp.pb",
            "urls.authentication_type": "0", "urls.authentication_info": "",
            "status": "active"}
    base.update(over)
    return base


# -- refusals ---------------------------------------------------------------

def test_a_truncated_read_is_refused() -> None:
    """A registry is a denominator. A capped read of this same catalogue
    once reported 4,930 rows where the true figure was 6,479."""
    with pytest.raises(RegistryRefused, match="truncated"):
        parse_registry_csv(b"a,b\n1,2\n", truncated=True)


def test_an_html_page_is_refused_rather_than_parsed_to_zero_rows() -> None:
    """Three chart.maryland.gov endpoints answer HTTP 200 with a web page.
    A registry that parsed a sign-in page into zero rows would report an
    outage as an empty world."""
    with pytest.raises(RegistryRefused, match="HTML"):
        parse_registry_csv(b"<!DOCTYPE html><html><body>hi</body></html>")


def test_a_json_error_body_is_refused() -> None:
    """ArcGIS and Socrata answer a bad query with 200 and an error object."""
    with pytest.raises(RegistryRefused, match="JSON"):
        parse_registry_csv(b'{"error":{"code":400,"message":"Invalid"}}')


def test_an_empty_registry_is_refused_not_believed() -> None:
    with pytest.raises(RegistryRefused, match="zero"):
        parse_registry_csv(b"state,url\n")


def test_a_bom_is_handled() -> None:
    rows = parse_registry_csv(b"\xef\xbb\xbfstate,url\nmd,https://x\n")
    assert rows[0]["state"] == "md"


# -- the booleans that are text ---------------------------------------------

def test_false_is_false_and_not_a_non_empty_string() -> None:
    """`bool("false")` is True, and `needAPIKey` is text. This function
    exists entirely because of that."""
    assert word_to_bool("false", "needAPIKey") is False
    assert word_to_bool("FALSE", "needAPIKey") is False
    assert word_to_bool(" true ", "active") is True


def test_a_word_outside_the_vocabulary_is_refused_not_guessed() -> None:
    """Guessing here is how a keyed feed gets polled without a key."""
    for bad in ("maybe", "", "2", "null"):
        with pytest.raises(RegistryRefused, match="vocabulary"):
            word_to_bool(bad, "needAPIKey")


# -- WZDx selection ---------------------------------------------------------

def test_a_clean_row_is_selected() -> None:
    sel, exc = wzdx_selection([wzdx_row()])
    assert len(sel) == 1 and not exc


@pytest.mark.parametrize("over,rule", [
    ({"active": "false"}, "inactive"),
    ({"needAPIKey": "true"}, "needs a key"),
    ({"version": "CWZ 1.0"}, "unknown spec version"),
    ({"version": "3.1"}, "unknown spec version"),
    ({"version": ""}, "unknown spec version"),
    ({"url": ""}, "no url"),
])
def test_each_exclusion_rule_fires_and_is_named(over, rule) -> None:
    sel, exc = wzdx_selection([wzdx_row(**over)])
    assert not sel
    assert len(exc) == 1 and exc[0]["rule"] == rule
    assert exc[0]["why"]


def test_every_row_lands_in_exactly_one_list() -> None:
    """Mechanism G. A feed that falls between the two lists leaves a
    denominator short in a direction nobody can see."""
    rows = [wzdx_row(), wzdx_row(active="false"), wzdx_row(needAPIKey="true"),
            wzdx_row(version="CWZ 1.0"), wzdx_row(url="")]
    sel, exc = wzdx_selection(rows)
    assert len(sel) + len(exc) == len(rows)


def test_cwz_is_excluded_by_name_not_by_a_failed_float() -> None:
    """'CWZ 1.0' is a different specification, not a malformed number."""
    _, exc = wzdx_selection([wzdx_row(version="CWZ 1.0")])
    assert "different specification" in exc[0]["why"]
    assert "4.1" in KNOWN_WZDX_VERSIONS


# -- transit selection ------------------------------------------------------

def test_a_clean_transit_row_is_selected() -> None:
    sel, _cred, exc = transit_selection([transit_row()])
    assert len(sel) == 1 and not exc


@pytest.mark.parametrize("over,rule", [
    ({"data_type": "gtfs"}, "not realtime"),
    ({"entity_type": "tu"}, "no vehicle positions"),
    ({"entity_type": ""}, "no vehicle positions"),
    ({"location.country_code": "CA"}, "outside the study area"),
    ({"urls.authentication_type": "1"}, "needs a key"),
    ({"urls.authentication_type": "2"}, "needs a key"),
    ({"status": "deprecated"}, "not current"),
    ({"status": "inactive"}, "not current"),
    ({"urls.direct_download": ""}, "no url"),
])
def test_each_transit_rule_fires_and_is_named(over, rule) -> None:
    sel, _cred, exc = transit_selection([transit_row(**over)])
    assert not sel
    assert len(exc) == 1 and exc[0]["rule"] == rule


def test_a_blank_authentication_type_means_none() -> None:
    """The published CSV leaves the column blank for feeds needing nothing.
    Asserted here because treating blank as 'unknown, exclude' would drop
    most of the catalogue silently."""
    sel, _c, _e = transit_selection([transit_row(**{"urls.authentication_type": ""})])
    assert len(sel) == 1


def test_entity_type_is_a_list_in_one_cell() -> None:
    """'vp,tu' and 'vp tu' are both real. A substring test would also match
    a hypothetical 'vpx', so the cell is split rather than searched."""
    for cell in ("vp", "vp,tu", "tu,vp", "vp tu sa"):
        sel, _c, _e = transit_selection([transit_row(entity_type=cell)])
        assert len(sel) == 1, cell
    for cell in ("tu", "sa", "tu,sa", ""):
        sel, _c, _e = transit_selection([transit_row(entity_type=cell)])
        assert not sel, cell


# -- the credential rule, which this file's author needed twice -------------

def test_a_credentialed_url_never_reaches_the_artefact() -> None:
    """MECHANISM X, AVOIDED THE SECOND TIME.

    The draft of `fetch_road_registries.py` recorded every excluded feed
    with its url, so a gap in coverage would be visible rather than silent.
    Right instinct. But the Mobility catalogue publishes feed urls with
    tokens in them, and the feeds excluded FOR NEEDING A CREDENTIAL are
    precisely the ones whose urls carry one -- so the artefact would have
    published other people's tokens as a direct consequence of a rule about
    honesty.
    """
    dirty = "https://example.test/feed.json?token=abcdefghijklmnopqrstuvwx"
    sel, _cred, exc = transit_selection([
        transit_row(**{"urls.direct_download": dirty,
                       "urls.authentication_type": "1"})])
    assert not sel and len(exc) == 1
    assert "token=abcdefghijklmnopqrstuvwx" not in json.dumps(exc)
    assert "query redacted" in exc[0]["url"]
    # The host and path survive, so the exclusion is still legible.
    assert "example.test/feed.json" in exc[0]["url"]


def test_a_clean_url_is_not_redacted() -> None:
    """Rule 16. A redaction that fired on everything would make the
    artefact useless and train the reader to ignore the marker."""
    clean = "https://example.test/vp.pb?agency=5"
    assert safe_url(clean) == clean


def test_a_feed_whose_url_and_catalogue_disagree_is_not_polled() -> None:
    """Two records of one fact disagreeing is a finding, not something to
    reconcile. Whichever is wrong, polling it means sending somebody else's
    credential."""
    dirty = "https://example.test/feed.json?api_key=abcdefghijklmnopqrst"
    sel, _cred, exc = transit_selection([
        transit_row(**{"urls.direct_download": dirty,
                       "urls.authentication_type": "0"})])
    assert not sel
    assert exc[0]["rule"] == "url and catalogue disagree"

    sel, exc = wzdx_selection([wzdx_row(url=dirty, needAPIKey="false")])
    assert not sel
    assert exc[0]["rule"] == "url and registry disagree"


def test_the_backstop_reads_the_finished_artefact() -> None:
    """A guard and a backstop that disagree is a finding; a guard alone is
    a feeling. This one trusts nothing and looks at what is about to be
    written."""
    art = {"selected": [{"url": "https://x/a?token=abcdefghijklmnopqrstuv"}]}
    with pytest.raises(RegistryRefused, match="will not be written"):
        check_no_credentials(art)
    check_no_credentials({"selected": [{"url": "https://x/a"}]})


# -- the artefact -----------------------------------------------------------

def test_the_artefact_records_the_inputs_that_define_it() -> None:
    """Rule 12. Endpoint, fetch time, digest of the bytes it was computed
    from, and the rule set -- in the same file as the numbers."""
    rows = [wzdx_row(), wzdx_row(needAPIKey="true")]
    sel, exc = wzdx_selection(rows)
    art = artefact("k", "https://x", b"raw", rows, sel, exc,
                   when=NOW, rules={"a": "b"})
    for field in ("_endpoint", "_fetched_at_utc", "_bytes", "_sha256",
                  "_selection_rules", "rows_read", "n_selected",
                  "n_excluded", "excluded_by_rule", "selected", "excluded"):
        assert field in art
    assert art["_bytes"] == 3
    assert art["_fetched_at_utc"].startswith("2026-10-05")
    # The digest's value is asserted in the next test, against hashlib.


def test_the_digest_is_of_the_bytes_not_of_the_rows() -> None:
    import hashlib
    raw = b"state,url\nmd,https://x\n"
    rows = parse_registry_csv(raw)
    art = artefact("k", "https://x", raw, rows, [], [{"rule": "r", "why": "w"}],
                   when=NOW, rules={})
    assert art["_sha256"] == hashlib.sha256(raw).hexdigest()


def test_the_counts_have_to_add_up() -> None:
    rows = [wzdx_row(), wzdx_row(needAPIKey="true")]
    sel, exc = wzdx_selection(rows)
    art = artefact("k", "u", b"r", rows, sel, exc, when=NOW, rules={})
    check_arithmetic(art)
    art["n_selected"] += 1
    with pytest.raises(RegistryRefused, match="lost between"):
        check_arithmetic(art)


def test_the_per_rule_counts_have_to_add_up_too() -> None:
    rows = [wzdx_row(needAPIKey="true")]
    sel, exc = wzdx_selection(rows)
    art = artefact("k", "u", b"r", rows, sel, exc, when=NOW, rules={})
    art["excluded_by_rule"]["invented"] = 3
    with pytest.raises(RegistryRefused, match="per-rule"):
        check_arithmetic(art)


# -- against the real recording ---------------------------------------------

def test_the_real_registry_parses_and_selects() -> None:
    """The committed WZDx recording is complete at 7,953 bytes, so this one
    is a recording test rather than a synthetic one."""
    raw = (FIX / "wzdx-registry.body").read_bytes()
    rows = parse_registry_csv(raw)
    assert len(rows) == 43
    sel, exc = wzdx_selection(rows)
    assert len(sel) + len(exc) == 43
    # 30 are keyless; some of those are excluded again for version or
    # activity, so the selected count is at most 30 and more than half.
    assert 15 <= len(sel) <= 30
    by_rule = {}
    for e in exc:
        by_rule[e["rule"]] = by_rule.get(e["rule"], 0) + 1
    assert by_rule.get("needs a key") == 13
    assert by_rule.get("inactive", 0) >= 1


def test_the_real_registry_artefact_passes_both_checks() -> None:
    raw = (FIX / "wzdx-registry.body").read_bytes()
    rows = parse_registry_csv(raw)
    sel, exc = wzdx_selection(rows)
    art = artefact("k", "https://x", raw, rows, sel, exc,
                   when=NOW, rules={"version": list(KNOWN_WZDX_VERSIONS)})
    check_arithmetic(art)
    check_no_credentials(art)


# -- feeds this project holds a key for -------------------------------------
#
# The whole design question here is what `n_selected` means. It means
# "pollable by anyone with no account", which is what makes the continental
# arm reproducible by a reader who has none. A key must not quietly change
# that number, so credentialed feeds are a third list and the tests below are
# mostly about keeping the three disjoint and the arithmetic honest.

NAMED = ({"id": "mdb-1850", "expect_provider": "WMATA", "env": "WMATA_API_KEY",
          "header": "api_key", "why": "most of the vehicle-hours in the AOI"},)


def keyed_row(**over) -> dict:
    return transit_row(**{"id": "mdb-1850",
                          "provider": "Washington Metropolitan Area Transit "
                                      "Authority (WMATA)",
                          "urls.authentication_type": "1", **over})


def test_a_named_feed_with_a_key_present_is_credentialed_not_selected() -> None:
    sel, cred, exc = transit_selection(
        [keyed_row()], credentialed=NAMED, env={"WMATA_API_KEY": "x" * 32})
    assert not sel, "a keyed feed must never enter the keyless count"
    assert not exc
    assert len(cred) == 1
    assert cred[0]["env_var"] == "WMATA_API_KEY"
    assert cred[0]["header"] == "api_key"


def test_the_key_value_never_reaches_the_record() -> None:
    """Only the variable's name. A record carrying the value would be the
    registry artefact publishing a credential, which is the thing three
    separate guards in this project now exist to prevent."""
    secret = "s3cret" * 6
    _, cred, _ = transit_selection(
        [keyed_row()], credentialed=NAMED, env={"WMATA_API_KEY": secret})
    assert secret not in json.dumps(cred)
    check_no_credentials({"credentialed": cred})


def test_a_named_feed_with_no_key_set_stays_excluded() -> None:
    """THE HONEST CASE.

    Naming a feed in code does not make it pollable. With the variable
    unset the feed stays excluded, and the reason says which variable is
    missing rather than repeating the generic one -- so a reader of the
    artefact can tell "we chose not to" from "we meant to and could not".
    """
    sel, cred, exc = transit_selection(
        [keyed_row()], credentialed=NAMED, env={})
    assert not sel and not cred
    assert exc[0]["rule"] == "needs a key"
    assert "WMATA_API_KEY" in exc[0]["why"] and "not set" in exc[0]["why"]


def test_an_empty_key_counts_as_unset() -> None:
    """`.env` with a bare `WMATA_API_KEY=` is the normal state of a fresh
    checkout, and it must not look like a credential."""
    _, cred, exc = transit_selection(
        [keyed_row()], credentialed=NAMED, env={"WMATA_API_KEY": ""})
    assert not cred and exc[0]["rule"] == "needs a key"


def test_a_named_id_that_is_not_the_expected_operator_is_refused() -> None:
    """The cross-check. An id is opaque, so each entry carries the provider
    it is expected to be. A credential sent to the wrong operator is a worse
    outcome than a missing feed."""
    sel, cred, exc = transit_selection(
        [keyed_row(provider="Some Other Transit Agency")],
        credentialed=NAMED, env={"WMATA_API_KEY": "x" * 32})
    assert not sel and not cred
    assert exc[0]["rule"] == "named feed does not match"
    assert "WMATA" in exc[0]["why"]


def test_a_key_does_not_rescue_any_other_exclusion() -> None:
    """A key buys exactly one thing. It does not make a trip-updates feed
    into a vehicle-positions feed, or a Canadian one into a US one."""
    env = {"WMATA_API_KEY": "x" * 32}
    for over, rule in (({"entity_type": "tu"}, "no vehicle positions"),
                       ({"data_type": "gtfs"}, "not realtime"),
                       ({"location.country_code": "CA"},
                        "outside the study area"),
                       ({"status": "deprecated"}, "not current")):
        sel, cred, exc = transit_selection(
            [keyed_row(**over)], credentialed=NAMED, env=env)
        assert not sel and not cred, (over, "a key rescued the wrong thing")
        assert exc[0]["rule"] == rule


def test_an_unnamed_keyed_feed_is_untouched() -> None:
    """78 feeds need keys this project does not hold. They stay excluded
    with the generic reason, so the gap in coverage is still visible."""
    _, cred, exc = transit_selection(
        [keyed_row(id="mdb-9999", provider="Somewhere Transit")],
        credentialed=NAMED, env={"WMATA_API_KEY": "x" * 32})
    assert not cred
    assert exc[0]["rule"] == "needs a key"
    assert "not set" not in exc[0]["why"]


def test_the_three_lists_are_disjoint_and_complete() -> None:
    rows = [transit_row(id="a"), keyed_row(), transit_row(id="c",
            data_type="gtfs"), transit_row(id="d", entity_type="tu")]
    sel, cred, exc = transit_selection(
        rows, credentialed=NAMED, env={"WMATA_API_KEY": "x" * 32})
    assert len(sel) + len(cred) + len(exc) == len(rows)
    ids = [e["id"] for e in sel] + [e["id"] for e in cred] + \
          [e["id"] for e in exc]
    assert len(ids) == len(set(ids)), "a row appears in two lists"


def test_the_arithmetic_check_counts_all_three() -> None:
    rows = [transit_row(id="a"), keyed_row()]
    sel, cred, exc = transit_selection(
        rows, credentialed=NAMED, env={"WMATA_API_KEY": "x" * 32})
    art = artefact("k", "u", b"r", rows, sel, exc, when=NOW, rules={},
                   credentialed=cred)
    check_arithmetic(art)
    assert art["n_credentialed"] == 1
    art["n_credentialed"] = 0
    with pytest.raises(RegistryRefused, match="lost between"):
        check_arithmetic(art)


# -- the bounding box, which G2's feed list depends on ----------------------
#
# 189 selected feeds times 2,880 polls a day is more than half a million
# requests, which is not a thing a laptop does. The boundary test does not
# want all of them: it wants agencies whose service area touches a state
# line. That selection is geometric, so the geometry has to be in the
# artefact or the selection is not reviewable.

def bbox_row(**over) -> dict:
    base = {"location.bounding_box.minimum_latitude": "38.80",
            "location.bounding_box.maximum_latitude": "39.10",
            "location.bounding_box.minimum_longitude": "-77.20",
            "location.bounding_box.maximum_longitude": "-76.90",
            "location.bounding_box.extracted_on": "2026-09-01T00:00:00Z"}
    base.update(over)
    return base


def test_a_box_arrives_as_text_and_is_cast() -> None:
    """Every column in this catalogue is a string. A numeric filter over a
    string column is how the aviation analysis once reported 100 per cent of
    independent targets at 0 feet."""
    b = bounding_box(bbox_row())
    assert b["valid"] is True
    for k in ("min_lat", "max_lat", "min_lon", "max_lon"):
        assert isinstance(b[k], float), k
    assert b["min_lat"] == 38.80 and b["max_lon"] == -76.90


def test_the_extraction_date_travels_with_the_box() -> None:
    """Rule 12. A box extracted in 2019 and one extracted last month are
    different evidence about where an agency runs today."""
    assert bounding_box(bbox_row())["extracted_on"] == "2026-09-01T00:00:00Z"


def test_no_box_at_all_is_none_and_not_a_zero_box() -> None:
    """A feed with no geometry must not become a feed at the origin, which
    is in the Gulf of Guinea and would be inside nobody's state line."""
    assert bounding_box({}) is None
    assert bounding_box({k: "" for k in bbox_row()}) is None


@pytest.mark.parametrize("over,fragment", [
    ({"location.bounding_box.minimum_latitude": ""}, "only part"),
    ({"location.bounding_box.minimum_latitude": "n/a"}, "not a number"),
    ({"location.bounding_box.minimum_latitude": "-91"}, "outside"),
    ({"location.bounding_box.maximum_longitude": "181"}, "outside"),
    ({"location.bounding_box.minimum_latitude": "39.5"}, "inside out"),
    ({"location.bounding_box.minimum_longitude": "-70.0"}, "inside out"),
])
def test_an_unusable_box_says_why_rather_than_vanishing(over, fragment) -> None:
    """Present-and-unusable is a third fact, distinct from absent and from
    usable. Collapsing it into either one reports it as the other."""
    b = bounding_box(bbox_row(**over))
    assert b is not None and b["valid"] is False
    assert fragment in b["why"]


def test_the_three_box_outcomes_are_distinguishable() -> None:
    assert bounding_box({}) is None
    assert bounding_box(bbox_row())["valid"] is True
    assert bounding_box(bbox_row(
        **{"location.bounding_box.minimum_latitude": "x"}))["valid"] is False


def test_selected_feeds_carry_their_box() -> None:
    sel, _c, _e = transit_selection([transit_row(**bbox_row())],
                                    credentialed=(), env={})
    assert sel[0]["bbox"]["valid"] is True


def test_the_artefact_counts_boxes_before_anything_narrows_by_them() -> None:
    """The denominator for G2's geographic narrowing, visible up front
    rather than discovered afterwards as a shortfall."""
    rows = [transit_row(id="a", **bbox_row()),
            transit_row(id="b"),
            transit_row(id="c", **bbox_row(
                **{"location.bounding_box.minimum_latitude": "99"}))]
    sel, cred, exc = transit_selection(rows, credentialed=(), env={})
    art = artefact("k", "u", b"r", rows, sel, exc, when=NOW, rules={},
                   credentialed=cred)
    assert art["bounding_boxes"] == {"usable": 1, "absent": 1, "invalid": 1}
    assert sum(art["bounding_boxes"].values()) == art["n_selected"] + \
        art["n_credentialed"]


def test_real_recorded_rows_have_real_boxes() -> None:
    """Parsing against the recording rather than against my idea of it. The
    Mobility clip holds no US rows, so selection cannot be tested here --
    but every row in it carries the same four columns, which is exactly the
    part worth testing against what the server actually sent."""
    import csv as _csv
    text = (FIX / "mobility-registry.body").read_bytes().decode(
        "utf-8-sig", errors="replace")
    text = text[:text.rfind("\n") + 1]
    rows = list(_csv.DictReader(io.StringIO(text)))
    boxes = [bounding_box(r) for r in rows]
    usable = [b for b in boxes if b and b.get("valid")]
    assert len(usable) > 50, "the recording should hold plenty of real boxes"
    for b in usable:
        assert -90 <= b["min_lat"] <= b["max_lat"] <= 90
        assert -180 <= b["min_lon"] <= b["max_lon"] <= 180
