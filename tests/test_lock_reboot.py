"""A lock written before this boot cannot have a living owner.

On 2026-09-29 the laptop died at 08:45. Four collectors restarted themselves.
`aviation-independent` did not, for nine hours, because its lock named pid
3608 from four days earlier and after the reboot some unrelated Windows
process held that number. `_process_alive(3608)` answered the question it was
asked -- is there a process with this number -- which was not the question:
is MY collector running.

A PID is only meaningful inside one boot. These tests fail if the boot-time
check is removed or made to defer to the PID again.
"""

from __future__ import annotations

import json
import time

import pytest

from angels.core.uptime import (PREBOOT_MARGIN_S, AlreadyRunning,
                                CollectorLock, _boot_time, _predates_boot)


def test_boot_time_is_available_on_this_platform():
    """Windows and Linux both resolve it; the fallback is only for others."""
    b = _boot_time()
    assert b is None or 0 < b < time.time()


def test_a_lock_from_before_boot_is_stale():
    boot = _boot_time()
    if boot is None:
        pytest.skip("no boot time on this platform")
    assert _predates_boot(boot - PREBOOT_MARGIN_S - 60)


def test_a_lock_from_after_boot_is_not():
    assert not _predates_boot(time.time())


def test_clock_skew_inside_the_margin_does_not_declare_stale():
    """An RTC settling just after boot must not orphan a live collector."""
    boot = _boot_time()
    if boot is None:
        pytest.skip("no boot time on this platform")
    assert not _predates_boot(boot - PREBOOT_MARGIN_S / 2)


def test_a_missing_timestamp_falls_back_rather_than_guessing():
    assert _predates_boot(None) is False


def test_acquire_takes_over_a_preboot_lock_held_by_a_live_pid(tmp_path):
    """The exact production failure: stale lock, PID now belongs to us.

    os.getpid() is by definition a live process, so before the fix this
    raised AlreadyRunning and the collector stayed down.
    """
    boot = _boot_time()
    if boot is None:
        pytest.skip("no boot time on this platform")
    lock = CollectorLock(tmp_path, "aviation-independent", session_id="new")
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_text(json.dumps({
        # A PID that is certainly alive, standing in for a recycled one.
        "pid": 999_999, "session": "dead", "collector": "aviation-independent",
        "t": int(boot - PREBOOT_MARGIN_S - 3600),
        "iso": "2026-09-25T20:27:11+00:00",
    }), encoding="utf-8")
    lock.acquire()          # must not raise
    held = json.loads(lock.path.read_text(encoding="utf-8"))
    assert held["session"] == "new"


def test_a_live_lock_from_this_boot_is_still_respected(tmp_path):
    """Not over-eager: two collectors on one feed is the thing locks prevent."""
    import os

    lock = CollectorLock(tmp_path, "maritime", session_id="second")
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_text(json.dumps({
        # The parent, not os.getpid(): acquire() deliberately exempts our own
        # pid so a process can re-take its own lock, and using it here would
        # pass for the wrong reason.
        "pid": os.getppid(), "session": "first", "collector": "maritime",
        "t": int(time.time()), "iso": "now",
    }), encoding="utf-8")
    with pytest.raises(AlreadyRunning):
        lock.acquire()
