"""Poll OpenSky and append raw state vectors to Parquet.

Leave this running. While the historical access application sits in OpenSky's
review queue, this accumulates your own archive -- so you are not blocked on
their approval, and by the end of the week you have real multi-day data to
build tracks and detectors against.

    python scripts/ingest_aviation.py                    # DC box, forever
    python scripts/ingest_aviation.py --aoi conus        # national, forever
    python scripts/ingest_aviation.py --interval 15      # gentler on quota
    python scripts/ingest_aviation.py --once             # single poll, exit
    python scripts/ingest_aviation.py --minutes 60       # run for an hour

Ctrl+C flushes whatever is buffered and exits cleanly.

Rows are stored EXACTLY as the API returned them, partitioned by hour. Storing
raw rather than translated means a bug in the field mapping can be fixed and
re-run against the archive instead of re-fetched -- and you cannot re-fetch a
moment that has passed.

TWO COLLECTORS, ONE SCRIPT

--aoi selects a footprint from config.AOIS. Each carries its own box, archive
directory, collector name and default interval, so the two runs never collide:
separate lock files, separate heartbeat streams, separate Parquet trees.

    air     DC-Baltimore     ~2 sq deg     30 s    1 credit/poll
    conus   continental US ~1450 sq deg   600 s    4 credits/poll

Run both. Depth where the detectors need to sample a turn rate, breadth where
they only need to know where things were.

QUOTA. OpenSky bills by box AREA in four coarse steps, not by aircraft
returned, so the national box costs four credits against the metro box's one.
A registered account gets 4,000/day refilling hourly. The pair above spends
3,456. Before changing any interval, ask config.daily_credits() rather than
guessing -- overspending does not raise, it just 429s every poll after the
bucket empties, leaving a hole at the same time each day that looks exactly
like a diurnal pattern.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


# Re-runs this script under the interpreter that has ANGELS installed, if the
# one invoking it does not. See scripts/_bootstrap.py.
#
# The name check matters. `import _bootstrap` only resolves when scripts/ is on
# sys.path, which is true when this file is RUN and false when the test suite
# IMPORTS it as scripts.<name>. Swallowing every ModuleNotFoundError here would
# also swallow the one _bootstrap raises about 'angels' itself -- turning a
# clear "wrong interpreter" message back into a confusing one.
try:
    import _bootstrap  # noqa: F401  (must precede the angels imports)
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

# Third-party imports come AFTER the bootstrap, never before: _bootstrap
# re-executes this script under the project interpreter, and an import
# placed above it runs first -- under whatever Python the user typed --
# and dies with ModuleNotFoundError before the switch can happen.
import pyarrow as pa
import pyarrow.parquet as pq
from dotenv import load_dotenv

from angels.adapters.aviation.opensky import (
    TokenManager,
    fetch_states,
    write_snapshot,
)
from angels.config import AOIS, LIVE, RAW, daily_credits, opensky_credits
from angels.core.uptime import AlreadyRunning, CollectorLock, HeartbeatLog

log = logging.getLogger("ingest")

# Raw state vectors, stored flat. Names match the index constants in
# adapters/aviation/opensky.py.
SCHEMA = pa.schema([
    ("fetched_at", pa.timestamp("s", tz="UTC")),   # when WE asked
    ("icao24", pa.string()),
    ("callsign", pa.string()),
    ("origin_country", pa.string()),
    ("time_position", pa.int64()),
    ("last_contact", pa.int64()),
    ("longitude", pa.float64()),
    ("latitude", pa.float64()),
    ("baro_altitude", pa.float64()),
    ("on_ground", pa.bool_()),
    ("velocity", pa.float64()),
    ("true_track", pa.float64()),
    ("vertical_rate", pa.float64()),
    ("geo_altitude", pa.float64()),
    ("squawk", pa.string()),
    ("spi", pa.bool_()),
    ("position_source", pa.int32()),
])

_COLUMNS = [f.name for f in SCHEMA][1:]      # everything but fetched_at
_INDICES = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16]


def rows_to_table(fetched_at: datetime, rows: list[list]) -> pa.Table:
    """Raw rows to an Arrow table, dropping only the sensors list (12)."""
    cols: dict[str, list] = {"fetched_at": [fetched_at] * len(rows)}
    for name, idx in zip(_COLUMNS, _INDICES):
        cols[name] = [r[idx] if idx < len(r) else None for r in rows]
    return pa.Table.from_pydict(cols, schema=SCHEMA)


def write_partition(table: pa.Table, when: datetime, root: Path,
                    dataset: str = "aviation") -> Path:
    """One file per flush, partitioned by hour.

    Hour partitioning is not decoration -- it is what lets DuckDB skip files it
    does not need when you query a time range later. A single growing file
    would mean scanning the whole archive for every query.

    `dataset` keeps the two footprints in separate trees. Merging them would
    be worse than untidy: the national feed samples twenty times slower, so
    any rate computed over the union -- reports per hour, gap length, track
    continuity -- would be a weighted average of two incomparable things and
    would not look wrong while being wrong.
    """
    part = root / dataset / f"hour={when:%Y%m%d%H}"
    part.mkdir(parents=True, exist_ok=True)
    path = part / f"states_{when:%Y%m%dT%H%M%S}.parquet"
    pq.write_table(table, path, compression="snappy")
    return path


class Ingester:
    def __init__(self, bbox, root: Path, interval: float, flush_every: int,
                 *, dataset: str = "aviation", collector: str = "aviation",
                 label: str = "", snapshot: Path | None = None):
        self.bbox = bbox
        self.root = root
        self.interval = interval
        self.flush_every = flush_every
        self.dataset = dataset
        self.collector = collector
        self.label = label or collector
        # Where to publish each poll for the viewer. Optional so tests and
        # one-off runs write nothing outside their own archive directory.
        self.snapshot = snapshot
        self.tokens = TokenManager()
        self.buffer: list[pa.Table] = []
        self.polls = 0
        self.rows = 0
        self.errors = 0
        self.running = True
        # Records that we were alive and asking, separately from what came
        # back. Without it, a missing hour is indistinguishable from an hour
        # in which every aircraft went dark -- and you cannot work out which
        # after the fact.
        #
        # Keyed by collector name, so the two footprints keep independent
        # uptime histories. They will genuinely differ -- one can 429 while
        # the other is fine -- and a shared log would make the national box's
        # downtime look like the metro box's.
        self.hb = HeartbeatLog(root, collector=collector, interval_s=interval)

    def poll_once(self) -> int:
        t, rows = fetch_states(self.bbox, self.tokens)
        if self.snapshot is not None:
            # Written for EVERY successful poll, including an empty one. An
            # empty sky that was asked about is a result; a snapshot that
            # stops updating is a collector that stopped asking, and the
            # viewer tells those apart by the file's age.
            write_snapshot(self.snapshot, t, rows, self.bbox)
        if rows:
            self.buffer.append(rows_to_table(t, rows))
            self.rows += len(rows)
        self.polls += 1
        return len(rows)

    def flush(self) -> Path | None:
        if not self.buffer:
            return None
        table = pa.concat_tables(self.buffer)
        path = write_partition(table, datetime.now(timezone.utc), self.root,
                               self.dataset)
        log.info("wrote %d rows -> %s", table.num_rows, path.name)
        self.buffer.clear()
        return path

    def stop(self, *_) -> None:
        log.info("stopping, flushing buffer...")
        self.running = False

    def run(self, max_polls: int | None = None) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        cost = opensky_credits(self.bbox)
        log.info("polling %s every %.0fs -> %s",
                 self.label, self.interval, self.root / self.dataset)
        log.info("%d credit(s)/poll, ~%d credits/day",
                 cost, daily_credits(self.bbox, self.interval))
        log.info("session %s", self.hb.session_id)
        self.hb.start(bbox=list(self.bbox), region=self.label,
                      dataset=self.dataset, credits_per_poll=cost)

        try:
            while self.running:
                try:
                    n = self.poll_once()
                    self.hb.poll(ok=True, n=n)
                    log.info("poll %d: %d aircraft", self.polls, n)
                except Exception as exc:
                    # Never let one bad poll end a multi-day run. A transient
                    # 5xx or a dropped connection at hour 40 should cost you
                    # ten seconds, not the whole archive.
                    #
                    # A FAILED poll is still a heartbeat: we were awake and we
                    # asked. That distinguishes "the API refused us" from
                    # "the laptop was asleep", which are different stories
                    # about the same empty hour.
                    self.errors += 1
                    self.polls += 1
                    self.hb.poll(ok=False, error=str(exc)[:200])
                    log.warning("poll failed (%d total): %s", self.errors, exc)

                if self.polls and self.polls % self.flush_every == 0:
                    self.flush()
                if max_polls and self.polls >= max_polls:
                    break
                if self.running:
                    time.sleep(self.interval)
        finally:
            self.flush()
            # A session with a start and no stop is a crash, and that is
            # itself information -- it says the downtime began abruptly
            # rather than by choice.
            self.hb.stop(reason="signal" if not self.running else "complete")
            log.info("done: %d polls, %d rows, %d errors",
                     self.polls, self.rows, self.errors)


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--aoi", choices=sorted(AOIS), default="air",
                    help="which footprint to collect (default air). Each "
                         "brings its own box, archive directory, collector "
                         "name and default interval.")
    ap.add_argument("--interval", type=float, default=None,
                    help="seconds between polls. Defaults to the AOI's own "
                         "interval: 30 for air, 600 for conus.")
    ap.add_argument("--flush-every", type=int, default=30,
                    help="polls per parquet file (default 30)")
    ap.add_argument("--once", action="store_true", help="one poll, then exit")
    ap.add_argument("--minutes", type=float, help="stop after this long")
    ap.add_argument("--force", action="store_true",
                    help="start even if another collector holds the lock. "
                         "Only when you are certain that one is dead.")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--log-file", type=Path,
                    help="also write logs here, rotating at 5 MB. Required in "
                         "practice when running under Task Scheduler, which "
                         "gives the process no console to print to.")
    args = ap.parse_args()

    aoi = AOIS[args.aoi]
    interval = args.interval if args.interval is not None else aoi["interval_s"]

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        # Rotate, because this runs for months. Five files of 5 MB is a few
        # weeks of history, which is enough to answer "what happened last
        # Tuesday" without ever needing attention.
        handlers.append(logging.handlers.RotatingFileHandler(
            args.log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8"))

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )

    max_polls = 1 if args.once else None
    if args.minutes:
        max_polls = max(1, int(args.minutes * 60 / interval))

    try:
        ing = Ingester(aoi["box"], RAW, interval,
                       1 if args.once else args.flush_every,
                       dataset=aoi["dataset"], collector=aoi["collector"],
                       label=aoi["label"],
                       snapshot=LIVE / f"{aoi['dataset']}.json")
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    # Two collectors ON THE SAME FOOTPRINT is not obviously broken -- both
    # work, both write, and nothing errors. You just burn quota twice and
    # duplicate every row, and you might not notice for a week. Worth making
    # impossible rather than remembering not to do.
    #
    # The lock is per collector name, so air and conus running side by side is
    # allowed and two of either is not -- which is exactly the distinction
    # that matters. A single global lock would have made the design this whole
    # change exists to enable impossible.
    lock = CollectorLock(RAW, aoi["collector"], session_id=ing.hb.session_id,
                         force=args.force)
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        log.error("%s", exc)
        return 2

    try:
        ing.run(max_polls=max_polls)
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
