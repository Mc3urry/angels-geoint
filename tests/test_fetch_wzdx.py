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

from scripts.fetch_wzdx import (HTTP_ERROR, KNOWN_SPEC_VERSIONS,
                                KNOWN_WZDX_VERSIONS, NOT_JSON, NOT_WZDX, OK,
                                STORED_OUTCOMES, TRUNCATED, VERSION_UNKNOWN,
                                append_ledger, blind_feeds, body_path,
                                canonical_version, classify, content_digest,
                                cycle_flags, cycle_health, cycle_sentence,
                                cycle_summary, decorative_update_date,
                                declared_version, digest, event_count,
                                feed_record, feeds_from, last_digest,
                                looks_like_html, store_body, version_verdict)

FEED = {"feedName": "mcdot", "issuingOrganization": "Maricopa County DOT",
        "state": "arizona", "version": "4.2",
        "url": "https://wzdxapi.aztech.org/construction"}


def wzdx(version: str = "4.2", n: int = 1, key: str = "feed_info",
         update: str = "2026-10-06T19:11:47Z",
         ids: list[str] | None = None) -> bytes:
    names = ids if ids is not None else [str(i) for i in range(n)]
    return json.dumps({
        key: {"version": version, "publisher": "x", "update_date": update},
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "id": i} for i in names],
    }).encode()


def store(root, feed: str, body: bytes, when=None) -> dict:
    """Store the way the collector does, with both digests.

    A test that passed only the body digest would exercise the fallback and
    quietly stop testing the path that runs in production -- which is how a
    guard comes to be green over code nobody is checking."""
    return store_body(root, feed, body, digest(body),
                      content_digest(json.loads(body)), when)


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
    ("4.0", "4", "agrees-by-alias"),
    ("4.1", "4", "disagrees"),
    ("3.1", "4", "disagrees"),
    (None, "4.2", "absent"),
    ("4.2", None, "registry-silent"),
])
def test_the_version_check_has_five_outcomes_not_two(
        declared, registry, expected) -> None:
    """`absent` is not `agrees`. A feed that declares nothing has told us
    nothing, and substituting the registry's claim for its silence would make
    a catalogue row look like a measurement -- mechanism D, and the reason
    Oklahoma and Florida were excluded by G0 in the first place."""
    assert version_verdict(declared, registry) == expected


def test_the_five_verdicts_are_five_distinct_strings() -> None:
    """Rule 13 applied to a verdict rather than a sentence: a swallowed branch
    returning a neighbour's value looks right in isolation."""
    said = {version_verdict(d, r) for d, r in
            (("4.2", "4.2"), ("4.0", "4"), ("4.1", "4"),
             (None, "4.2"), ("4.2", None))}
    assert len(said) == 5


# -- the two vocabularies, bought with five states -------------------------

def test_the_registry_shorthand_and_the_spec_notation_are_one_spec() -> None:
    """THE CORRECTION OF 2026-10-06, FOUND BY THE FIRST LIVE CYCLE.

    Five feeds -- Kansas, North Dakota, Utah, Illinois and Indiana -- declare
    `"4.0"` in their bodies against a registry row saying `"4"`. One
    specification, two notations: the registry writes the shorthand and the
    spec writes the number.

    String comparison rejected all five, and the rejection looked principled,
    because this project has three separate entries about never coercing a
    version to a float. The guard was right about the coercion and wrong about
    the vocabulary: `KNOWN_WZDX_VERSIONS` was built from the catalogue rather
    than from the thing the catalogue describes.

    Its effect was not conservative. About 9.2 MB of real work zone data
    across five states dropped out of the counted layer -- five states
    quietly declaring nothing, in the direction that flatters the hypothesis.
    Mechanism S: wrong in the safe direction, which is not safe.
    """
    assert canonical_version("4") == "4.0"
    assert canonical_version("4.0") == "4.0"
    assert "4.0" in KNOWN_SPEC_VERSIONS
    for v in ("4.0", "4", "4.1", "4.2"):
        assert canonical_version(v) in KNOWN_SPEC_VERSIONS, v
    assert classify(200, wzdx("4.0"), "4.0") == OK, (
        "a feed on WZDx 4.0 is countable; it was not, for five feeds, for as "
        "long as this file took to run once against the real internet")


def test_canonicalising_is_translation_and_not_coercion() -> None:
    """The float trap stays shut. `4.10` is not `4.1`, and an alias table that
    quietly became a numeric comparison would reopen it."""
    assert canonical_version("4.10") == "4.10"
    assert "4.10" not in KNOWN_SPEC_VERSIONS
    assert classify(200, wzdx("4.10"), "4.10") == VERSION_UNKNOWN
    assert version_verdict("4.10", "4.1") == "disagrees"


def test_the_spec_versions_are_derived_from_the_registry_tuple() -> None:
    """Not restated. `fetch_road_registries.KNOWN_WZDX_VERSIONS` is what G0
    selects on, so a version added there must appear here without anyone
    remembering to -- a fix landing on one side of a pair is mechanism E."""
    assert len(KNOWN_SPEC_VERSIONS) == len(set(KNOWN_WZDX_VERSIONS))
    for v in KNOWN_WZDX_VERSIONS:
        assert canonical_version(v) in KNOWN_SPEC_VERSIONS


def test_a_notation_difference_is_recorded_but_not_flagged() -> None:
    """Rule 16, and it was earned. The first live cycle flagged six version
    disagreements, five of which were this notation. St Charles County's was
    real -- a body on 4.1 against a registry claiming 4 -- and it sat fifth in
    a list of six, which is how a true alarm gets skipped."""
    rows = [row("kansas", verdict="agrees-by-alias"),
            row("stcharlesco_v4", verdict="disagrees")]
    s = cycle_summary(rows, {"kansas"})
    assert s["version_notation_differs"] == ["kansas"]
    assert s["version_disagreements"] == ["stcharlesco_v4"]
    assert sum("VERSION DISAGREEMENT" in f for f in s["flags"]) == 1
    assert "kansas" not in " ".join(s["flags"])


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
    (200, wzdx("4.0", n=0), "4.0", OK),
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


# -- our ceiling is not their fault ----------------------------------------

def test_a_body_cut_off_by_our_cap_is_not_called_bad_json() -> None:
    """THE OTHER CORRECTION OF 2026-10-06.

    North Carolina and Wisconsin both returned exactly 8,388,608 bytes on the
    first live cycle -- the cap, to the byte -- so their bodies arrived chopped
    mid-object, failed to parse, and were filed as `not_json`. That is a claim
    about their data when the only fact in evidence was about our ceiling.

    `truncated` is now checked before anything else, because a truncated body
    tells us nothing about whether the feed is valid JSON, valid WZDx, or on a
    known specification. Answering any of those questions from a prefix is
    reading at a scale that cannot show the thing."""
    whole = wzdx("4.2", n=5)
    assert classify(200, whole, "4.2", False) == OK
    assert classify(200, whole, "4.2", True) == TRUNCATED
    assert classify(200, b'{"features":[{"id":"a"', None, True) == TRUNCATED
    assert classify(200, b"genuinely not json", None, False) == NOT_JSON


def test_a_truncated_body_is_never_stored() -> None:
    """A chopped body written under a digest of its chopped bytes is a partial
    answer wearing a whole answer's name -- the 483 polygons again. On the
    first live cycle the archive was spared by luck rather than design:
    `not_json` happened not to be in the stored set."""
    assert TRUNCATED not in STORED_OUTCOMES
    assert OK in STORED_OUTCOMES and VERSION_UNKNOWN in STORED_OUTCOMES


def test_a_truncated_feed_is_not_counted_and_is_flagged_as_ours() -> None:
    rec = feed_record(FEED, status=200, body=wzdx(n=9), elapsed_s=1.0,
                      truncated=True)
    assert rec["outcome"] == TRUNCATED
    assert rec["n_events"] is None, "a prefix is not an event count"
    assert rec["returned"] is True, "it answered; that is a separate fact"

    s = cycle_summary([row("ncdot", TRUNCATED, True, None), row("b")], {"b"})
    assert s["feeds_truncated"] == ["ncdot"]
    flag = " ".join(s["flags"])
    assert "TRUNCATED BY OUR OWN CAP" in flag
    assert "our ceiling and not their fault" in flag


def test_a_run_with_no_truncation_is_not_flagged() -> None:
    """Rule 16."""
    s = cycle_summary([row(f) for f in "abc"], {"a"})
    assert s["feeds_truncated"] == []
    assert not any("TRUNCATED" in f for f in s["flags"])


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
    s1 = store(tmp_path, "mcdot", b1)
    s2 = store(tmp_path, "mcdot", b2)
    assert s1["path"] != s2["path"]
    assert gzip.decompress(s1["path"].read_bytes()) == b1
    assert gzip.decompress(s2["path"].read_bytes()) == b2


def test_an_unchanged_body_is_not_stored_again(tmp_path) -> None:
    """A work zone lives for weeks. Storing it on every cycle would write the
    same lane closure 144 times a day."""
    b = wzdx()
    first = store(tmp_path, "mcdot", b)
    again = store(tmp_path, "mcdot", b)
    assert first["digest_is_new"] and first["bytes_stored"] > 0
    assert not again["digest_is_new"]
    assert again["bytes_stored"] == 0 and again["path"] is None
    assert not again["envelope_only"], (
        "identical bytes are not an envelope change either")


# -- the envelope moves and no road does -----------------------------------

def test_a_moving_update_date_over_identical_events_is_not_a_change() -> None:
    """THE MEASUREMENT OF 2026-10-06, FROM THE ONLY TWO CYCLES THIS COLLECTOR
    HAS EVER RUN, 15.6 MINUTES APART.

    Eleven of twenty-three feeds changed their bytes. Five changed nothing
    about any road: iddot, modot, msdot and stcharlesco_v4 advanced
    `feed_info.update_date` over byte-identical features, and necdot returned
    the same 182 events in a different order. 581,926 bytes -- 36 per cent of
    the recurring traffic -- for bodies carrying no new claim.

    The storage was the smaller problem. A whole-body digest makes `changed`
    mean "the bytes moved", when the quantity this layer exists to track is
    "the agency's declaration about a road moved". A timestamp rewritten on
    every request answers that question `always`, which is the WMATA header
    timestamp exactly -- advancing every poll while the fleet sat still -- one
    domain over.
    """
    a = wzdx(n=3, update="2026-10-06T19:11:47Z")
    b = wzdx(n=3, update="2026-10-06T19:27:07Z")
    assert a != b, "the bodies differ"
    assert digest(a) != digest(b), "and so do their whole-body digests"
    assert content_digest(json.loads(a)) == content_digest(json.loads(b)), (
        "but no road event changed, so the content digest must not move")


def test_reordered_features_are_the_same_claim() -> None:
    """necdot returned its 182 events shuffled. A FeatureCollection is a set;
    a different sequence is not news, and treating it as one would report
    churn forever on a feed whose backend does not promise an order."""
    a = wzdx(ids=["a", "b", "c"])
    b = wzdx(ids=["c", "a", "b"])
    assert digest(a) != digest(b)
    assert content_digest(json.loads(a)) == content_digest(json.loads(b))


def test_a_real_event_change_does_move_the_content_digest() -> None:
    """The other half, and the half that would be catastrophic to lose. dedot
    gained 8 events and njdot lost 60 in that same window."""
    assert content_digest(json.loads(wzdx(n=3))) != \
        content_digest(json.loads(wzdx(n=4)))
    one = json.loads(wzdx(ids=["a"]))
    two = json.loads(wzdx(ids=["a"]))
    two["features"][0]["properties"] = {"lanes": "closed"}
    assert content_digest(one) != content_digest(two), (
        "a changed attribute on an unchanged id is a changed claim")


def test_duplicate_ids_do_not_make_the_digest_unstable() -> None:
    """Sorted by (id, canonical form) rather than id alone. Two features
    under one id would be a real defect in somebody's feed, and it must not
    also make this digest flip between orderings and report churn forever."""
    a = {"features": [{"id": "a", "x": 1}, {"id": "a", "x": 2}]}
    b = {"features": [{"id": "a", "x": 2}, {"id": "a", "x": 1}]}
    assert content_digest(a) == content_digest(b)


def test_no_features_has_no_content_digest() -> None:
    """None, not the digest of an empty list. "not WZDx" and "a feed with no
    work zones" are different facts and an empty-list digest would be a real
    value standing in for an absent one."""
    assert content_digest({"feed_info": {"version": "4.2"}}) is None
    assert content_digest("<html>") is None
    assert content_digest(json.loads(wzdx(n=0))) is not None, (
        "an empty features list IS a claim and does have a digest")


def test_an_envelope_change_is_recorded_and_not_stored(tmp_path) -> None:
    """The ledger gets the observation; the archive does not get the body.
    Four feeds did this between the only two cycles so far, and the fact that
    their `update_date` moves is worth keeping even though the bytes are
    not."""
    a = wzdx(n=3, update="2026-10-06T19:11:47Z")
    b = wzdx(n=3, update="2026-10-06T19:27:07Z")
    first = store(tmp_path, "modot", a)
    second = store(tmp_path, "modot", b)

    assert first["digest_is_new"] and first["bytes_stored"] > 0
    assert second["envelope_only"] is True
    assert second["digest_is_new"] is False
    assert second["bytes_stored"] == 0
    bodies = list((tmp_path / "roads" / "wzdx").rglob("*.json.gz"))
    assert len(bodies) == 1, "one body for one unchanged set of road events"
    assert last_digest(tmp_path, "modot") == content_digest(json.loads(b))


def test_an_envelope_only_cycle_is_stated_but_not_flagged() -> None:
    """Rule 16, and this one would have fired on every cycle for four feeds.
    It belongs in the sentence as information, not in the flag list as an
    alarm."""
    rows = [row(f) for f in "abcdef"]
    s = cycle_summary(rows, {"a"}, set(), {"b", "c", "d", "e"})
    assert s["n_feeds_envelope_only"] == 4
    assert s["feeds_envelope_only"] == ["b", "c", "d", "e"]
    assert s["flags"] == []
    assert "moved their update_date over identical road events" in \
        cycle_sentence(s)


def test_a_feed_whose_update_date_is_decorative_is_named_across_cycles() -> None:
    """A cross-cycle question, so it is a function over the ledger rather than
    a per-cycle flag: one envelope-only cycle is ordinary, and a feed that has
    done nothing else all day is telling us its freshness claim is furniture.

    This is the finding that matters beyond storage. If G4 or G6 ever reads
    `feed_info.update_date` as "how current is this declaration", these feeds
    answer "seconds old" and mean nothing by it -- an unverified self-report
    about the self-report's own freshness, which is this project's whole
    subject one level up."""
    rows = ([{"feed": "modot", "envelope_only": True} for _ in range(4)]
            + [{"feed": "mcdot", "envelope_only": True}]
            + [{"feed": "mcdot", "stored_as": "x.json.gz"}]
            + [{"feed": "njdot", "stored_as": "y.json.gz"} for _ in range(3)])
    assert decorative_update_date(rows) == {"modot": 4}, (
        "mcdot had one real change so its update_date is earning its place; "
        "njdot never moved an envelope at all")


def test_one_envelope_only_cycle_is_not_enough_to_call_it_decorative() -> None:
    rows = [{"feed": "modot", "envelope_only": True}]
    assert decorative_update_date(rows) == {}


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
    store(tmp_path, "mcdot", b1)
    store(tmp_path, "mcdot", b2)
    revert = store(tmp_path, "mcdot", b1)

    assert revert["digest_is_new"] is True, "the feed did change"
    assert revert["bytes_stored"] == 0, "and needed no write"
    assert revert["path"] is not None, "and the bytes are at a known path"
    assert last_digest(tmp_path, "mcdot") == content_digest(json.loads(b1)), (
        "the sidecar records what we last SAW, which is true whether or not a "
        "write was needed")


def test_the_stored_body_is_the_body_that_arrived(tmp_path) -> None:
    """The cheapest integrity check there is, and it found the overwrite bug
    on its first run. Compression that lost or altered a byte would be a
    storage win paid for in data."""
    b = wzdx(n=40)
    s = store(tmp_path, "mcdot", b)
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


def test_the_event_yield_names_the_feeds_it_came_from() -> None:
    """THE THIRD CORRECTION OF 2026-10-06.

    The first live cycle printed "23 of 25 feeds answered with 9225 events".
    Every number in it was correct and the sentence was not: the 9,225 came
    from 15 feeds, not 23. Eight answered and were not countable -- two
    truncated, two unparseable, one not WZDx, three on a notation the guard
    rejected -- so the reader got a yield attached to the wrong denominator,
    and `n_feeds_counted` was sitting in the summary unused.

    Mechanism U: a correct number under a shape that answers a question nobody
    asked. The denominator of a rate appears beside it.
    """
    mixed = ([row(f"ok{i}", n=100) for i in range(15)]
             + [row(f"bad{i}", NOT_WZDX, True, None) for i in range(8)]
             + [row(f"down{i}", HTTP_ERROR, False, None) for i in range(2)])
    s = cycle_summary(mixed, {f"ok{i}" for i in range(15)})
    assert (s["n_feeds_returned"], s["n_feeds_counted"]) == (23, 15)
    said = cycle_sentence(s)
    assert "1500 events from 15 countable" in said
    assert "23 of 25 feeds answered" in said


def test_the_yield_is_stated_plainly_when_every_answer_counted() -> None:
    """The clause only earns its place when the two numbers differ. Printing
    "15 events from 3 countable" on every ordinary cycle would be noise of the
    same kind as a flag that always fires."""
    s = cycle_summary([row(f) for f in "abc"], {"a"})
    assert "15 events," in cycle_sentence(s)
    assert "countable" not in cycle_sentence(s)


def test_uncountable_feeds_are_named_and_not_merely_counted() -> None:
    """"Which eight?" is the next question anyone asks, and the summary has to
    answer it without going back to the ledger."""
    s = cycle_summary([row("a"), row("nysdot", NOT_WZDX, True, None),
                       row("ncdot", TRUNCATED, True, None)], {"a"})
    assert s["feeds_uncountable"] == ["ncdot", "nysdot"]


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


def test_every_feed_refreshing_at_once_is_flagged() -> None:
    """Twenty-five independent agencies do not refresh in lockstep. That is a
    generation timestamp in the body, and it would make every cycle look like
    new information and defeat the change-only storage entirely -- the
    resolution-floor problem from the transit cadence work, one layer up."""
    s = cycle_summary([row(f) for f in "abcde"], set("abcde"), set())
    assert any("NEARLY EVERY FEED REFRESHED" in f for f in s["flags"])


def test_an_empty_archive_is_not_twenty_five_agencies_in_lockstep() -> None:
    """THE FOURTH CORRECTION OF 2026-10-06.

    On the first cycle against an empty archive, every feed's body is new by
    definition. The lockstep flag compared changes against answers, so it
    would have fired and accused the agencies of refreshing in unison on the
    strength of the instrument's own initial condition.

    It did not fire on the real run -- 20 of 23 is below the threshold -- but
    only because three unrelated feeds were uncountable. A guard that is
    correct by accident has not been tested, which is what mechanism Z said
    about a convention that had been right eleven times running.
    """
    feeds = set("abcde")
    s = cycle_summary([row(f) for f in feeds], feeds, first_seen=feeds)
    assert s["n_feeds_first_seen"] == 5
    assert s["n_feeds_refreshed"] == 0
    assert not any("REFRESHED" in f for f in s["flags"])
    assert "stored for the first time" in cycle_sentence(s)


def test_a_mixed_cycle_separates_refreshes_from_first_sightings() -> None:
    """One new feed added to a running collector must not drag the whole
    cycle's reading with it."""
    s = cycle_summary([row(f) for f in "abcde"], {"a", "b", "c"},
                      first_seen={"c"})
    assert (s["n_feeds_changed"], s["n_feeds_first_seen"],
            s["n_feeds_refreshed"]) == (3, 1, 2)
    assert "3 changed, 1 of them a first sighting" in cycle_sentence(s)


def test_a_first_sighting_is_reported_by_the_writer() -> None:
    """The fact has to come from the archive, not be inferred by the caller
    from a path being non-None -- which is how `digest_is_new` and
    `bytes_stored` came to be one variable in the first place."""
    import tempfile
    from pathlib import Path as _P
    with tempfile.TemporaryDirectory() as d:
        root, b1, b2 = _P(d), wzdx(n=1), wzdx(n=2)
        first = store(root, "mcdot", b1)
        later = store(root, "mcdot", b2)
        assert first["first_sighting"] is True
        assert later["first_sighting"] is False
        assert later["digest_is_new"] is True


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
