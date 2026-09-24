"""Collector uptime: proving whether silence was theirs or ours.

THE PROBLEM THIS SOLVES

A gap in the archive is ambiguous. Every aircraft in the region disappearing
for an hour is either the most coordinated act of transponder-switching in
aviation history, or the laptop went to sleep. Those mean opposite things and
look identical in the data.

You cannot reconstruct this later. In February there will be no way to know
whether hour 20 on 2 September was you or them. So the collector records that
it was alive and asked, separately from whatever came back, and it does so at
the time -- which is the only time the information exists.

    heartbeats present, no aircraft   -> the sky was quiet. A real finding.
    no heartbeats                     -> we were not looking. Exclude the window.

core.detectors.gaps and core.coverage both depend on this distinction. A gap
detector that cannot make it will confidently report your own downtime as
non-cooperative behaviour.

FORMAT

Line-delimited JSON, appended and flushed per poll, one file per day. Not
Parquet, deliberately: a buffered columnar writer loses whatever it is holding
when the process is killed -- and the polls immediately before a crash are
precisely the ones you need. An appended line survives anything short of the
disk going away.

Stdlib only, like the rest of core.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

Event = Literal["start", "poll", "stop"]

COLLECTOR_DIR = "collector"


def _day_file(root: Path, when: datetime) -> Path:
    return root / COLLECTOR_DIR / f"heartbeat-{when:%Y-%m-%d}.jsonl"


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

class HeartbeatLog:
    """Append-only record that a collector was alive and asking.

    Used by every ingest script, not just aviation -- the maritime collector
    will have exactly the same problem.

        hb = HeartbeatLog(RAW, collector="aviation", interval_s=30)
        hb.start(bbox=AOI_AIR)
        ...
        hb.poll(ok=True, n=23)
        hb.poll(ok=False, error="connection reset")
        ...
        hb.stop(reason="signal")

    A session with a start and no stop is a crash. That is itself information:
    it tells you the downtime began abruptly rather than by choice.
    """

    def __init__(self, root: Path, *, collector: str,
                 interval_s: float, session_id: str | None = None) -> None:
        self.root = Path(root)
        self.collector = collector
        self.interval_s = interval_s
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.polls = 0
        self.failures = 0

    def _write(self, event: Event, **fields: Any) -> None:
        now = datetime.now(timezone.utc)
        rec = {
            "t": int(now.timestamp()),
            "iso": now.isoformat(),
            "event": event,
            "collector": self.collector,
            "session": self.session_id,
            **fields,
        }
        path = _day_file(self.root, now)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            # fsync so a hard power loss cannot lose the last few lines. The
            # cost is irrelevant at one line per poll, and the whole point of
            # this file is to be trustworthy about failures.
            os.fsync(fh.fileno())

    def start(self, **fields: Any) -> None:
        self._write("start", interval_s=self.interval_s, **fields)

    def poll(self, *, ok: bool, n: int = 0, error: str | None = None) -> None:
        self.polls += 1
        if not ok:
            self.failures += 1
        self._write("poll", ok=ok, n=n, error=error)

    def stop(self, *, reason: str = "clean") -> None:
        self._write("stop", reason=reason, polls=self.polls,
                    failures=self.failures)


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Session:
    """One run of a collector."""

    session_id: str
    collector: str
    started: datetime
    last_seen: datetime
    ended: datetime | None       # None when the process never wrote a stop
    polls: int
    failures: int

    @property
    def clean(self) -> bool:
        """False means it was killed rather than stopped."""
        return self.ended is not None

    @property
    def duration(self) -> timedelta:
        return (self.ended or self.last_seen) - self.started

    def covers(self, t: datetime) -> bool:
        return self.started <= t <= (self.ended or self.last_seen)


def read_heartbeats(root: Path, t_start: datetime, t_end: datetime,
                    *, collector: str | None = None) -> Iterator[dict[str, Any]]:
    """Every heartbeat record in the window, oldest first.

    Malformed lines are skipped rather than raised on. A truncated final line
    is the normal signature of a process killed mid-write, and refusing to
    read the file because of it would defeat the purpose.
    """
    day = t_start.date()
    while day <= t_end.date():
        path = _day_file(root, datetime(day.year, day.month, day.day,
                                        tzinfo=timezone.utc))
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if collector and rec.get("collector") != collector:
                    continue
                t = datetime.fromtimestamp(rec["t"], tz=timezone.utc)
                if t_start <= t <= t_end:
                    rec["_t"] = t
                    yield rec
        day += timedelta(days=1)


def sessions(root: Path, t_start: datetime, t_end: datetime,
             *, collector: str | None = None) -> list[Session]:
    """Reconstruct collector runs from the log."""
    acc: dict[str, dict[str, Any]] = {}

    for rec in read_heartbeats(root, t_start, t_end, collector=collector):
        sid = rec.get("session", "?")
        s = acc.setdefault(sid, {
            "collector": rec.get("collector", "?"),
            "started": rec["_t"], "last": rec["_t"],
            "ended": None, "polls": 0, "failures": 0,
        })
        s["last"] = max(s["last"], rec["_t"])
        if rec["event"] == "start":
            s["started"] = min(s["started"], rec["_t"])
        elif rec["event"] == "poll":
            s["polls"] += 1
            if not rec.get("ok", True):
                s["failures"] += 1
        elif rec["event"] == "stop":
            s["ended"] = rec["_t"]

    return sorted(
        (Session(sid, s["collector"], s["started"], s["last"], s["ended"],
                 s["polls"], s["failures"]) for sid, s in acc.items()),
        key=lambda s: s.started,
    )


def uptime_intervals(root: Path, t_start: datetime, t_end: datetime,
                     *, collector: str | None = None
                     ) -> list[tuple[datetime, datetime]]:
    """Merged windows during which a collector was demonstrably running."""
    spans = [(s.started, s.ended or s.last_seen)
             for s in sessions(root, t_start, t_end, collector=collector)]
    if not spans:
        return []

    merged = [spans[0]]
    for a, b in spans[1:]:
        if a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def blind_intervals(root: Path, t_start: datetime, t_end: datetime,
                    *, collector: str | None = None,
                    min_seconds: float = 60.0
                    ) -> list[tuple[datetime, datetime]]:
    """When we were NOT looking. The complement of uptime_intervals.

    This is the function that matters. Any apparent silence falling inside one
    of these windows is OURS, and has to be excluded before a gap detector is
    allowed to draw a conclusion from it.

    min_seconds ignores the sub-minute seams between consecutive sessions --
    a restart is not an outage worth modelling.
    """
    up = uptime_intervals(root, t_start, t_end, collector=collector)
    if not up:
        return [(t_start, t_end)]

    blind: list[tuple[datetime, datetime]] = []
    cursor = t_start
    for a, b in up:
        if a > cursor:
            blind.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < t_end:
        blind.append((cursor, t_end))

    return [(a, b) for a, b in blind
            if (b - a).total_seconds() >= min_seconds]


def was_collecting(root: Path, t: datetime,
                   *, collector: str | None = None) -> bool:
    """Were we watching at this instant?

    The question core.detectors.gaps must ask before believing a silence.
    """
    window = timedelta(hours=1)
    return any(a <= t <= b for a, b in
               uptime_intervals(root, t - window, t + window,
                                collector=collector))


# --------------------------------------------------------------------------
# one collector at a time
# --------------------------------------------------------------------------

def _process_alive(pid: int) -> bool:
    """Is this PID a running process?

    os.kill(pid, 0) is the POSIX idiom and is WRONG on Windows -- there
    os.kill ignores the signal and calls TerminateProcess, so the liveness
    check would kill the very process it is asking about. Windows gets
    OpenProcess + GetExitCodeProcess instead.

    "COULD NOT OPEN IT" IS NOT "IT IS NOT THERE".

    OpenProcess returns NULL for two unrelated reasons, and only GetLastError
    separates them: ERROR_INVALID_PARAMETER means no such process, and
    ERROR_ACCESS_DENIED means the process is running and we are not allowed
    to touch it. Until 2026-09-23 both returned False.

    That is not a cosmetic bug. CollectorLock.acquire() calls this to decide
    whether a held lock is a corpse it may take over. Once the collectors ran
    as SYSTEM -- which is the whole point of installing them as scheduled
    tasks -- a collector started from an ordinary shell would look at the live
    SYSTEM collector's lock, be told the process was gone, take the lock, and
    run a SECOND collector on the same feed. Two maritime collectors write
    every position twice, per-process dedup cannot see across them, and the
    reception grid then reads the median inter-report gap as half what it is.
    That is the one measurement this project cannot afford to get wrong, and
    it licenses every dark-vessel claim downstream.

    The POSIX branch below already draws this distinction -- PermissionError
    means "exists, just not ours". Windows simply never got the same care.
    """
    if pid <= 0:
        return False

    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5

        # use_last_error so ctypes captures GetLastError before any of its own
        # calls can overwrite it; restypes set because a HANDLE is pointer-
        # sized and the default c_int would truncate it on 64-bit.
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        k32.CloseHandle.argtypes = (ctypes.c_void_p,)
        k32.GetExitCodeProcess.argtypes = (ctypes.c_void_p,
                                           ctypes.POINTER(ctypes.c_ulong))

        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # Denied means it is running and out of reach. Treat it as alive:
            # refusing to start beside a process we cannot see is the safe
            # error, and stealing its lock is the unsafe one.
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists, just not ours
    except OSError:
        return False
    return True


class AlreadyRunning(RuntimeError):
    """Another collector holds the lock."""

    def __init__(self, pid: int, since: str, session: str) -> None:
        self.pid, self.since, self.session = pid, since, session
        super().__init__(
            f"Another collector is already running: pid {pid}, "
            f"session {session}, started {since}. Two collectors means double "
            f"quota spend and duplicate rows. Stop that one first, or pass "
            f"--force if you are certain it is dead."
        )


class CollectorLock:
    """Refuse to start when a collector is already collecting.

    Running two pollers is not obviously broken -- both work, both write, and
    nothing errors. You simply burn quota twice as fast and duplicate every
    row, and you may not notice for a week. That makes it exactly the kind of
    mistake worth making impossible rather than remembering not to make.

    A stale lock left by a crash is taken over automatically: if the recorded
    PID is not alive, the lock is not real.

        with CollectorLock(RAW, "aviation", session_id=hb.session_id):
            ...
    """

    def __init__(self, root: Path, collector: str, *,
                 session_id: str = "", force: bool = False) -> None:
        self.path = Path(root) / COLLECTOR_DIR / f"{collector}.lock"
        self.collector = collector
        self.session_id = session_id
        self.force = force

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        if self.path.exists() and not self.force:
            try:
                held = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                held = {}
            pid = int(held.get("pid", 0))
            if pid and pid != os.getpid() and _process_alive(pid):
                raise AlreadyRunning(pid, held.get("iso", "?"),
                                     held.get("session", "?"))
            # Otherwise the holder is gone; the lock is a leftover.

        self.path.write_text(json.dumps({
            "pid": os.getpid(),
            "session": self.session_id,
            "collector": self.collector,
            "t": int(datetime.now(timezone.utc).timestamp()),
            "iso": datetime.now(timezone.utc).isoformat(),
        }), encoding="utf-8")

    def release(self) -> None:
        try:
            if self.path.exists():
                held = json.loads(self.path.read_text(encoding="utf-8"))
                if int(held.get("pid", 0)) == os.getpid():
                    self.path.unlink()
        except (OSError, json.JSONDecodeError):
            pass          # a lock we cannot clean is not worth crashing over

    def __enter__(self) -> "CollectorLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def running_collectors(root: Path) -> list[dict[str, Any]]:
    """Every collector currently holding a lock, with liveness checked.

    What `collector.ps1 status` uses to answer "am I double-polling".
    """
    out = []
    d = Path(root) / COLLECTOR_DIR
    if not d.exists():
        return out
    for lock in sorted(d.glob("*.lock")):
        try:
            held = json.loads(lock.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        held["alive"] = _process_alive(int(held.get("pid", 0)))
        held["lock"] = str(lock)
        out.append(held)
    return out
