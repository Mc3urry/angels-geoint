"""G1's decisions, every one of them a pure function. No network, no clock.

The polling loop is not tested: it is a `urlopen` in a `for`, and a test of it
would be a test of a mock. Everything that *decides* anything -- what a feed
said, whether it changed, whether the cycle is a measurement or an outage --
is exercised here against bytes and status codes.

WHAT THIS FILE IS DEFENDING

The land domain's cooperative layer is 25 feeds behind one collector, and the
failure that would quietly ruin it is not a crash. It is one feed going dark
while twenty-four answer, the archive containing no work zones for that state,
and `road_discrepancy.py` reading the silence as a road with nothing on it.

That is mechanism J -- "missing" that means "missing where I looked" -- and it
is the 18 missing air hours in a different costume. So the distinctions these
tests hold are, in order of how much they matter:

    returned, n_events == 0   the agency says its roads are clear
    returned == False         we were not looking at that agency
    no ledger row at all      the cycle never ran

Three facts, three footprints. Collapsing any pair of them turns our own
blindness into somebody else's behaviour.

THREE DEFECTS THIS FILE FOUND BEFORE IT WAS A FILE

Written alongside the script and run against it as it was built, which is why
all three were caught before the first commit rather than after the first
publication:

  * a gzip round-trip returned False, because body filenames were timestamped
    to the second and three stores inside one second made the third overwrite
    the first. The 2026-10-02 decision about `probe-<date>.json` is the same
    defect one directory over.
  * `store_body` returned `(path, bytes)` and the caller read `path is not
    None` as "the feed changed" -- two different facts in one variable, which
    is the shape that published a file count under the name `n_polls`.
  * the script carried a `try/except ImportError` fallback copy of
    `KNOWN_WZDX_VERSIONS`, a silent second copy of a constant in the file
    whose own docstring cites mechanism D.
"""

from __future__ import annotations

import gzip
import json

import pytest

from scripts.fetch_wzdx import (HTTP_ERROR, KNOWN_WZDX_VERSIONS, NOT_JSON,
                                NOT_WZDX, OK, VERSION_UNKNOWN, append_ledger,
                                blind_feeds, body_path, classify,
                                cycle_flags, cycle_health, cycle_sentence,
                                cycle_summary, declared_version, digest,
                                event_count, feed_record, feeds_from,
                                last_digest, looks_like_html, store_body,
                                version_verdict)

FEED = {"feedName": "mcdot", "issuingOrganization": "Maricopa County DOT",
        "state": "arizona", "version": "4.2",
        "url": "https://wzdxapi.aztech.org/construction"}


def wzdx(version: str = "4.2", n: int = 1, key: str = "feed_info") -> bytes:
    return json.dumps({
        key: {"version": version, "publisher": "x"},
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "id": str(i)} for i in range(n)],
    }).encode()


# -- what a body says about itself ------------------------------------------

def test_version_is_read_from_the_v4_key_and_the_v3_key() -> None:
    """The probe stored `null` and printed nothing for three days because it
    knew only the v3 name while MDOT declares 4.1 under the v4 one. Mechanism
    F, third instance. Both names are tried and the answering one is named."""
    assert declared_version(json.loads(wzdx("4.1"))) == ("4.1", "feed_info")
    assert declared_version(json.loads(wzdx(
        "3.1", key="road_event_feed_info"))) == ("3.1",
                                                 "road_event_feed_info")


def test_a_body_declaring_nothing_says_so_rather_than_defaulting() -> None:
    assert declared_version({"features": []}) == (None, None)
    assert declared_version([1, 2, 3]) == (None, None)
    assert declared_version("not even a mapping") == (None, None)


def test_the_version_stays_a_string() -> None:
    """`float("4.10") == float("4.1")` is True and those are not the same
    specification. Third domain for this trap, after `alt_baro` and the
    registry's own version column."""
    assert version_verdict("4.10", "4.1") == "disagrees"
    v, _ = declared_version(json.loads(wzdx("4.10")))
    assert isinstance(v, str) and v == "4.10"


@pytest.mark.parametrize("declared,registry,expected", [
    ("4.2", "4.2", "agrees"),
    ("4.1", "4.2", "disagrees"),
    (None, "4.2", "absent"),
    ("4.2", None, "registry-silent"),
])
def test_the_version_check_has_four_outcomes_not_two(
        declared, registry, expected) -> None:
    """`absent` is not `agrees`. A feed that declares nothing has told us
    nothing, and substituting the registry's claim for its silence would make
    a catalogue row look like a measurement -- mechanism D, and the reason
    Oklahoma and Florida were excluded by G0 in the first place."""
    assert version_verdict(declared, registry) == expected


def test_the_four_verdicts_are_four_distinct_strings() -> None:
    """Rule 13 applied to a verdict rather than a sentence: a swallowed branch
    returning a neighbour's value looks right in isolation."""
    said = {version_verdict(d, r) for d, r in
            (("4.2", "4.2"), ("4.1", "4.2"), (None, "4.2"), ("4.2", None))}
    assert len(said) == 4


# -- zero is a finding, absent is not --------------------------------------

def test_zero_events_is_a_count_and_a_missing_key_is_not() -> None:
    """THE DISTINCTION THIS WHOLE LAYER RESTS ON.

    An empty `features` list is an agency saying its roads are clear, which is
    data. A body with no `features` key has said nothing and may not be WZDx
    at all. `heartbeats present, no aircraft` versus `no heartbeats` is the
    same distinction, and collapsing it here would let a misconfigured
    endpoint read as a quiet highway."""
    assert event_count(json.loads(wzdx(n=0))) == 0
    assert event_count(json.loads(wzdx(n=7))) == 7
    assert event_count({"feed_info": {"version": "4.2"}}) is None
    assert event_count("<html>") is None


def test_an_empty_feed_and_a_dead_feed_do_not_share_a_footprint() -> None:
    clear = feed_record(FEED, status=200, body=wzdx(n=0), elapsed_s=0.2)
    blind = feed_record(FEED, status=None, body=None, elapsed_s=30.0,
                        error="TimeoutError: timed out")
    assert (clear["returned"], clear["outcome"], clear["n_events"]) == \
           (True, OK, 0)
    assert (blind["returned"], blind["outcome"], blind["n_events"]) == \
           (False, HTTP_ERROR, None)


# -- classification --------------------------------------------------------

def test_html_served_with_a_200_is_not_a_feed() -> None:
    """Maryland's CHART answered 200 with a web page three times. An error
    page has a length and parses as nothing, and averaging it into a size
    estimate makes the estimate wrong in the comfortable direction."""
    page = b"<!DOCTYPE html>\n<html><body>Service Unavailable</body></html>"
    assert looks_like_html(page)
    assert classify(200, page, None) == NOT_WZDX
    assert not looks_like_html(wzdx())


@pytest.mark.parametrize("status,body,declared,expected", [
    (200, wzdx("4.2"), "4.2", OK),
    (200, wzdx("4", n=0), "4", OK),
    (200, wzdx("9.9"), "9.9", VERSION_UNKNOWN),
    (200, b"this is not json", None, NOT_JSON),
    (200, b'{"ok":true}', None, NOT_WZDX),
    (404, b'{"error":"gone"}', None, HTTP_ERROR),
    (500, b"", None, HTTP_ERROR),
    (None, None, None, HTTP_ERROR),
])
def test_one_outcome_name_per_distinguishable_fact(
        status, body, declared, expected) -> None:
    assert classify(status, body, declared) == expected


def test_a_feed_on_an_unread_specification_is_recorded_not_counted() -> None:
    """9.9 is not in KNOWN_WZDX_VERSIONS, so its events are not summed into a
    total that would then mix two schemas. The body is still kept: the first
    thing anyone wants when a feed changes spec is the body from before."""
    assert "9.9" not in KNOWN_WZDX_VERSIONS
    rec = feed_record(FEED, status=200, body=wzdx("9.9", n=4), elapsed_s=0.1)
    assert rec["outcome"] == VERSION_UNKNOWN
    assert rec["n_events"] is None, "events on an unread spec are not counted"
    assert rec["returned"] is True, "it answered; that is a separate fact"
    assert rec["sha256"], "and the body is identified so it can be stored"


# -- storage ---------------------------------------------------------------

def test_two_bodies_in_one_second_cannot_overwrite_each_other(tmp_path) -> None:
    """THE DEFECT OF 2026-10-06.

    Filenames were `{feed}_{timestamp}.json.gz` to the second, which felt like
    enough. A round-trip check failed on its first run: three stores inside
    one second, and the third silently overwrote the first, so the archive
    held the newest body under the oldest body's name.

    That is the 2026-10-02 `probe-<date>.json` decision one directory over --
    and raising a resolution because the collision you met was at the previous
    one is mechanism N. The digest is in the name now, so the collision cannot
    occur rather than being made less likely."""
    b1, b2 = wzdx(n=1), wzdx(n=2)
    s1 = store_body(tmp_path, "mcdot", b1, digest(b1))
    s2 = store_body(tmp_path, "mcdot", b2, digest(b2))
    assert s1["path"] != s2["path"]
    assert gzip.decompress(s1["path"].read_bytes()) == b1
    assert gzip.decompress(s2["path"].read_bytes()) == b2


def test_an_unchanged_body_is_not_stored_again(tmp_path) -> None:
    """A work zone lives for weeks. Storing it on every cycle would write the
    same lane closure 144 times a day."""
    b = wzdx()
    first = store_body(tmp_path, "mcdot", b, digest(b))
    again = store_body(tmp_path, "mcdot", b, digest(b))
    assert first["digest_is_new"] and first["bytes_stored"] > 0
    assert not again["digest_is_new"]
    assert again["bytes_stored"] == 0 and again["path"] is None


def test_a_changed_feed_and_a_written_file_are_separate_facts(tmp_path) -> None:
    """THE SECOND DEFECT OF 2026-10-06.

    `store_body` returned `(path, bytes)` and the caller read `path is not
    None` as "the feed changed". They come apart: a feed reverting to a body
    already on disk under this second's name has genuinely changed while
    needing no write. The caller recorded it as unchanged AND left the sidecar
    naming the wrong digest.

    One variable holding whichever of two quantities is convenient is the
    shape that published `n_polls` as a file count against 19,329 attempted
    polls, understated 11.7x, for three days."""
    b1, b2 = wzdx(n=1), wzdx(n=2)
    store_body(tmp_path, "mcdot", b1, digest(b1))
    store_body(tmp_path, "mcdot", b2, digest(b2))
    revert = store_body(tmp_path, "mcdot", b1, digest(b1))

    assert revert["digest_is_new"] is True, "the feed did change"
    assert revert["bytes_stored"] == 0, "and needed no write"
    assert revert["path"] is not None, "and the bytes are at a known path"
    assert last_digest(tmp_path, "mcdot") == digest(b1), (
        "the sidecar records what we last SAW, which is true whether or not a "
        "write was needed")


def test_the_stored_body_is_the_body_that_arrived(tmp_path) -> None:
    """The cheapest integrity check there is, and it found the overwrite bug
    on its first run. Compression that lost or altered a byte would be a
    storage win paid for in data."""
    b = wzdx(n=40)
    s = store_body(tmp_path, "mcdot", b, digest(b))
    assert gzip.decompress(s["path"].read_bytes()) == b
    assert json.loads(gzip.decompress(s["path"].read_bytes()))["features"]


def test_the_path_is_partitioned_by_hour_and_names_its_digest(tmp_path) -> None:
    from datetime import datetime, timezone
    when = datetime(2026, 10, 6, 14, 5, 9, tzinfo=timezone.utc)
    b = wzdx()
    p = body_path(tmp_path, "mcdot", when, digest(b))
    assert p.parent.name == "hour=2026100614"
    assert digest(b)[:12] in p.name
    assert p.name.endswith(".json.gz")


def test_the_ledger_records_every_attempt_including_the_failures(tmp_path) -> None:
    """The file that lets a blind feed be told from a clear road. One line per
    feed per cycle, append-only and fsynced for the reason uptime.py gives:
    the cycles immediately before a crash are the ones needed to explain the
    gap, and a buffered writer loses them."""
    recs = [feed_record(FEED, status=200, body=wzdx(n=3), elapsed_s=0.2),
            feed_record(dict(FEED, feedName="other"), status=None, body=None,
                        elapsed_s=30.0, error="timeout")]
    path = append_ledger(tmp_path, recs)
    lines = [json.loads(ln) for ln in
             path.read_text(encoding="utf-8").strip().splitlines()]
    assert len(lines) == 2
    assert [ln["returned"] for ln in lines] == [True, False]
    assert path.name.startswith("attempts-")


def test_the_ledger_appends_rather_than_replaces(tmp_path) -> None:
    rec = [feed_record(FEED, status=200, body=wzdx(), elapsed_s=0.1)]
    append_ledger(tmp_path, rec)
    path = append_ledger(tmp_path, rec)
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2


# -- the cycle -------------------------------------------------------------

def row(feed: str, outcome: str = OK, returned: bool = True,
        n: int | None = 5, verdict: str = "agrees") -> dict:
    return {"feed": feed, "outcome": outcome, "returned": returned,
            "n_events": n, "version_verdict": verdict}


def test_counts_are_named_for_what_they_hold() -> None:
    s = cycle_summary([row("a"), row("b"),
                       row("c", HTTP_ERROR, False, None)], {"a"})
    assert s["n_feeds_attempted"] == 3
    assert s["n_feeds_returned"] == 2
    assert s["n_feeds_counted"] == 2
    assert s["n_feeds_changed"] == 1
    assert s["n_events_total"] == 10
    assert s["feeds_failed"] == ["c"]


def test_six_cycle_shapes_produce_six_distinct_sentences() -> None:
    """Rule 13. The count is what catches a missing branch; a swallowed case
    reads as perfectly correct on its own."""
    shapes = [
        cycle_summary([], set()),
        cycle_summary([row(f, HTTP_ERROR, False, None) for f in "abc"], set()),
        cycle_summary([row("a"), row("b"),
                       row("c", HTTP_ERROR, False, None)], {"a"}),
        cycle_summary([row(f) for f in "abc"], set()),
        cycle_summary([row(f) for f in "abc"], {"a", "b", "c"}),
        cycle_summary([row(f) for f in "abcd"], {"a"}),
    ]
    said = [cycle_sentence(s) for s in shapes]
    assert len(set(said)) == 6, f"not all distinct: {said}"


def test_the_blind_count_agrees_with_itself_grammatically() -> None:
    """These sentences are the lab notebook -- they are what gets quoted into
    the worklog instead of the archive -- so they are held to prose."""
    one = cycle_summary([row("a"), row("b", HTTP_ERROR, False, None)], {"a"})
    two = cycle_summary([row("a")] + [row(f, HTTP_ERROR, False, None)
                                      for f in "bc"], {"a"})
    assert "1 was blind" in cycle_sentence(one)
    assert "is named in the ledger" in cycle_sentence(one)
    assert "2 were blind" in cycle_sentence(two)
    assert "are named in the ledger" in cycle_sentence(two)


def test_a_healthy_cycle_raises_no_flags() -> None:
    """Rule 16. A flag's currency is the reader's attention, and one that
    fires on an ordinary cycle spends what the next real one will need."""
    s = cycle_summary([row(f) for f in "abcdefgh"], {"a", "b"})
    assert s["flags"] == []


def test_zero_work_zones_everywhere_is_not_flagged() -> None:
    """Every agency reporting clear roads at 4 a.m. is plausible and is a
    finding. Flagging it would train the reader to skip the list."""
    s = cycle_summary([row(f, n=0) for f in "abcde"], set())
    assert s["n_events_total"] == 0
    assert s["n_feeds_counted"] == 5
    assert s["flags"] == []


def test_every_feed_changing_at_once_is_flagged() -> None:
    """Twenty-five independent agencies do not refresh in lockstep. That is a
    generation timestamp in the body, and it would make every cycle look like
    new information and defeat the change-only storage entirely -- the
    resolution-floor problem from the transit cadence work, one layer up."""
    s = cycle_summary([row(f) for f in "abcde"], set("abcde"))
    assert any("NEARLY EVERY FEED CHANGED" in f for f in s["flags"])


def test_nothing_returning_is_an_outage_and_not_a_measurement() -> None:
    s = cycle_summary([row(f, HTTP_ERROR, False, None) for f in "abc"], set())
    assert any("NOTHING RETURNED" in f for f in s["flags"])
    assert cycle_health(s) == (False, "all 3 feeds failed: a, b, c")


def test_no_feeds_attempted_is_its_own_fault() -> None:
    """An empty cycle on disk looks exactly like a cycle in which nothing was
    wrong. Same shape as a glob that matched no files passing a completeness
    check."""
    s = cycle_summary([], set())
    assert any("NO FEEDS ATTEMPTED" in f for f in s["flags"])
    assert cycle_health(s)[0] is False


def test_feeds_answering_but_none_parsing_is_distinguished() -> None:
    """25 feeds returning HTML with a 200 would give n_events_total == 0, and
    reading that as empty roads is how three CHART endpoints nearly became a
    finding."""
    s = cycle_summary([row(f, NOT_WZDX, True, None) for f in "abc"], set())
    assert s["n_feeds_returned"] == 3
    assert s["n_feeds_counted"] == 0
    assert any("NOTHING COUNTABLE" in f for f in s["flags"])


def test_a_version_disagreement_names_the_feed() -> None:
    s = cycle_summary([row("a"), row("b", verdict="disagrees")], {"a"})
    assert s["version_disagreements"] == ["b"]
    assert any("VERSION DISAGREEMENT" in f for f in s["flags"])


def test_a_feed_declaring_no_version_is_listed_but_not_flagged() -> None:
    """Recorded, because the registry's claim must not be substituted for the
    body's silence. Not flagged, because it is common and harmless and rule 16
    applies."""
    s = cycle_summary([row("a"), row("b", verdict="absent")], {"a"})
    assert s["feeds_without_declared_version"] == ["b"]
    assert not any("VERSION" in f for f in s["flags"])


def test_one_feed_failing_is_their_silence_and_not_our_outage() -> None:
    """The heartbeat answers "were WE looking". One refusal belongs in the
    ledger; every refusal is almost certainly our network, and the air
    domain's 2,621 `getaddrinfo failed` polls are what that looks like
    recorded properly."""
    s = cycle_summary([row("a"), row("b", HTTP_ERROR, False, None)], {"a"})
    assert cycle_health(s) == (True, None)


def test_flags_are_computed_once_and_stored_on_the_summary() -> None:
    s = cycle_summary([row(f) for f in "abc"], set("abc"))
    assert s["flags"] == cycle_flags(s)


# -- the window, which is what G6 will ask about ---------------------------

def test_a_feed_that_never_answered_is_reported_as_blind() -> None:
    """The question `road_discrepancy.py` must ask before reading an absence
    of work zones as an absence of work zones. Mechanism J: "missing" that
    means "missing where I looked"."""
    rows = [row("a"), row("b", HTTP_ERROR, False, None),
            row("a"), row("b", HTTP_ERROR, False, None),
            row("a"), row("b", HTTP_ERROR, False, None)]
    assert blind_feeds(rows) == {"b": 3}


def test_a_feed_that_answered_once_is_not_blind() -> None:
    """Partially blind is a different fact from blind, and it is the ledger's
    row count that quantifies it rather than this function."""
    rows = [row("b", HTTP_ERROR, False, None), row("b"),
            row("b", HTTP_ERROR, False, None)]
    assert blind_feeds(rows) == {}


def test_a_feed_reporting_zero_events_is_never_blind() -> None:
    """The entire point. An agency saying "my roads are clear" has observed
    for us, and must not be filtered out alongside the agency that said
    nothing at all."""
    assert blind_feeds([row("a", n=0), row("a", n=0)]) == {}


# -- the registry selection ------------------------------------------------

def test_a_feed_name_that_matches_nothing_is_an_error_not_an_empty_run() -> None:
    """An empty run writes a cycle of zero attempts, which on disk is
    indistinguishable from a cycle in which nothing was wrong."""
    art = {"selected": [FEED]}
    with pytest.raises(SystemExit) as exc:
        feeds_from(art, ("nosuchfeed",))
    assert "mcdot" in str(exc.value), "the message lists what IS available"


def test_narrowing_by_name_is_case_insensitive_and_returns_the_row() -> None:
    art = {"selected": [FEED, dict(FEED, feedName="other")]}
    picked = feeds_from(art, ("MCDOT",))
    assert [p["feedName"] for p in picked] == ["mcdot"]


def test_no_narrowing_returns_every_selected_feed() -> None:
    art = {"selected": [FEED, dict(FEED, feedName="other")]}
    assert len(feeds_from(art, ())) == 2


def test_the_real_registry_selects_twenty_five_keyless_feeds() -> None:
    """Read from the committed artefact rather than asserted from a summary.
    G0 read 43 rows and selected 25; the other 18 are 13 needing a key, 2
    disagreeing with themselves, 2 on an unread spec and 1 inactive.

    A summary is a pointer to a source, never a substitute for it -- rule 20,
    and the reason this project once believed in 22 keyless feeds when the CSV
    says 30."""
    from pathlib import Path
    reg = Path(__file__).resolve().parents[1] / "data" / "reference" / \
        "roads" / "wzdx-registry.json"
    if not reg.exists():                         # pragma: no cover
        pytest.skip("registry not fetched in this checkout")
    art = json.loads(reg.read_text(encoding="utf-8"))
    assert art["rows_read"] == 43
    assert art["n_selected"] == 25
    assert len(art["selected"]) == 25
    assert sum(art["excluded_by_rule"].values()) == 18
    for row_ in art["selected"]:
        assert row_["version"] in KNOWN_WZDX_VERSIONS
        assert row_["url"].startswith("http")


def test_every_selected_feed_has_the_fields_g1_reads() -> None:
    """A KeyError on cycle 400 of a multi-day run would be found by its
    absence from the archive, which is the worst way to find anything."""
    from pathlib import Path
    reg = Path(__file__).resolve().parents[1] / "data" / "reference" / \
        "roads" / "wzdx-registry.json"
    if not reg.exists():                         # pragma: no cover
        pytest.skip("registry not fetched in this checkout")
    art = json.loads(reg.read_text(encoding="utf-8"))
    for row_ in art["selected"]:
        rec = feed_record(row_, status=None, body=None, elapsed_s=0.0,
                          error="not fetched")
        assert rec["feed"] and rec["version_registry"]
        assert rec["returned"] is False
