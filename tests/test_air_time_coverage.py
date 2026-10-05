"""The time denominator, and the distinction it exists to preserve.

Nothing here touches the network or the collected archive. Heartbeat files
are written into tmp_path in the documented on-disk format, so the tests
exercise the real reader rather than a stand-in for it.

WHY THIS FILE EXISTS

Until 2026-10-05 `air_discrepancy.py` read whatever parquet happened to be on
disk and said nothing about the hours that were not. Eighteen hours of a
235-hour span held no partition and left the published result by accident of
absence rather than by declaration. The information needed to classify them
had been written to `data/raw/collector/heartbeat-*.jsonl` since 2 September
and no analysis read it.

The distinction those heartbeats exist to preserve is the whole point, and it
is stated in `angels/core/uptime.py`:

    heartbeats present, no aircraft   -> the sky was quiet. A real finding.
    no heartbeats                     -> we were not looking. Exclude it.

Collapsing those two reports our own downtime as somebody else's silence. So
the tests below are mostly about which of five outcomes a missing hour gets,
and the one that matters most is `test_missing_file_is_not_downtime`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from angels.core import uptime
from scripts.air_discrepancy import (COLLECTOR_NAME, coverage_sentence,
                                     hours_in_span, time_coverage)


# -- helpers ----------------------------------------------------------------

def write_beats(root, hour: str, *, ok: int = 0, fail: int = 0,
                collector: str = COLLECTOR_NAME,
                error: str = "ConnectError: getaddrinfo failed") -> None:
    """Append `ok` successful and `fail` failed poll records inside `hour`.

    Written through the documented path helper rather than a hand-built one,
    so a change to the on-disk layout breaks this file instead of silently
    making it test nothing.
    """
    when = datetime.strptime(hour, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    path = uptime.day_file(root, when)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    t = when + timedelta(minutes=1)
    for i in range(ok + fail):
        good = i < ok
        lines.append(json.dumps({
            "t": int((t + timedelta(seconds=30 * i)).timestamp()),
            "iso": (t + timedelta(seconds=30 * i)).isoformat(),
            "event": "poll", "collector": collector,
            "session": "testsession", "ok": good,
            "n": 120 if good else 0,
            "error": None if good else error,
        }))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def touch_day(root, day: str) -> None:
    """An empty heartbeat file for a day: present, holding no records."""
    when = datetime.strptime(day, "%Y%m%d").replace(tzinfo=timezone.utc)
    path = uptime.day_file(root, when)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


# -- hours_in_span ----------------------------------------------------------

def test_span_is_contiguous_between_the_hours_on_disk() -> None:
    """The span is first-to-last inclusive, including the hours that are
    missing -- which is the only way a missing hour can be counted."""
    assert hours_in_span(["2026100120", "2026100123"]) == [
        "2026100120", "2026100121", "2026100122", "2026100123"]


def test_span_crosses_a_day_boundary() -> None:
    assert hours_in_span(["2026100123", "2026100201"]) == [
        "2026100123", "2026100200", "2026100201"]


def test_one_hour_is_a_span_of_one() -> None:
    assert hours_in_span(["2026100512"]) == ["2026100512"]


def test_no_hours_is_not_an_error_and_not_a_span() -> None:
    """An empty archive produces an empty span rather than a crash or a
    silent zero that reads as full coverage."""
    assert hours_in_span([]) == []
    assert hours_in_span(set()) == []


# -- the five outcomes ------------------------------------------------------

def test_an_hour_with_a_partition_is_simply_data(tmp_path) -> None:
    touch_day(tmp_path, "20261001")
    cov = time_coverage(tmp_path, ["2026100110", "2026100111"])
    assert cov["hours_in_span"] == 2
    assert cov["hours_with_data"] == 2
    assert cov["hours_not_looking"] == 0
    assert cov["hours_looked_all_failed"] == 0


def test_every_poll_failed_is_a_measured_hole_not_an_absence(tmp_path) -> None:
    """120 attempts, 120 failures, no partition. We were looking; the
    resolver was not. This is the ten-of-eighteen case from 2026-10-05."""
    write_beats(tmp_path, "2026100110", ok=12)
    write_beats(tmp_path, "2026100111", fail=120)
    write_beats(tmp_path, "2026100112", ok=12)
    cov = time_coverage(tmp_path, ["2026100110", "2026100112"])
    assert cov["hours_looked_all_failed"] == 1
    assert cov["looked_all_failed_hours"] == ["2026100111"]
    assert cov["hours_not_looking"] == 0
    assert cov["polls_failed"] == 120
    assert cov["polls_returned"] == 24
    assert cov["polls_attempted"] == 144
    assert "ConnectError: getaddrinfo failed" in cov["failure_reasons"]


def test_a_present_file_with_no_record_is_downtime(tmp_path) -> None:
    """The file for the day exists and holds nothing for that hour, so we
    have a record of not having polled. This is the eight-of-eighteen case,
    and the only true exclusion."""
    write_beats(tmp_path, "2026100110", ok=12)
    write_beats(tmp_path, "2026100112", ok=12)
    cov = time_coverage(tmp_path, ["2026100110", "2026100112"])
    assert cov["hours_not_looking"] == 1
    assert cov["not_looking_hours"] == ["2026100111"]
    assert cov["hours_unknown_no_heartbeat_file"] == 0


def test_missing_file_is_not_downtime(tmp_path) -> None:
    """THE ASSERTION THIS FILE IS FOR.

    No heartbeat file for the day means we cannot say what the collector was
    doing. Calling that downtime would be an inference dressed as a
    measurement, and in the other direction -- calling real downtime
    "unknown" -- it would hide an exclusion that has to be declared.

    Both wrong answers are the same defect: a routine reporting an outcome
    its own evidence does not entitle it to report.

    The first version of this test asserted `hours_not_looking == 0` over a
    span of 2026-10-01 10:00 to 2026-10-02 10:00 and failed with 13. It was
    right to fail. The file for 1 October existed and held no record for
    hours 11 to 23, so those thirteen hours ARE downtime, correctly
    classified; the assertion had quietly assumed the span contained only the
    day with no file. The test was wrong and the code was not, which is the
    compared-implementations technique working in the direction nobody plans
    for.

    So the span is now kept tight enough that each outcome has exactly one
    cause.
    """
    write_beats(tmp_path, "2026100123", ok=12)
    # 2026-10-02 gets NO heartbeat file at all.
    cov = time_coverage(tmp_path, ["2026100123", "2026100202"])
    # span is 10-01 23:00, then 10-02 00:00, 01:00, 02:00
    assert cov["hours_in_span"] == 4
    assert cov["hours_with_data"] == 2                     # 23:00 and 02:00
    assert cov["unknown_hours"] == ["2026100200", "2026100201"]
    assert cov["hours_not_looking"] == 0
    assert not any(h.startswith("20261002")
                   for h in cov["not_looking_hours"])


def test_returned_polls_without_a_partition_are_the_flush_boundary(tmp_path) -> None:
    """2026-10-02 21:00 UTC: four polls returned, 685 aircraft recorded, and
    no partition. The archive is keyed on flush time, so polls near an hour
    boundary land next door. Nothing is lost, and it is not a hole -- but it
    must not be counted as one, and anything computing coverage from
    partition names alone would count it as one at every boundary."""
    write_beats(tmp_path, "2026100220", ok=12)
    write_beats(tmp_path, "2026100221", ok=4, fail=113)
    write_beats(tmp_path, "2026100222", ok=12)
    cov = time_coverage(tmp_path, ["2026100220", "2026100222"])
    assert cov["hours_rows_in_adjacent_partition"] == 1
    assert cov["rows_in_adjacent_partition_hours"] == ["2026100221"]
    assert cov["hours_looked_all_failed"] == 0
    assert cov["hours_not_looking"] == 0


def test_another_collectors_heartbeats_are_not_ours(tmp_path) -> None:
    """Five collectors write into one file per day. Reading all of them would
    make another poller's uptime cover this archive's downtime."""
    write_beats(tmp_path, "2026100110", ok=12)
    write_beats(tmp_path, "2026100111", ok=12, collector="maritime")
    write_beats(tmp_path, "2026100112", ok=12)
    cov = time_coverage(tmp_path, ["2026100110", "2026100112"])
    assert cov["hours_not_looking"] == 1, (
        "a maritime heartbeat made an aviation hour look covered")
    assert cov["polls_returned"] == 24


def test_a_start_record_is_not_a_poll(tmp_path) -> None:
    """`start` and `stop` events share the file with `poll`. Counting them as
    polls inflates the attempted count, which is a denominator."""
    when = datetime(2026, 10, 1, 11, 5, tzinfo=timezone.utc)
    path = uptime.day_file(tmp_path, when)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "t": int(when.timestamp()), "iso": when.isoformat(),
        "event": "start", "collector": COLLECTOR_NAME,
        "session": "s", "ok": True, "n": 0, "error": None}) + "\n",
        encoding="utf-8")
    write_beats(tmp_path, "2026100110", ok=12)
    write_beats(tmp_path, "2026100112", ok=12)
    cov = time_coverage(tmp_path, ["2026100110", "2026100112"])
    assert cov["polls_attempted"] == 24
    # The hour holds a start event and no poll, so we were not polling.
    assert cov["hours_not_looking"] == 1


def test_an_empty_archive_reports_no_span_rather_than_full_coverage(tmp_path) -> None:
    cov = time_coverage(tmp_path, [])
    assert cov["hours_in_span"] == 0
    assert cov["hours_with_data"] == 0
    assert cov["span_first_hour_utc"] is None
    assert "no hours on disk" in coverage_sentence(cov)


# -- coverage_sentence ------------------------------------------------------

def test_full_coverage_says_so_plainly() -> None:
    cov = {"hours_in_span": 24, "hours_with_data": 24,
           "hours_not_looking": 0, "hours_looked_all_failed": 0,
           "hours_rows_in_adjacent_partition": 0,
           "hours_unknown_no_heartbeat_file": 0}
    assert coverage_sentence(cov) == "all 24 hour(s) of the span carry data"


def test_sixteen_combinations_give_sixteen_distinct_sentences() -> None:
    """Rule 13. A conclusion is a named function tested for every combination,
    and the COUNT is what catches a swallowed case -- a missing branch looks
    perfectly correct in isolation.

    Four non-data outcomes, each present or absent, is sixteen cases. Any two
    collapsing into one sentence means the artefact and the console can
    describe two different coverage situations identically.
    """
    keys = ("hours_not_looking", "hours_looked_all_failed",
            "hours_rows_in_adjacent_partition",
            "hours_unknown_no_heartbeat_file")
    seen = {}
    for bits in range(16):
        cov = {"hours_in_span": 100, "hours_with_data": 60}
        for i, k in enumerate(keys):
            cov[k] = (bits >> i) & 1
        s = coverage_sentence(cov)
        assert s not in seen, (
            "two different coverage states produce the same sentence:\n"
            "  %r and %r\n  both say: %s" % (seen.get(s), bits, s))
        seen[s] = bits
    assert len(seen) == 16


def test_the_sentence_never_calls_an_unknown_hour_downtime() -> None:
    """The wording matters as much as the count. An hour with no heartbeat
    file must not be described in the language of downtime."""
    cov = {"hours_in_span": 10, "hours_with_data": 8,
           "hours_not_looking": 0, "hours_looked_all_failed": 0,
           "hours_rows_in_adjacent_partition": 0,
           "hours_unknown_no_heartbeat_file": 2}
    s = coverage_sentence(cov)
    assert "cannot be called downtime" in s
    assert "real downtime" not in s


def test_a_measured_hole_is_not_described_as_an_absence() -> None:
    cov = {"hours_in_span": 10, "hours_with_data": 9,
           "hours_not_looking": 0, "hours_looked_all_failed": 1,
           "hours_rows_in_adjacent_partition": 0,
           "hours_unknown_no_heartbeat_file": 0}
    s = coverage_sentence(cov)
    assert "measured hole" in s and "not an absence" in s


# -- the artefact contract --------------------------------------------------

@pytest.mark.parametrize("field", [
    "collector", "heartbeat_source", "span_first_hour_utc",
    "span_last_hour_utc", "hours_in_span", "hours_with_data",
    "hours_looked_all_failed", "hours_rows_in_adjacent_partition",
    "hours_not_looking", "hours_unknown_no_heartbeat_file",
    "not_looking_hours", "looked_all_failed_hours",
    "rows_in_adjacent_partition_hours", "unknown_hours",
    "polls_attempted", "polls_returned", "polls_failed", "failure_reasons",
])
def test_the_coverage_block_declares_every_field(tmp_path, field) -> None:
    """The artefact has to carry the inputs that define the measurement, in
    the same file -- rule 12. A coverage block missing a field would let a
    reader assume a zero that was never computed."""
    write_beats(tmp_path, "2026100110", ok=12)
    cov = time_coverage(tmp_path, ["2026100110"])
    assert field in cov


def test_the_five_outcomes_account_for_every_hour(tmp_path) -> None:
    """The five counts sum to the span. If they did not, hours would be
    disappearing between categories and the denominator would be wrong in a
    direction nobody could see."""
    write_beats(tmp_path, "2026100110", ok=12)
    write_beats(tmp_path, "2026100112", fail=120)
    write_beats(tmp_path, "2026100114", ok=4)
    write_beats(tmp_path, "2026100116", ok=12)
    # 2026-10-02 has no file; 11, 13, 15 have the file but no records.
    cov = time_coverage(tmp_path, ["2026100110", "2026100116", "2026100210"])
    total = (cov["hours_with_data"] + cov["hours_looked_all_failed"]
             + cov["hours_rows_in_adjacent_partition"]
             + cov["hours_not_looking"]
             + cov["hours_unknown_no_heartbeat_file"])
    assert total == cov["hours_in_span"]


# -- file_poll_agreement ----------------------------------------------------
#
# Two independent records of one quantity, related by a known constant. The
# comparison is what caught a file count published as a poll count, so it
# lives in the code now rather than in somebody's memory of one afternoon.

def test_ten_polls_per_file_is_agreement() -> None:
    from scripts.air_discrepancy import (EXPECTED_POLLS_PER_FILE,
                                         file_poll_agreement)
    assert EXPECTED_POLLS_PER_FILE == 10
    assert file_poll_agreement(100, {"polls_returned": 1000}) is None


def test_the_published_mistake_would_now_be_caught() -> None:
    """The real numbers from the Phase F window: 1,683 partition files and
    16,851 returned polls agree. A field holding 1,659 and claiming to be
    polls does not, and this is the comparison that says so."""
    from scripts.air_discrepancy import file_poll_agreement
    assert file_poll_agreement(1683, {"polls_returned": 16851}) is None
    msg = file_poll_agreement(1683, {"polls_returned": 1659})
    assert msg is not None
    assert "disagree about volume" in msg
    assert "1.0 polls per file" in msg


def test_a_tolerance_band_rather_than_an_exact_ratio() -> None:
    """Flushes straddle hour boundaries and a run can end mid-flush, so the
    ratio is never exactly ten. A check that fired on every ordinary run
    would be rule 16's false alarm."""
    from scripts.air_discrepancy import file_poll_agreement
    assert file_poll_agreement(100, {"polls_returned": 600}) is None
    assert file_poll_agreement(100, {"polls_returned": 1400}) is None
    assert file_poll_agreement(100, {"polls_returned": 400}) is not None
    assert file_poll_agreement(100, {"polls_returned": 1600}) is not None


def test_nothing_to_compare_is_not_a_disagreement() -> None:
    """An empty archive, or a span with no heartbeats, gives nothing to
    compare. Reporting that as a disagreement would be an absence read as a
    finding, which is this project's own defect class."""
    from scripts.air_discrepancy import file_poll_agreement
    assert file_poll_agreement(0, {"polls_returned": 0}) is None
    assert file_poll_agreement(100, {"polls_returned": 0}) is None
    assert file_poll_agreement(0, {"polls_returned": 1000}) is None
    assert file_poll_agreement(100, {}) is None
