"""Collect the INDEPENDENT aviation channel, and find out whether it exists.

    python scripts/ingest_adsbfi.py                 # DC box, forever
    python scripts/ingest_adsbfi.py --once          # one poll, exit
    python scripts/ingest_adsbfi.py --minutes 60    # an hour, then stop
    python scripts/ingest_adsbfi.py --dist 120      # a wider radius

WHY THIS EXISTS

The maritime half of this project compares AIS -- unauthenticated
self-reporting -- against Sentinel-1, which observes without consent. The
aviation half is meant to make the same comparison: ADS-B against MLAT and
TIS-B, positions derived from receiver geometry or uplinked from ground
radar rather than transmitted by choice.

**It has not been doing that.** 152,040 reports across 224 collected hours
carry `position_source = 0` on every single row: ADS-B, all of it. The
aviation collector has been recording one side of a two-sided question, and
`scripts/check_tisb.py` -- the probe meant to notice -- was reading the
aircraft list from the wrong key and returning a confident zero every time it
ran.

This collector is the other side. adsb.fi labels each aircraft's position
source in `type`, so both channels arrive from the SAME receiver network,
which matters more than it sounds: comparing OpenSky's ADS-B against another
feed's MLAT would make every coverage difference between the two networks
look like a behavioural one. The maritime half learned that lesson the
expensive way and called it the searched-water denominator.

MEASUREMENT BEFORE COMPARISON

One snapshot over DC on 2026-09-25 returned 157 aircraft: 156 `adsb_icao`,
1 `adsr_icao`, and no MLAT- or TIS-B-positioned targets at all. So the first
question is not "where do independent and cooperative disagree" but "is there
an independent channel here to disagree with". This script exists to answer
that honestly, and it may well answer no.

That is a result either way. An AOI with no independent aviation coverage is
a finding about what this method can and cannot see -- the same shape as the
reception grid, which exists so that an absence of AIS is only read where AIS
could have been heard.

WHAT IS STORED

Every field the feed returns, typed where it is documented and kept as JSON
where it is not, because an hour you did not collect is gone and a field you
discarded cannot be recovered from it. Partitioned by hour, like the other
collectors.

  type     the POSITION SOURCE: adsb_icao, adsr_icao, tisb_icao, mlat, ...
           Not to be confused with `t`, the airframe type code (B738).
  mlat     array naming the FIELDS on this aircraft that came from
  tisb     multilateration or TIS-B. A cooperative aircraft with one
           TIS-B-sourced field is not an independent target, and counting it
           as one would overstate the channel.

SOURCE AND COURTESY

adsb.fi open data, public endpoint, **1 request per second**, personal and
non-commercial use, attribution requested. The default interval here is 30 s,
thirty times gentler than the limit. There are no credits to spend and no
quota to exhaust -- unlike OpenSky, this collector cannot starve the archive.
Do not lower the interval below 5 s.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from angels.config import AOI_AIR, RAW
from angels.core.uptime import AlreadyRunning, CollectorLock, HeartbeatLog

log = logging.getLogger("adsbfi")

API = "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{dist}"
COLLECTOR = "aviation-independent"
DATASET = "aviation-adsbfi"
MIN_INTERVAL_S = 5.0

# Named footprints, so this collector takes the same `--aoi` interface as the
# other three and `collector.ps1` needs only a table entry rather than a new
# branch. Centre and radius rather than a box, because that is what the
# endpoint takes.
BOXES: dict[str, dict] = {
    "air-indep": {
        "lat": round((AOI_AIR[1] + AOI_AIR[3]) / 2, 4),
        "lon": round((AOI_AIR[0] + AOI_AIR[2]) / 2, 4),
        "dist": 70,
        "interval_s": 30.0,
        "label": "DC-Baltimore, independent channel",
    },
}

# `type` values that mean the position was DERIVED rather than reported.
INDEPENDENT_PREFIXES = ("tisb", "adsr")
INDEPENDENT_EXACT = ("mlat",)

# Documented fields get a column. Everything else is preserved in `extra` as
# JSON: the feed is not versioned and a field that appears next month would
# otherwise be silently dropped from an archive that cannot be refetched.
SCHEMA = pa.schema([
    ("fetched_at", pa.timestamp("s", tz="UTC")),   # when WE asked
    ("now", pa.float64()),                         # the feed's own timestamp
    ("hex", pa.string()),
    ("type", pa.string()),                         # POSITION SOURCE
    ("flight", pa.string()),
    ("r", pa.string()),                            # registration
    ("t", pa.string()),                            # airframe type code
    ("lat", pa.float64()),
    ("lon", pa.float64()),
    ("alt_baro", pa.string()),                     # int or "ground"
    ("alt_geom", pa.float64()),
    ("gs", pa.float64()),
    ("track", pa.float64()),
    ("baro_rate", pa.float64()),
    ("geom_rate", pa.float64()),
    ("squawk", pa.string()),
    ("emergency", pa.string()),
    ("category", pa.string()),
    ("nav_altitude_mcp", pa.float64()),
    ("nic", pa.int64()),
    ("rc", pa.int64()),
    ("seen_pos", pa.float64()),
    ("seen", pa.float64()),
    ("rssi", pa.float64()),
    ("messages", pa.int64()),
    ("alert", pa.int64()),
    ("spi", pa.int64()),
    ("dst", pa.float64()),
    ("dir", pa.float64()),
    ("mlat_fields", pa.string()),                  # JSON array, often "[]"
    ("tisb_fields", pa.string()),
    ("extra", pa.string()),                        # anything undocumented
])

_TYPED = {f.name for f in SCHEMA} - {
    "fetched_at", "now", "mlat_fields", "tisb_fields", "extra"}


def is_independent(kind: str | None) -> bool:
    """Was this position derived rather than self-reported?"""
    k = (kind or "").strip()
    return k.startswith(INDEPENDENT_PREFIXES) or k in INDEPENDENT_EXACT


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) \
        else None


def _int(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) \
        else None


def rows_to_table(fetched_at: datetime, now: float | None,
                  craft: list[dict]) -> pa.Table:
    cols: dict[str, list] = {f.name: [] for f in SCHEMA}
    for a in craft:
        cols["fetched_at"].append(fetched_at)
        cols["now"].append(now)
        for name in ("hex", "type", "flight", "r", "t", "squawk", "emergency",
                     "category"):
            v = a.get(name)
            cols[name].append(str(v).strip() if v is not None else None)
        cols["alt_baro"].append(
            str(a["alt_baro"]) if a.get("alt_baro") is not None else None)
        for name in ("lat", "lon", "alt_geom", "gs", "track", "baro_rate",
                     "geom_rate", "nav_altitude_mcp", "seen_pos", "seen",
                     "rssi", "dst", "dir"):
            cols[name].append(_num(a.get(name)))
        for name in ("nic", "rc", "messages", "alert", "spi"):
            cols[name].append(_int(a.get(name)))
        cols["mlat_fields"].append(json.dumps(a.get("mlat") or []))
        cols["tisb_fields"].append(json.dumps(a.get("tisb") or []))
        extra = {k: v for k, v in a.items()
                 if k not in _TYPED and k not in ("mlat", "tisb")}
        cols["extra"].append(json.dumps(extra, separators=(",", ":"))
                             if extra else None)
    return pa.Table.from_pydict(cols, schema=SCHEMA)


def write_partition(table: pa.Table, when: datetime, root: Path) -> Path:
    part = root / DATASET / f"hour={when:%Y%m%d%H}"
    part.mkdir(parents=True, exist_ok=True)
    path = part / f"aircraft_{when:%Y%m%dT%H%M%S}.parquet"
    pq.write_table(table, path, compression="zstd")
    return path


class Ingester:
    def __init__(self, lat: float, lon: float, dist: int, root: Path,
                 interval: float, flush_every: int) -> None:
        self.url = API.format(lat=lat, lon=lon, dist=dist)
        self.root = root
        self.interval = interval
        self.flush_every = flush_every
        self.buffer: list[pa.Table] = []
        self.running = True
        self.seen = {"independent": 0, "cooperative": 0, "polls_with_indep": 0}
        self.hb = HeartbeatLog(root, collector=COLLECTOR, interval_s=interval)

    def poll_once(self) -> int:
        fetched_at = datetime.now(timezone.utc).replace(microsecond=0)
        r = httpx.get(self.url, timeout=30,
                      headers={"User-Agent": "ANGELS/0.1 (capstone research; "
                                             "non-commercial)"})
        r.raise_for_status()
        doc = r.json()

        # Accept either spelling, and refuse to call a missing list empty --
        # see check_tisb.py for what that mistake cost.
        craft = None
        for key in ("aircraft", "ac"):
            if key in doc:
                craft = doc[key] or []
                break
        if craft is None:
            raise KeyError(f"no aircraft list in the response; keys were "
                           f"{sorted(doc)}")

        indep = sum(1 for a in craft if is_independent(a.get("type")))
        self.seen["independent"] += indep
        self.seen["cooperative"] += len(craft) - indep
        if indep:
            self.seen["polls_with_indep"] += 1
        self.buffer.append(rows_to_table(fetched_at, _num(doc.get("now")),
                                         craft))
        return len(craft)

    def flush(self) -> Path | None:
        if not self.buffer:
            return None
        table = pa.concat_tables(self.buffer)
        path = write_partition(table, datetime.now(timezone.utc), self.root)
        self.buffer.clear()
        return path

    def stop(self, *_) -> None:
        self.running = False

    def run(self, max_polls: int | None = None) -> None:
        self.hb.start(source="adsb.fi", url=self.url)
        polls = 0
        try:
            while self.running and (max_polls is None or polls < max_polls):
                try:
                    n = self.poll_once()
                    self.hb.poll(ok=True, n=n)
                    log.info("%3d aircraft  (independent so far: %d)",
                             n, self.seen["independent"])
                except Exception as exc:                  # noqa: BLE001
                    self.hb.poll(ok=False, error=f"{type(exc).__name__}: {exc}")
                    log.warning("poll failed: %s", exc)
                polls += 1
                if len(self.buffer) >= self.flush_every:
                    p = self.flush()
                    if p:
                        log.info("wrote %s", p.name)
                if self.running and (max_polls is None or polls < max_polls):
                    time.sleep(self.interval)
        finally:
            p = self.flush()
            if p:
                log.info("wrote %s", p.name)
            self.hb.stop(reason="clean" if self.running else "signal")
            total = self.seen["independent"] + self.seen["cooperative"]
            print(f"\n  {polls} poll(s), {total:,} aircraft reports")
            print(f"    cooperative  {self.seen['cooperative']:,}")
            print(f"    INDEPENDENT  {self.seen['independent']:,}"
                  f"   (on {self.seen['polls_with_indep']} of {polls} polls)")
            if not self.seen["independent"]:
                print("\n  No independently positioned aircraft in this run. "
                      "That is a\n  measurement, not a failure -- but it is "
                      "not yet a rate either.\n")
            else:
                print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--aoi", choices=sorted(BOXES), default="air-indep",
                    help="named footprint; --lat/--lon/--dist override it")
    ap.add_argument("--lat", type=float, default=None)
    ap.add_argument("--lon", type=float, default=None)
    ap.add_argument("--dist", type=int, default=None, help="radius, nm")
    ap.add_argument("--interval", type=float, default=0.0,
                    help="seconds between polls. Defaults to the footprint's "
                         "own (adsb.fi allows 1/s; ours is far gentler)")
    ap.add_argument("--flush-every", type=int, default=30,
                    help="polls per parquet file")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--minutes", type=float, default=None)
    ap.add_argument("--force", action="store_true",
                    help="take over a lock held by a dead process")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--log-file", type=Path,
                    help="also write logs here, rotating at 5 MB. Required in "
                         "practice under Task Scheduler, which gives the "
                         "process no console to print to.")
    args = ap.parse_args()

    box = BOXES[args.aoi]
    lat = args.lat if args.lat is not None else box["lat"]
    lon = args.lon if args.lon is not None else box["lon"]
    dist = args.dist if args.dist is not None else box["dist"]
    if args.interval <= 0:
        args.interval = box["interval_s"]

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            args.log_file, maxBytes=5_000_000, backupCount=5,
            encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers)

    if args.interval < MIN_INTERVAL_S:
        print(f"\n  --interval {args.interval:g} is below the {MIN_INTERVAL_S:g} s "
              f"floor. adsb.fi asks for\n  one request a second and gives this "
              f"data away; do not crowd it.\n")
        return 2

    ing = Ingester(lat, lon, dist, RAW, args.interval, args.flush_every)
    max_polls = 1 if args.once else (
        int(args.minutes * 60 / args.interval) if args.minutes else None)

    signal.signal(signal.SIGINT, ing.stop)
    signal.signal(signal.SIGTERM, ing.stop)

    print(f"\n  adsb.fi  {box['label']}  {lat}, {lon}  r={dist} nm  "
          f"every {args.interval:g}s")
    print(f"  -> {RAW / DATASET}\n")
    try:
        with CollectorLock(RAW, COLLECTOR, session_id=ing.hb.session_id,
                           force=args.force):
            ing.run(max_polls)
    except AlreadyRunning as exc:
        print(f"\n  {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
