"""Is everything actually running? One command, after a reboot or an update.

    python scripts/doctor.py

WHY THIS EXISTS

A Windows update restarts the machine overnight. Nothing in the repo changes,
the environment survives, the tests still pass -- and the collectors may or may
not have come back. The archive cannot be backfilled, so the cost of not
noticing is measured in days, and the only signal is an absence.

This project has been bitten by that once already: nine days of the air archive
were lost between 3 and 12 September 2026 because a task went to Ready and
waited for a logon that never came. Nothing errored anywhere.

So this asks, in order, the questions whose answers stop being recoverable:

    1. Are the collectors collecting RIGHT NOW?
    2. How much have we missed since the last time they were?
    3. Is the rest of it -- credentials, data, environment -- still here?

Question 2 is answerable only because the heartbeat log records that we were
awake and asking, separately from what came back. That is the whole reason it
exists, and a reboot is exactly the case it was written for.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import AOIS, RAW, ROOT
from angels.core.uptime import blind_intervals, running_collectors, sessions

OK, WARN, BAD = "  ok  ", " WARN ", " BAD  "


def line(state: str, what: str, detail: str = "") -> None:
    print(f"  [{state}] {what:<34}{detail}")


def check_collectors(hours: float, started: dict[str, bool] | None = None) -> int:
    """The only question whose answer expires."""
    print("\n  COLLECTORS\n")
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
    problems = 0

    live = {c.get("collector"): c for c in running_collectors(RAW)
            if c.get("alive")}
    started = {} if started is None else started

    for key, aoi in AOIS.items():
        name = aoi["collector"]
        alive = name in live

        if alive:
            line(OK, f"{key} polling", f"pid {live[name].get('pid')}")
            ses_now = sessions(RAW, now - timedelta(hours=2), now,
                               collector=name)
            if ses_now:
                up = (now - ses_now[-1].started).total_seconds() / 60
                started[key] = up < 45
        else:
            line(BAD, f"{key} NOT polling",
                 f"fix: .\\collector.ps1 install -Aoi {key}")
            problems += 1

        # How much did we miss? Not inferred from missing files -- asked of
        # the heartbeat log, which records that we were awake and asking.
        #
        # PAST GAPS ARE HISTORY, NOT WORK. A gap that has already ended cannot
        # be acted on -- the hours are gone, and the only thing to do with them
        # is exclude the window from the analysis, which run_detectors.py does
        # automatically. Counting them as "needs attention" on every run makes
        # the summary permanently non-zero, and a health check that always
        # reports problems is one you stop reading. Then the day it reports a
        # REAL one, it looks like every other day.
        #
        # So a gap counts as a problem only while it is still open.
        blind = blind_intervals(RAW, start, now, collector=name)
        lost = sum((b - a).total_seconds() for a, b in blind) / 3600
        ongoing = bool(blind) and (now - blind[-1][1]).total_seconds() < 120

        if lost > 0.05:
            pct = 100 * lost / hours
            if ongoing and not alive:
                line(BAD, f"{key} blind NOW",
                     f"{lost:.1f}h of the last {hours:.0f}h and still dark")
                problems += 1
            else:
                line(OK, f"{key} blind time (past)",
                     f"{lost:.1f}h of the last {hours:.0f}h ({pct:.0f}%) "
                     f"-- recorded, will be excluded")
            for a, b in blind[-3:]:
                gap = (b - a).total_seconds() / 3600
                print(f"          {a:%m-%d %H:%M} -> {b:%m-%d %H:%M} UTC "
                      f"({gap:.1f}h)")
        else:
            line(OK, f"{key} continuous", f"no gap in {hours:.0f}h")

        # HOW it stopped, not just that it did. A session with a start and a
        # stop was shut down; one with a start and no stop was killed. Those
        # are different problems -- a clean stop means something asked it to
        # quit (a terminal closed, a reboot, Task Scheduler), a crash means
        # the process died where it stood. Only the heartbeat log can tell
        # them apart, and only because it is written at the time.
        ses = sessions(RAW, now - timedelta(days=7), now, collector=name)
        if ses and not alive:
            last = ses[-1]
            ran = last.duration.total_seconds() / 3600
            if last.clean:
                line(WARN, f"{key} last session",
                     f"stopped cleanly after {ran:.1f}h -- something asked it to")
            else:
                line(WARN, f"{key} last session",
                     f"KILLED after {ran:.1f}h -- no stop was written")
            print(f"          last heartbeat {last.last_seen:%m-%d %H:%M} UTC, "
                  f"{last.polls:,} polls, {last.failures} failed")

    return problems


def check_archives(started_recently: dict[str, bool]) -> int:
    print("\n  ARCHIVES\n")
    problems = 0
    for key, aoi in AOIS.items():
        d = RAW / aoi["dataset"]
        files = list(d.glob("hour=*/*.parquet")) if d.exists() else []
        if not files:
            line(BAD, f"{key} archive", "empty")
            problems += 1
            continue
        mb = sum(f.stat().st_size for f in files) / 1e6
        newest = max(files, key=lambda f: f.stat().st_mtime)
        age = (datetime.now().timestamp() - newest.stat().st_mtime) / 60
        # Minutes between flushes: interval x flush_every, from collector.ps1.
        flush_every = 10 if key == "air" else 3
        due = aoi["interval_s"] * flush_every / 60

        # A collector that has only just started has not had time to flush.
        # Reporting its still-old newest file as stale would be true and
        # useless -- the first file is simply not due yet.
        if started_recently.get(key) and age > due:
            line(OK, f"{key} archive",
                 f"{len(files)} files, {mb:,.0f} MB -- first new file due "
                 f"in ~{due:.0f} min")
        elif age < due * 3:
            line(OK, f"{key} archive",
                 f"{len(files)} files, {mb:,.0f} MB, newest {age:.0f} min ago")
        else:
            line(WARN, f"{key} archive",
                 f"{len(files)} files, newest {age:.0f} min ago "
                 f"(expected one every {due:.0f} min)")
            problems += 1

    sar = RAW / "sar"
    zips = list(sar.glob("*.zip")) if sar.exists() else []
    if zips:
        gb = sum(z.stat().st_size for z in zips) / 1e9
        line(OK, "sar scenes", f"{len(zips)} slices, {gb:.1f} GB")
    else:
        line(WARN, "sar scenes", "none downloaded")

    parts = list(sar.glob("*.part")) if sar.exists() else []
    if parts:
        line(WARN, "interrupted downloads",
             f"{len(parts)} .part files -- rerun fetch_sar to resume")
    return problems


def check_environment() -> int:
    print("\n  ENVIRONMENT\n")
    problems = 0
    line(OK, "python", sys.executable)
    line(OK, "version", ".".join(map(str, sys.version_info[:3])))

    for mod in ("duckdb", "pyarrow", "httpx", "rasterio"):
        try:
            __import__(mod)
            line(OK, mod, "")
        except ImportError:
            state = WARN if mod == "rasterio" else BAD
            line(state, mod, "missing")
            problems += 1

    env = ROOT / ".env"
    if not env.exists():
        line(BAD, ".env", "missing -- collectors cannot authenticate")
        return problems + 1

    # Presence only. Never print a credential, not even truncated: this output
    # is exactly the sort of thing that gets pasted into a chat or an issue.
    wanted = ("OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET",
              "CDSE_USERNAME", "CDSE_PASSWORD", "AISSTREAM_API_KEY")
    have = {k for k in wanted if os.getenv(k)}
    for k in wanted:
        if k in have:
            line(OK, k, "set")
        else:
            line(WARN, k, "not set")
            problems += 1
    return problems


def main() -> int:
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 24.0

    print(f"\n  ANGELS doctor -- {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    print("  " + "=" * 66)

    started: dict[str, bool] = {}
    problems = check_collectors(hours, started)
    problems += check_archives(started)
    problems += check_environment()

    print("\n  " + "=" * 66)
    if problems == 0:
        print("  Everything is running.\n")
        return 0

    print(f"  {problems} thing(s) need attention.\n")
    print("  After a reboot or an update, the usual fix is:")
    print("    .\\collector.ps1 install -Aoi air")
    print("    .\\collector.ps1 install -Aoi conus")
    print()
    print("  Blind time is permanent -- those hours cannot be re-collected.")
    print("  It is recorded, though, so run_detectors.py will exclude the")
    print("  window rather than report it as a sky full of silent aircraft.\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
