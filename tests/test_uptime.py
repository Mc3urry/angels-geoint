"""Tests for collector uptime.

The distinction under test is the one the whole gap detector rests on: an
empty hour with heartbeats means the sky was quiet, and an empty hour without
them means we were not looking. Confusing the two would have a detector report
a closed laptop as coordinated non-cooperative behaviour.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from angels.core.uptime import (
    HeartbeatLog, blind_intervals, read_heartbeats, sessions,
    uptime_intervals, was_collecting,
)

T0 = datetime(2026, 9, 2, 18, 0, 0, tzinfo=timezone.utc)


def _log(root, collector="aviation", interval=30.0):
    return HeartbeatLog(root, collector=collector, interval_s=interval)


def _write_session(root, start: datetime, n_polls: int, *,
                   collector="aviation", clean=True, gap_s=30,
                   fail_every: int | None = None):
    """Write a session by hand at controlled timestamps."""
    hb = _log(root, collector)
    recs = [{"event": "start", "t": start}]
    for i in range(n_polls):
        recs.append({"event": "poll", "t": start + timedelta(seconds=gap_s * (i + 1)),
                     "ok": not (fail_every and i % fail_every == 0)})
    if clean:
        recs.append({"event": "stop", "t": start + timedelta(seconds=gap_s * (n_polls + 1))})

    path = root / "collector" / f"heartbeat-{start:%Y-%m-%d}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for r in recs:
            t = r.pop("t")
            fh.write(json.dumps({
                "t": int(t.timestamp()), "iso": t.isoformat(),
                "collector": collector, "session": hb.session_id, **r,
            }) + "\n")
    return hb.session_id


# -- writing ---------------------------------------------------------------

def test_writes_one_line_per_event(tmp_path) -> None:
    hb = _log(tmp_path)
    hb.start()
    hb.poll(ok=True, n=23)
    hb.poll(ok=True, n=19)
    hb.stop()
    recs = list(read_heartbeats(tmp_path, T0 - timedelta(days=1),
                                datetime.now(timezone.utc) + timedelta(days=1)))
    assert [r["event"] for r in recs] == ["start", "poll", "poll", "stop"]


def test_failed_poll_is_still_a_heartbeat(tmp_path) -> None:
    """We were awake and we asked. That distinguishes 'the API refused us'
    from 'the laptop was asleep' -- different stories, same empty hour."""
    hb = _log(tmp_path)
    hb.start()
    hb.poll(ok=False, error="connection reset")
    hb.stop()
    recs = [r for r in read_heartbeats(tmp_path, T0 - timedelta(days=1),
                                       datetime.now(timezone.utc) + timedelta(days=1))
            if r["event"] == "poll"]
    assert len(recs) == 1
    assert recs[0]["ok"] is False
    assert "connection reset" in recs[0]["error"]


def test_counts_accumulate(tmp_path) -> None:
    hb = _log(tmp_path)
    hb.poll(ok=True, n=5)
    hb.poll(ok=False, error="x")
    hb.poll(ok=True, n=7)
    assert hb.polls == 3
    assert hb.failures == 1


def test_survives_a_truncated_final_line(tmp_path) -> None:
    """A half-written last line is the normal signature of a killed process.
    Refusing to read the file because of it would defeat the purpose."""
    hb = _log(tmp_path)
    hb.start()
    hb.poll(ok=True, n=3)
    path = next((tmp_path / "collector").glob("*.jsonl"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"t": 123, "event": "pol')          # killed mid-write

    recs = list(read_heartbeats(tmp_path, T0 - timedelta(days=1),
                                datetime.now(timezone.utc) + timedelta(days=1)))
    assert len(recs) == 2


# -- sessions --------------------------------------------------------------

def test_clean_session_is_marked_clean(tmp_path) -> None:
    _write_session(tmp_path, T0, 10, clean=True)
    s = sessions(tmp_path, T0 - timedelta(hours=1), T0 + timedelta(hours=2))[0]
    assert s.clean is True
    assert s.polls == 10
    assert s.ended is not None


def test_crashed_session_has_no_stop(tmp_path) -> None:
    """Start with no stop means killed, not stopped. That is information:
    the downtime began abruptly rather than by choice."""
    _write_session(tmp_path, T0, 10, clean=False)
    s = sessions(tmp_path, T0 - timedelta(hours=1), T0 + timedelta(hours=2))[0]
    assert s.clean is False
    assert s.ended is None
    assert s.last_seen > s.started


def test_failures_are_counted_separately(tmp_path) -> None:
    _write_session(tmp_path, T0, 10, fail_every=3)
    s = sessions(tmp_path, T0 - timedelta(hours=1), T0 + timedelta(hours=2))[0]
    assert s.polls == 10
    assert 0 < s.failures < 10


def test_separate_runs_are_separate_sessions(tmp_path) -> None:
    _write_session(tmp_path, T0, 5)
    _write_session(tmp_path, T0 + timedelta(hours=3), 5)
    ss = sessions(tmp_path, T0 - timedelta(hours=1), T0 + timedelta(hours=5))
    assert len(ss) == 2
    assert ss[0].started < ss[1].started


def test_collector_filter(tmp_path) -> None:
    _write_session(tmp_path, T0, 5, collector="aviation")
    _write_session(tmp_path, T0, 5, collector="maritime")
    assert len(sessions(tmp_path, T0 - timedelta(hours=1), T0 + timedelta(hours=2))) == 2
    assert len(sessions(tmp_path, T0 - timedelta(hours=1), T0 + timedelta(hours=2),
                        collector="aviation")) == 1


# -- the question that actually matters ------------------------------------

def test_blind_interval_found_between_two_sessions(tmp_path) -> None:
    """The gap at hour 20 between runs. This is the window a gap detector
    must refuse to draw conclusions from."""
    _write_session(tmp_path, T0, 10)                        # 18:00 -> ~18:05
    _write_session(tmp_path, T0 + timedelta(hours=3), 10)   # 21:00 -> ~21:05

    blind = blind_intervals(tmp_path, T0, T0 + timedelta(hours=4))
    assert any((b - a).total_seconds() > 9000 for a, b in blind), \
        "the ~3h hole between sessions was not reported"


def test_no_log_at_all_means_entirely_blind(tmp_path) -> None:
    """Absence of evidence is not evidence of a quiet sky."""
    blind = blind_intervals(tmp_path, T0, T0 + timedelta(hours=2))
    assert blind == [(T0, T0 + timedelta(hours=2))]


def test_running_period_is_not_blind(tmp_path) -> None:
    _write_session(tmp_path, T0, 100, gap_s=30)   # 50 minutes of polling
    blind = blind_intervals(tmp_path, T0 + timedelta(minutes=5),
                            T0 + timedelta(minutes=40))
    assert blind == []


def test_short_restart_seam_is_ignored(tmp_path) -> None:
    """A restart is not an outage worth modelling.

    Session one runs 18:00:00 -> 18:05:30, session two 18:05:40 -> 18:11:10.
    The ten-second seam between them must not be reported. The window here is
    bounded by the sessions themselves -- asking about time after the second
    one ended would correctly report that we had stopped looking, which is a
    different question.
    """
    _write_session(tmp_path, T0, 10, gap_s=30)                    # ends ~18:05:30
    _write_session(tmp_path, T0 + timedelta(seconds=340), 10)     # resumes ~18:05:40
    blind = blind_intervals(tmp_path, T0, T0 + timedelta(minutes=11),
                            min_seconds=60)
    assert blind == [], f"the restart seam was reported as an outage: {blind}"


def test_the_seam_is_real_but_short(tmp_path) -> None:
    """Same setup, min_seconds=0. The gap genuinely exists -- the point is
    that it is ten seconds, not that it is nothing."""
    _write_session(tmp_path, T0, 10, gap_s=30)
    _write_session(tmp_path, T0 + timedelta(seconds=340), 10)
    blind = blind_intervals(tmp_path, T0, T0 + timedelta(minutes=11),
                            min_seconds=0)
    # There may also be a short tail: session two's stop event falls outside
    # the query window, so its span reads as ending at its last poll. That is
    # correct behaviour -- we only claim uptime we have evidence for.
    seam = [(b - a).total_seconds() for a, b in blind]
    assert seam, "the seam should exist at min_seconds=0"
    assert min(seam) < 30, f"expected a sub-30s seam, got {seam}"


def test_was_collecting_answers_per_instant(tmp_path) -> None:
    _write_session(tmp_path, T0, 20, gap_s=30)     # 18:00 -> ~18:10
    assert was_collecting(tmp_path, T0 + timedelta(minutes=5)) is True
    assert was_collecting(tmp_path, T0 + timedelta(minutes=45)) is False


def test_uptime_intervals_merge_overlaps(tmp_path) -> None:
    _write_session(tmp_path, T0, 20, gap_s=30)
    _write_session(tmp_path, T0 + timedelta(minutes=5), 20, gap_s=30)
    up = uptime_intervals(tmp_path, T0, T0 + timedelta(hours=1))
    assert len(up) == 1, "overlapping sessions should merge into one window"


def test_blind_and_uptime_are_complements(tmp_path) -> None:
    _write_session(tmp_path, T0 + timedelta(minutes=10), 20, gap_s=30)
    t0, t1 = T0, T0 + timedelta(hours=1)
    up = uptime_intervals(tmp_path, t0, t1)
    blind = blind_intervals(tmp_path, t0, t1, min_seconds=0)
    total = sum((b - a).total_seconds() for a, b in up + blind)
    assert total == pytest.approx((t1 - t0).total_seconds(), abs=1)


# -- the lock --------------------------------------------------------------

def test_lock_blocks_a_second_collector(tmp_path) -> None:
    """The mistake this exists to prevent: two pollers, both working, both
    spending quota, neither complaining.

    The holder has to be a DIFFERENT live process. acquire() deliberately
    exempts our own PID so a caller cannot deadlock against itself, which
    means two locks inside one test process would not conflict. os.getppid()
    gives a real, live, other PID.
    """
    import json, os
    from angels.core.uptime import AlreadyRunning, CollectorLock

    d = tmp_path / "collector"
    d.mkdir(parents=True)
    (d / "aviation.lock").write_text(json.dumps({
        "pid": os.getppid(), "session": "aaa", "collector": "aviation",
        "t": 0, "iso": "2026-09-02T22:00:00+00:00",
    }))

    with pytest.raises(AlreadyRunning) as e:
        CollectorLock(tmp_path, "aviation", session_id="bbb").acquire()
    assert "double quota" in str(e.value)
    assert str(os.getppid()) in str(e.value)


def test_lock_is_released_on_exit(tmp_path) -> None:
    from angels.core.uptime import CollectorLock

    with CollectorLock(tmp_path, "aviation", session_id="aaa"):
        pass
    CollectorLock(tmp_path, "aviation", session_id="bbb").acquire()   # no raise


def test_stale_lock_from_a_crash_is_taken_over(tmp_path) -> None:
    """A crashed collector leaves its lock behind. A lock whose PID is not
    alive is not a lock -- otherwise one crash would need manual cleanup
    before collection could resume."""
    import json
    from angels.core.uptime import CollectorLock

    d = tmp_path / "collector"
    d.mkdir(parents=True)
    (d / "aviation.lock").write_text(json.dumps({
        "pid": 999_999, "session": "dead", "collector": "aviation",
        "t": 0, "iso": "2026-01-01T00:00:00+00:00",
    }))
    CollectorLock(tmp_path, "aviation", session_id="new").acquire()   # no raise


def test_force_overrides_a_live_lock(tmp_path) -> None:
    """The escape hatch, for when you are certain the holder is dead."""
    import json, os
    from angels.core.uptime import CollectorLock

    d = tmp_path / "collector"
    d.mkdir(parents=True)
    (d / "aviation.lock").write_text(json.dumps({
        "pid": os.getppid(), "session": "aaa", "collector": "aviation",
        "t": 0, "iso": "x",
    }))
    CollectorLock(tmp_path, "aviation", session_id="bbb",
                  force=True).acquire()      # no raise


def test_running_collectors_reports_liveness(tmp_path) -> None:
    from angels.core.uptime import CollectorLock, running_collectors

    lock = CollectorLock(tmp_path, "aviation", session_id="aaa")
    lock.acquire()
    try:
        live = running_collectors(tmp_path)
        assert len(live) == 1
        assert live[0]["alive"] is True
        assert live[0]["session"] == "aaa"
    finally:
        lock.release()
    assert running_collectors(tmp_path) == []


def test_release_does_not_remove_another_process_lock(tmp_path) -> None:
    """Releasing must only clear a lock we hold. Deleting someone else's
    would defeat the whole mechanism."""
    import json, os
    from angels.core.uptime import CollectorLock

    d = tmp_path / "collector"
    d.mkdir(parents=True)
    (d / "aviation.lock").write_text(json.dumps({
        "pid": os.getpid() + 1, "session": "other", "collector": "aviation",
        "t": 0, "iso": "x",
    }))
    CollectorLock(tmp_path, "aviation").release()
    assert (d / "aviation.lock").exists()
