"""Poll OpenSky and append raw state vectors to Parquet.

Leave this running. While the historical access application sits in OpenSky's
review queue, this accumulates your own archive -- so you are not blocked on
their approval, and by the end of the week you have real multi-day data to
build tracks and detectors against.

    python scripts/ingest_aviation.py                 # poll forever
    python scripts/ingest_aviation.py --interval 15   # gentler on quota
    python scripts/ingest_aviation.py --once          # single poll, then exit
    python scripts/ingest_aviation.py --minutes 60    # run for an hour

Ctrl+C flushes whatever is buffered and exits cleanly.

Rows are stored EXACTLY as the API returned them, partitioned by hour. Storing
raw rather than translated means a bug in the field mapping can be fixed and
re-run against the archive instead of re-fetched -- and you cannot re-fetch a
moment that has passed.

Quota: authenticated accounts get 4000+ credits per day. At 10 s intervals a
continuous run is roughly 8600 polls per day, which is over. Use --interval 30
for an unattended multi-day run, or accept gaps.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from dotenv import load_dotenv

from angels.adapters.aviation.opensky import TokenManager, fetch_states
from angels.config import AOI, AOI_NAME, RAW

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


def write_partition(table: pa.Table, when: datetime, root: Path) -> Path:
    """One file per flush, partitioned by hour.

    Hour partitioning is not decoration -- it is what lets DuckDB skip files it
    does not need when you query a time range later. A single growing file
    would mean scanning the whole archive for every query.
    """
    part = root / "aviation" / f"hour={when:%Y%m%d%H}"
    part.mkdir(parents=True, exist_ok=True)
    path = part / f"states_{when:%Y%m%dT%H%M%S}.parquet"
    pq.write_table(table, path, compression="snappy")
    return path


class Ingester:
    def __init__(self, bbox, root: Path, interval: float, flush_every: int):
        self.bbox = bbox
        self.root = root
        self.interval = interval
        self.flush_every = flush_every
        self.tokens = TokenManager()
        self.buffer: list[pa.Table] = []
        self.polls = 0
        self.rows = 0
        self.errors = 0
        self.running = True

    def poll_once(self) -> int:
        t, rows = fetch_states(self.bbox, self.tokens)
        if rows:
            self.buffer.append(rows_to_table(t, rows))
            self.rows += len(rows)
        self.polls += 1
        return len(rows)

    def flush(self) -> Path | None:
        if not self.buffer:
            return None
        table = pa.concat_tables(self.buffer)
        path = write_partition(table, datetime.now(timezone.utc), self.root)
        log.info("wrote %d rows -> %s", table.num_rows, path.name)
        self.buffer.clear()
        return path

    def stop(self, *_) -> None:
        log.info("stopping, flushing buffer...")
        self.running = False

    def run(self, max_polls: int | None = None) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        log.info("polling %s every %.0fs -> %s",
                 AOI_NAME, self.interval, self.root / "aviation")
        try:
            while self.running:
                try:
                    n = self.poll_once()
                    log.info("poll %d: %d aircraft", self.polls, n)
                except Exception as exc:
                    # Never let one bad poll end a multi-day run. A transient
                    # 5xx or a dropped connection at hour 40 should cost you
                    # ten seconds, not the whole archive.
                    self.errors += 1
                    log.warning("poll failed (%d total): %s", self.errors, exc)

                if self.polls and self.polls % self.flush_every == 0:
                    self.flush()
                if max_polls and self.polls >= max_polls:
                    break
                if self.running:
                    time.sleep(self.interval)
        finally:
            self.flush()
            log.info("done: %d polls, %d rows, %d errors",
                     self.polls, self.rows, self.errors)


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--interval", type=float, default=10.0,
                    help="seconds between polls (default 10)")
    ap.add_argument("--flush-every", type=int, default=30,
                    help="polls per parquet file (default 30)")
    ap.add_argument("--once", action="store_true", help="one poll, then exit")
    ap.add_argument("--minutes", type=float, help="stop after this long")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    max_polls = 1 if args.once else None
    if args.minutes:
        max_polls = max(1, int(args.minutes * 60 / args.interval))

    try:
        ing = Ingester(AOI, RAW, args.interval, 1 if args.once else args.flush_every)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    ing.run(max_polls=max_polls)
    return 0


if __name__ == "__main__":
    sys.exit(main())
