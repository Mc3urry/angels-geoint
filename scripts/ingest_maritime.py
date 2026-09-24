"""Collect live AIS into the archive, and publish a snapshot for the viewer.

    python scripts/ingest_maritime.py                     # the study box
    python scripts/ingest_maritime.py --aoi conus         # national, forever
    python scripts/ingest_maritime.py --minutes 5 -v      # a look, then stop

WHY THIS EXISTS

The bulk AIS this project analyses is MarineCadastre's, which lands months
late. Everything retrospective is built on it and will stay built on it. What
it cannot do is answer a question about this week, and it cannot be paired
with a Sentinel-1 pass taken yesterday.

So this collector listens to aisstream and writes what it hears in the SAME
SCHEMA as the bulk files -- one row per position report, ais.COLUMNS. The
entire existing maritime stack then runs on live-collected data unchanged:
clip_ais, the reception grid, the matcher, the boundary analysis. Nothing had
to be taught a second format. See adapters/maritime/archive.py.

ON STORING WHAT THE STREAM SENDS

aisstream's own documentation says events are "not durably replayed" and that
you must "persist messages your application cannot afford to lose" -- storage
is the expected use, not a tolerated one, and no retention or storage limit is
published. The underlying AIS transmissions are a public safety broadcast,
and the same positions are already in the CC0 bulk files this project
archives. Checked 2026-09-23; if that changes, this script is the one place
to stop.

TWO OUTPUTS, ONE SOCKET

    data/raw/<dataset>/hour=.../ais_*.parquet   the archive
    data/interim/live/<dataset>.json            the latest table, for the API

The second is the same trick the aviation collector uses. aisstream allows
three subscriptions per account and the API server holds one of its own; more
importantly, the API's socket starts COLD every time uvicorn restarts, and a
cold AIS table looks exactly like an empty sea for the three minutes an
anchored vessel takes to report. A collector that has been listening for
hours hands the viewer a warm table instantly. Run this and the sea view
stops having a cold start at all.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import logging.handlers
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.adapters.maritime import aisstream
from angels.adapters.maritime.archive import AISArchive, row_from_vessel
from angels.config import AOI_SEA, AOI_SEA_CONUS, INTERIM, RAW
from angels.core.uptime import AlreadyRunning, CollectorLock, HeartbeatLog

log = logging.getLogger("ingest")

# Mirrors config.AOIS for the sea. Not folded into it because those entries
# carry a poll interval and a credit cost, and this feed is pushed and free --
# writing a 600 s "interval" beside a socket would be a lie in a config file.
BOXES = {
    "sea": {"box": AOI_SEA, "dataset": "maritime-live",
            "collector": "maritime", "label": "Chesapeake-Delaware"},
    "conus": {"box": AOI_SEA_CONUS, "dataset": "maritime-live-conus",
              "collector": "maritime-conus", "label": "US waters"},
}

# How often the table is written out for the viewer. The socket updates it
# continuously; this only decides how stale the API's copy can be, and five
# seconds is already finer than the ten-second poll the browser makes.
SNAPSHOT_EVERY_S = 5.0

# How often this collector records that it was alive and listening.
#
# THE AVIATION COLLECTOR GETS THIS FOR FREE and this one does not, which is
# why it was missing until 2026-09-23. A poller writes a heartbeat per poll
# because polling IS the act of asking; a socket is asked once and then just
# sits there, so there is no natural moment at which to claim to be alive.
#
# It needs one anyway, and for exactly the reason in core/uptime.py: an hour
# with no vessels is either a quiet sea or a dead collector, and after the
# fact there is no way to tell. The maritime reception grid draws the same
# distinction the aviation one does, and until now it had nothing to draw it
# from -- so a silent socket would have been published as an empty sea.
#
# One minute, matching the existing progress line, so the two agree.
HEARTBEAT_EVERY_S = 60.0


def write_snapshot(path: Path, snap, bbox) -> bool:
    """The current table, atomically, for the API to read.

    Same contract as opensky.write_snapshot: temp file then replace, and a
    failure is swallowed rather than allowed to interrupt collection. On
    Windows the replace can lose a race with a reader; that costs one
    snapshot out of thousands and the next one is five seconds away.
    """
    import json

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "written": time.time(),
            "bbox": list(bbox),
            "listening_s": snap.listening_s,
            "connected": snap.connected,
            "warming": snap.warming,
            "n_messages": snap.n_messages,
            "last_message_age_s": snap.last_message_age_s,
            "discovery_per_min": snap.discovery_per_min,
            "peak_discovery_per_min": snap.peak_discovery_per_min,
            "n_typed": snap.n_typed,
            "n_heard_static": snap.n_heard_static,
            "by_subtype": snap.by_subtype,
            "static_parts": snap.static_parts,
            "features": snap.features,
        }), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError as exc:
        log.debug("snapshot not written: %s", exc)
        return False


async def collect(args, hb: HeartbeatLog) -> int:
    spec = BOXES[args.aoi]
    box = tuple(spec["box"])
    archive = AISArchive(RAW, dataset=spec["dataset"],
                         flush_rows=args.flush_rows,
                         flush_seconds=args.flush_seconds)
    archive.last_flush = time.time()
    snap_path = INTERIM / "live" / f"{spec['dataset']}.json"

    stream = aisstream.Stream(box)
    await stream.start()
    log.info("listening to %s  %s", spec["label"], box)
    log.info("archive -> %s", RAW / spec["dataset"])
    log.info("snapshot -> %s", snap_path)
    log.info("session %s", hb.session_id)
    hb.start(bbox=list(box), region=spec["label"], dataset=spec["dataset"])

    t_end = None if args.minutes is None else time.time() + args.minutes * 60
    last_snap = 0.0
    last_report = time.time()
    last_messages = 0

    try:
        while True:
            await asyncio.sleep(1.0)
            now = time.time()

            # THE ARCHIVE IS BUILT FROM THE TABLE, not from the socket
            # callback, so that one code path decides what a vessel's current
            # state is. A row is queued only when the vessel's OWN timestamp
            # has moved on, which is what makes this a record of reports
            # rather than a resampling of them -- see archive.py.
            for v in list(stream._vessels.values()):
                archive.add(row_from_vessel(v, now), now)

            if archive.due(now):
                path = archive.flush(now)
                if path is not None:
                    log.info("wrote %s rows -> %s",
                             f"{archive.n_written:,}", path.name)

            if now - last_snap >= SNAPSHOT_EVERY_S:
                last_snap = now
                write_snapshot(snap_path, stream.snapshot(), box)

            if now - last_report >= HEARTBEAT_EVERY_S:
                last_report = now
                s = stream.snapshot()
                log.info("%s vessels, %s messages, %s rows archived, "
                         "%s pending, %s dup",
                         f"{len(s.features):,}", f"{s.n_messages:,}",
                         f"{archive.n_written:,}", f"{archive.pending:,}",
                         f"{archive.n_duplicates:,}")
                if not s.connected:
                    log.warning("socket is not connected: %s", s.error)

                # WHAT ARRIVED IN THIS MINUTE, not how big the table is.
                #
                # The table is an accumulation: vessels stay in it long after
                # their last report, so a socket that died five minutes ago
                # still describes a busy sea. Logging len(features) as the
                # heartbeat's `n` would make a dead feed look healthy for as
                # long as the table takes to age out -- the exact confusion
                # this log exists to prevent, reintroduced at the one point
                # that is supposed to resolve it.
                #
                # Messages since the last heartbeat is the honest number: it
                # is zero the moment nothing is arriving, whatever the table
                # still holds.
                arrived = s.n_messages - last_messages
                last_messages = s.n_messages
                hb.poll(ok=bool(s.connected), n=arrived,
                        error=None if s.connected else str(s.error)[:200])

            if t_end is not None and now >= t_end:
                break
    finally:
        # A collector stopped with an unwritten buffer has lost it, and a
        # push feed has no backfill -- so the last act is always a flush.
        with contextlib.suppress(Exception):
            path = archive.flush(time.time())
            if path is not None:
                log.info("final flush -> %s", path.name)
        await stream.stop()
        # A session with a start and no stop is a crash, and that is itself
        # information: it says the silence began abruptly rather than by
        # choice. A --minutes run that reached its end is the only clean
        # stop this collector has; one told to run forever never stops on
        # its own, so anything else ending it came from outside.
        hb.stop(reason="complete" if t_end is not None and time.time() >= t_end
                else "signal")

    log.info("stopped: %s rows in %s file(s), %s duplicates dropped",
             f"{archive.n_written:,}", archive.n_files,
             f"{archive.n_duplicates:,}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--aoi", choices=sorted(BOXES), default="sea",
                    help="which water to listen to (default sea)")
    ap.add_argument("--minutes", type=float, default=None,
                    help="stop after this long; default is forever")
    ap.add_argument("--flush-rows", type=int, default=20_000,
                    help="write a file once this many rows are buffered")
    ap.add_argument("--flush-seconds", type=float, default=120.0,
                    help="...or this long since the last write, whichever first")
    ap.add_argument("--force", action="store_true",
                    help="take the lock even if another copy holds it")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--log-file", type=Path, default=None,
                    help="also log here, rotating at 5 MB")
    args = ap.parse_args()

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            args.log_file, maxBytes=5_000_000, backupCount=3))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers)

    print(f"\n  ANGELS maritime collector  ({args.aoi})")
    print(f"  {datetime.now(UTC):%Y-%m-%d %H:%M} UTC  "
          f"{'forever' if args.minutes is None else f'{args.minutes:g} min'}"
          f"  -- Ctrl-C to stop\n")
    # ONE COLLECTOR PER BOX, enforced rather than remembered.
    #
    # Two copies of this is not obviously broken: both connect, both write,
    # nothing errors. What you get is every position archived twice, and the
    # dedup that protects the cadence measurement is PER PROCESS -- so the
    # duplicates land in different files and no later pass can tell them from
    # genuine re-reports. The reception grid would then read a feed twice as
    # attentive as it is, which is the one error this project cannot afford.
    # It also spends a second of aisstream's three connections for nothing.
    #
    # Per collector NAME, so sea and sea-conus side by side is allowed and
    # two of either is not.
    # Built before the lock so the lock file can carry the session id, the
    # way the aviation collector's does -- it is what ties a lock on disk to
    # the run that wrote it in the heartbeat log.
    hb = HeartbeatLog(RAW, collector=BOXES[args.aoi]["collector"],
                      interval_s=HEARTBEAT_EVERY_S)
    lock = CollectorLock(RAW, BOXES[args.aoi]["collector"],
                         session_id=hb.session_id, force=args.force)
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        print(f"\n  {exc}\n")
        return 2

    try:
        return asyncio.run(collect(args, hb))
    except KeyboardInterrupt:
        print("\n  stopped by hand\n")
        return 0
    except aisstream.MissingCredentials as exc:
        print(f"\n  {exc}\n")
        return 2
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
