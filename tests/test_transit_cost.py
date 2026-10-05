"""The one-feed cost measurement. No network, no clock, no sleeping.

Everything here exercises `summarise_polls` and `verdict`, which are pure.
The polling loop itself is not tested: it is a `urlopen` in a `for`, and a
test of it would be a test of a mock.

WHAT THE MEASUREMENT IS FOR

189 keyless feeds plus 2 credentialed, at a 30-second poll, is 550,080
requests a day. The only size estimate the project had was a guess of about
50 kB a protobuf, and a plan resting on a factor-of-ten guess is not a plan.

The second question matters as much: polling every 30 seconds a feed that
refreshes every 60 buys nothing but bytes, and polling every 30 a feed that
refreshes every 10 loses two thirds of the movement. Both errors are
invisible afterwards -- the first looks like a dense record, the second like
a sparse world -- so the cadence is measured from digest changes rather than
assumed from the feed's own header.
"""

from __future__ import annotations

import pytest

from scripts.probe_transit_feed import (INTERVALS_TO_PROJECT, POLLABLE_FEEDS,
                                        summarise_polls, verdict)


def poll(sha: str, size: int = 50_000, ok: bool = True,
         binary: bool = True) -> dict:
    if not ok:
        return {"ok": False, "status": 503, "reason": "nope"}
    return {"ok": True, "status": 200, "bytes": size, "sha256": sha,
            "elapsed_s": 0.1, "looks_binary": binary, "truncated": False}


# -- sizes ------------------------------------------------------------------

def test_sizes_are_reported_as_a_distribution_not_a_number() -> None:
    """A median alone hides a feed that is tiny at 3 a.m. and large at
    rush hour, which is exactly the shape a daily projection gets wrong."""
    s = summarise_polls([poll("a", 10), poll("b", 20), poll("c", 30)], 30)
    assert s["bytes_min"] == 10 and s["bytes_median"] == 20
    assert s["bytes_max"] == 30 and s["bytes_mean"] == 20


def test_an_exact_zero_is_flagged_rather_than_averaged() -> None:
    s = summarise_polls([poll("a", 0), poll("b", 100)], 30)
    assert any("EXACT ZERO" in f for f in s["flags"])


def test_a_text_response_invalidates_the_sizes() -> None:
    """An error page served with HTTP 200 has a size, and averaging it into
    a feed-size estimate makes the estimate wrong in the comfortable
    direction. This project has met that page three times."""
    s = summarise_polls([poll("a", 50_000), poll("b", 900, binary=False)], 30)
    assert any("NOT BINARY" in f for f in s["flags"])


# -- cadence, from digests rather than from the feed's own claims ----------

def test_identical_bodies_mean_the_feed_did_not_update() -> None:
    """Six polls, three distinct bodies, each held for two polls. At 30 s
    that is an update every 60."""
    polls = [poll(s) for s in ("a", "a", "b", "b", "c", "c")]
    s = summarise_polls(polls, 30)
    assert s["distinct_bodies"] == 3
    assert s["duplicate_fraction"] == 0.5
    assert s["polls_between_changes_median"] == 2
    assert s["update_period_s_estimate"] == 60.0


def test_a_feed_that_changes_every_poll_is_reported_as_unresolved() -> None:
    """If the body changes between almost every pair of polls, the update
    period is at most the poll interval and the run cannot say how much
    less. Reporting the interval as the answer would be reporting the
    instrument's own resolution as a measurement."""
    s = summarise_polls([poll(c) for c in "abcdef"], 30)
    assert s["distinct_bodies"] == 6
    assert any("RESOLUTION FLOOR" in f for f in s["flags"])


def test_a_couple_of_duplicates_do_not_clear_the_resolution_floor() -> None:
    """THE CORRECTION OF 2026-10-05.

    The guard used to require that EVERY body differed. A real 5-second run
    against WMATA bus returned 34 distinct bodies of 36 -- two duplicate
    pairs, so the condition was false -- while the median gap between
    changes was still exactly one poll. The estimate equalled the poll
    interval and was just as unresolved, and the output said "changes about
    every 5 s" as though that were a measurement.

    The symptom was "all distinct". The principle is "the median gap is at
    the instrument's resolution floor", which holds whether or not a few
    duplicates land. A guard specified by the symptom in front of you is
    mechanism N, and this was the fourth instance.
    """
    digests = [f"d{i}" for i in range(36)]
    digests[8] = digests[7]
    digests[10] = digests[9]
    s = summarise_polls([poll(d) for d in digests], 5)
    assert s["distinct_bodies"] == 34
    assert s["duplicate_fraction"] == pytest.approx(0.056, abs=0.005)
    assert s["polls_between_changes_median"] == 1
    assert any("RESOLUTION FLOOR" in f for f in s["flags"])


def test_a_resolved_cadence_is_not_flagged() -> None:
    """Rule 16. A guard that fired on every run would be noise, and the
    whole point is that this one distinguishes resolved from not."""
    s = summarise_polls([poll(c) for c in "aaabbbccc"], 10)
    assert s["polls_between_changes_median"] == 3
    assert s["update_period_s_estimate"] == 30.0
    assert s["flags"] == []


def test_a_body_that_never_changes_is_not_evidence_of_a_slow_feed() -> None:
    """Static feed, stale feed, or the same error returned every time --
    three different facts with one footprint. The flag refuses to pick."""
    s = summarise_polls([poll("same") for _ in range(8)], 30)
    assert s["update_period_s_estimate"] is None
    msg = " ".join(s["flags"])
    assert "NEVER CHANGED" in msg
    assert "not evidence of a slow feed" in msg


def test_the_cadence_scales_with_the_poll_interval() -> None:
    """The same digest pattern at 10 s means a third of the period it means
    at 30 s. The estimate is in seconds, so the interval has to enter it."""
    polls = [poll(s) for s in ("a", "a", "b", "b", "c", "c")]
    assert summarise_polls(polls, 10)["update_period_s_estimate"] == 20.0
    assert summarise_polls(polls, 60)["update_period_s_estimate"] == 120.0


# -- failures ---------------------------------------------------------------

def test_every_poll_failing_is_an_outage_and_not_a_measurement() -> None:
    s = summarise_polls([poll("x", ok=False) for _ in range(5)], 30)
    assert s["polls_returned"] == 0
    assert "bytes_median" not in s
    assert any("only an outage" in f for f in s["flags"])
    assert verdict(s) == "no measurement, so no verdict"


def test_failures_are_counted_and_do_not_enter_the_sizes() -> None:
    s = summarise_polls([poll("a", 100), poll("x", ok=False),
                         poll("b", 200)], 30)
    assert s["polls_attempted"] == 3 and s["polls_returned"] == 2
    assert s["polls_failed"] == 1
    assert s["bytes_max"] == 200


# -- the projection, which is the whole point -------------------------------

def test_the_projection_covers_every_interval_and_names_its_assumption() -> None:
    s = summarise_polls([poll("a", 50_000), poll("b", 50_000)], 30)
    p = s["projection"]
    assert p["feeds_assumed"] == POLLABLE_FEEDS
    assert set(p["per_interval"]) == {str(i) for i in INTERVALS_TO_PROJECT}
    assert "order of magnitude and not a forecast" in p["note"]


def test_requests_per_day_is_arithmetic_anyone_can_check() -> None:
    s = summarise_polls([poll("a", 1), poll("b", 1)], 30)
    at30 = s["projection"]["per_interval"]["30"]
    assert at30["requests_per_day"] == (86_400 // 30) * POLLABLE_FEEDS


def test_halving_the_interval_doubles_the_cost() -> None:
    s = summarise_polls([poll("a", 50_000), poll("b", 50_000)], 30)
    per = s["projection"]["per_interval"]
    assert per["30"]["requests_per_day"] == 2 * per["60"]["requests_per_day"]
    assert per["30"]["gib_per_day"] == pytest.approx(
        2 * per["60"]["gib_per_day"], rel=0.02)


# -- the verdict ------------------------------------------------------------

def test_a_small_feed_means_the_cost_is_not_the_constraint() -> None:
    """At 5 kB a poll, 191 feeds every 15 s is well under a gibibyte, so the
    narrowing is about which feeds are RELEVANT rather than affordable --
    and saying so stops a geographic selection being justified by a cost
    argument that does not hold."""
    s = summarise_polls([poll("a", 5_000), poll("b", 5_000)], 30)
    v = verdict(s, budget_gib=5.0)
    assert "affordable at full rate" in v and "relevance" in v


def test_a_large_feed_forces_the_list_to_be_narrowed() -> None:
    s = summarise_polls([poll("a", 5_000_000), poll("b", 5_000_000)], 30)
    v = verdict(s, budget_gib=5.0)
    assert "narrowed by geography, not by rate" in v


def test_a_middling_feed_says_narrow_and_keep_the_rate() -> None:
    """The answer that matters most: slowing the poll to afford the whole
    list would destroy segment speed, which is the measurement. Narrow the
    list and keep the rate.

    15 kB is chosen by arithmetic rather than by feel. 191 feeds at 30 s is
    550,080 requests a day, so a 5 GiB budget is passed at about 9.8 kB a
    poll and at 60 s it is passed at about 19.5 kB. A feed between those two
    is affordable only by halving the rate, which is the case this verdict
    exists for. The first draft used 300 kB, which is 165 GB a day and lands
    in the unaffordable case instead -- the fixture was wrong, not the code.
    """
    s = summarise_polls([poll("a", 15_000), poll("b", 15_000)], 30)
    v = verdict(s, budget_gib=5.0)
    assert "too coarse for segment speed" in v


def test_the_three_verdicts_are_distinct() -> None:
    """Rule 13. Three cases, three sentences."""
    said = {verdict(summarise_polls([poll("a", n), poll("b", n)], 30))
            for n in (5_000, 15_000, 5_000_000)}
    assert len(said) == 3
