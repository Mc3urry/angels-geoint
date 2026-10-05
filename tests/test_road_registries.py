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
    sel, exc = transit_selection([transit_row()])
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
    sel, exc = transit_selection([transit_row(**over)])
    assert not sel
    assert len(exc) == 1 and exc[0]["rule"] == rule


def test_a_blank_authentication_type_means_none() -> None:
    """The published CSV leaves the column blank for feeds needing nothing.
    Asserted here because treating blank as 'unknown, exclude' would drop
    most of the catalogue silently."""
    sel, _ = transit_selection([transit_row(**{"urls.authentication_type": ""})])
    assert len(sel) == 1


def test_entity_type_is_a_list_in_one_cell() -> None:
    """'vp,tu' and 'vp tu' are both real. A substring test would also match
    a hypothetical 'vpx', so the cell is split rather than searched."""
    for cell in ("vp", "vp,tu", "tu,vp", "vp tu sa"):
        sel, _ = transit_selection([transit_row(entity_type=cell)])
        assert len(sel) == 1, cell
    for cell in ("tu", "sa", "tu,sa", ""):
        sel, _ = transit_selection([transit_row(entity_type=cell)])
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
    sel, exc = transit_selection([
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
    sel, exc = transit_selection([
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
