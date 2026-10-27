"""G2c: poll vehicle positions from the transit feeds that straddle a state line.

    python scripts/fetch_vehicles.py --once                # one cycle, then exit
    python scripts/fetch_vehicles.py                       # every 30 s, forever
    python scripts/fetch_vehicles.py --feed mdb-1850 --once   # one feed
    python scripts/fetch_vehicles.py --list                # what would be polled

The observed half of the land domain's cooperative layer. Work zones (G1) are
what an agency DECLARES about a road; a bus's reported position is what a
vehicle on that road did. G4 turns consecutive positions into segment speeds,
and G6 compares the two at state lines.

WHICH FEEDS

Exactly the `selected` list of `data/reference/roads/border-feeds.json`, the
G2b artefact: 38 of 191 feeds, each carrying the states it touches. The list
is read, never re-derived here -- a second selection rule in a second file is
mechanism E waiting to happen. The file's own sha256 goes into every ledger
row and the heartbeat, so a change of selection is visible in the archive.

WHAT IS KEPT, AND WHAT IS NOT

Raw bodies are NOT kept. The 2026-10-05 measurement put one WMATA bus poll at
77 KB on the wire and 13.6 KB as batched, sorted parquet; bodies for 38 feeds
every 30 s would be the largest thing this project has ever stored and would
hold nothing the analysis reads. Kept: one parquet row per NEW fix, with the
columns `probe_transit_parse.py` sized, plus the feed, the entity id, the
vehicle label and the header timestamp.

**A new fix, not a new poll.** A vehicle whose (timestamp, lat, lon) has not
changed since the previous poll is the same observation fetched again, and
storing it twice would double-count it in every rate G4 computes. The ledger
records how many were repeats, so the de-duplication is visible rather than
silent. The first poll of a session has nothing to compare with: every fix is
stored and the ledger row says `baseline`. Across a restart that can repeat a
fix already archived, so **G4 de-duplicates on (feed_id, vehicle_key,
vehicle_ts) and does not assume this file did.** That sentence is the
mechanism-AA lesson applied in advance: in-memory state is empty after a
restart, and the code that reads the archive must not believe otherwise.

ONE LEDGER ROW PER FEED PER POLL

Append-only, fsynced JSON lines, for the reason G1 gave: 38 feeds behind one
heartbeat would make "answered with no vehicles" and "did not answer" the same
footprint, and G6 would read our blindness as an empty road. Outcomes, one
per distinguishable fact:

    ok              parsed GTFS-Realtime; n_vehicles may be 0 (a quiet night)
    not_positions   valid GTFS-Realtime, entities present, none positioned:
                    probably a trip-updates feed at a vehicle-positions URL
    not_gtfs_rt     bytes arrived whole and are not a FeedMessage
    truncated       OUR cap cut it off
    http_error      the server answered with a status >= 400
    fetch_error     no status at all: DNS, timeout, refused. The cause is
                    recorded as the exception's class and NOT INFERRED
    key_missing     the feed needs a credential this machine does not have.
                    We did not ask, so it is our gap and not their silence

PROTOBUF IS LENIENT, which is the trap in `not_gtfs_rt`. An HTML error page
can fail to parse, but a short run of arbitrary bytes can parse "successfully"
into an empty message. A body is accepted only if it parses AND carries a
header with a `gtfs_realtime_version`. The test suite holds both cases.

CREDENTIALS

Only WMATA's two feeds need one, sent as the `api_key` HEADER from
`angels.config` -- never in the URL, which would put it in the ledger, the log
and the selection artefact. No other host is sent anything but a User-Agent.

STORAGE IS NOT YET MEASURED

The 2026-10-05 projection (191 feeds, WMATA-sized) was 7 GiB/day at 30 s.
38 feeds, most of them far smaller than WMATA's 831 buses, should be a
fraction of that; how large a fraction is what the first hours of the ledger
(`bytes_received`) and the parquet partition sizes will say. Measure, then
decide whether 30 s stays -- as was done for WZDx.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import logging.handlers
import os
import signal
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    import _bootstrap  # noqa: F401  (must precede the angels imports)
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import RAW, REFERENCE, WMATA_API_KEY, WMATA_KEY_HEADER
from angels.core.uptime import AlreadyRunning, CollectorLock, HeartbeatLog

log = logging.getLogger("vehicles")

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

COLLECTOR_NAME = "roads-vehicles"
DATASET = "roads/vehicles"
SELECTION = REFERENCE / "roads" / "border-feeds.json"

# 30 s, the plan's figure. WMATA's median position age was 5 s on 2026-10-05
# (p90 9 s), so a 30 s poll is not re-collecting stale fixes on that feed;
# the ledger's n_repeat will say whether that holds on the other 37.
DEFAULT_INTERVAL_S = 30.0
INTERVAL_BAND_S = (15.0, 120.0)

# Polls per parquet file. Ten polls at 30 s is five minutes, the air
# collector's flush cadence, and the unit the 2026-10-05 sizing measured.
DEFAULT_FLUSH = 10

# Shorter than G1's 30 s: a cycle has to fit inside its interval, and the
# fetches run in parallel so one slow agency cannot hold the others.
TIMEOUT_S = 20.0
MAX_WORKERS = 12
CAP_BYTES = 32 * 1024 * 1024

USER_AGENT = ("angels-geoint/0.1 (senior capstone, GIS; contact via "
              "github.com/Mc3urry/angels-geoint)")

# host -> (header name, the config attribute holding its value). Looked up at
# call time, so a test can see that a missing key is refused without a key
# ever being present.
KEYED_HOSTS: dict[str, tuple[str, str]] = {
    "api.wmata.com": (WMATA_KEY_HEADER, "WMATA_API_KEY"),
}

OK = "ok"
NOT_POSITIONS = "not_positions"
NOT_GTFS_RT = "not_gtfs_rt"
TRUNCATED = "truncated"
HTTP_ERROR = "http_error"
FETCH_ERROR = "fetch_error"
KEY_MISSING = "key_missing"
OUTCOMES = (OK, NOT_POSITIONS, NOT_GTFS_RT, TRUNCATED, HTTP_ERROR,
            FETCH_ERROR, KEY_MISSING)
# Did the agency hand us a readable answer. key_missing is NOT in either
# list: we never asked, so it is neither their answer nor their silence.
ANSWERED = (OK, NOT_POSITIONS)
NOT_ASKED = (KEY_MISSING,)

# The parquet columns, in order. Absent is None, never 0: a bearing of 0.0 is
# due north and an unsent one is unknown.
COLUMNS = ("feed_id", "vehicle_key", "vehicle_id", "entity_id", "label",
           "trip_id", "route_id", "lat", "lon", "bearing", "speed",
           "vehicle_ts", "current_status", "stop_id", "header_ts",
           "fetched_at")


# --------------------------------------------------------------------------
# the selection
# --------------------------------------------------------------------------

def load_selection(path: Path = SELECTION) -> tuple[list[dict[str, Any]], str]:
    """(feeds, sha256 of the file). Refuses a selection it cannot trust."""
    raw = Path(path).read_bytes()
    doc = json.loads(raw)
    feeds = doc.get("selected")
    if not isinstance(feeds, list) or not feeds:
        raise ValueError(f"{path}: no 'selected' list")
    if doc.get("n_selected") != len(feeds):
        raise ValueError(f"{path}: n_selected says {doc.get('n_selected')} "
                         f"but the list holds {len(feeds)}")
    ids = [f.get("id") for f in feeds]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path}: duplicate feed ids")
    for f in feeds:
        if not str(f.get("url", "")).startswith(("http://", "https://")):
            raise ValueError(f"{path}: feed {f.get('id')} has no http(s) url")
    return feeds, hashlib.sha256(raw).hexdigest()


def narrow(feeds: list[dict[str, Any]],
           wanted: tuple[str, ...]) -> list[dict[str, Any]]:
    """Feeds whose id is in `wanted`; all of them if it is empty. A name that
    matches nothing is an error, not an empty run that looks like a quiet one."""
    if not wanted:
        return list(feeds)
    by_id = {f["id"].lower(): f for f in feeds}
    missing = [w for w in wanted if w.lower() not in by_id]
    if missing:
        raise ValueError(f"no selected feed called {', '.join(missing)}")
    return [by_id[w.lower()] for w in wanted]


def key_for(url: str, config: Any = None) -> tuple[str, str] | None:
    """(header, value) for a keyed host, ("", "") when it is keyed and no value
    is configured, None when the host takes no key at all."""
    host = (urlsplit(url).hostname or "").lower()
    spec = KEYED_HOSTS.get(host)
    if spec is None:
        return None
    header, attr = spec
    if config is None:
        value = {"WMATA_API_KEY": WMATA_API_KEY}.get(attr, "")
    else:
        value = getattr(config, attr, "")
    return (header, value) if value else ("", "")


# --------------------------------------------------------------------------
# what a body says
# --------------------------------------------------------------------------

def need_bindings():
    try:
        from google.transit import gtfs_realtime_pb2  # noqa: PLC0415
        return gtfs_realtime_pb2
    except ImportError:
        return None


def looks_like_html(body: bytes) -> bool:
    head = body[:512].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def parse(body: bytes, pb2=None):
    """A FeedMessage, or None if these bytes are not one.

    Parsing alone proves little: protobuf accepts many short byte strings as
    a valid empty message. A GTFS-Realtime feed always carries a header with
    its specification version, so that is required too."""
    pb2 = pb2 or need_bindings()
    if pb2 is None:
        raise RuntimeError("gtfs-realtime-bindings is not installed: "
                           ".\\tasks.ps1 roads")
    if not body or looks_like_html(body):
        return None
    msg = pb2.FeedMessage()
    try:
        msg.ParseFromString(body)
    except Exception:                       # noqa: BLE001 - the answer is "no"
        return None
    if not msg.HasField("header") or not msg.header.gtfs_realtime_version:
        return None
    return msg


def rows_from(msg, feed_id: str, fetched_at: int) -> tuple[list[dict], int]:
    """(one row per positioned vehicle, entities that were not positioned)."""
    header_ts = int(msg.header.timestamp) if msg.header.HasField(
        "timestamp") else None
    rows: list[dict] = []
    unpositioned = 0
    for ent in msg.entity:
        if not ent.HasField("vehicle") or not ent.vehicle.HasField("position"):
            unpositioned += 1
            continue
        v = ent.vehicle
        p = v.position
        vid = v.vehicle.id if v.HasField("vehicle") else ""
        rows.append({
            "feed_id": feed_id,
            # The descriptor's id when the agency sends one; otherwise the
            # entity id, PREFIXED, because some vendors renumber entities on
            # every poll and a track built on them is not a vehicle.
            "vehicle_key": vid or f"entity:{ent.id}",
            "vehicle_id": vid,
            "entity_id": ent.id,
            "label": v.vehicle.label if v.HasField("vehicle") else "",
            "trip_id": v.trip.trip_id if v.HasField("trip") else "",
            "route_id": v.trip.route_id if v.HasField("trip") else "",
            "lat": float(p.latitude),
            "lon": float(p.longitude),
            "bearing": float(p.bearing) if p.HasField("bearing") else None,
            "speed": float(p.speed) if p.HasField("speed") else None,
            "vehicle_ts": int(v.timestamp) if v.HasField("timestamp") else None,
            "current_status": int(v.current_status)
            if v.HasField("current_status") else None,
            "stop_id": v.stop_id,
            "header_ts": header_ts,
            "fetched_at": fetched_at,
        })
    return rows, unpositioned


def classify(status: int | None, body: bytes | None, truncated: bool,
             msg, n_positioned: int, n_entities: int) -> str:
    if status is None:
        return FETCH_ERROR
    if status >= 400:
        return HTTP_ERROR
    if truncated:
        return TRUNCATED
    if msg is None:
        return NOT_GTFS_RT
    if n_entities and not n_positioned:
        return NOT_POSITIONS
    return OK


def fix_of(row: dict) -> tuple:
    return (row["vehicle_ts"], row["lat"], row["lon"])


def new_fixes(rows: list[dict], previous: dict[str, tuple] | None
              ) -> tuple[list[dict], dict[str, tuple]]:
    """(rows that are new observations, the state to compare the next poll to).

    `previous` None means this feed has not been read this session: every
    fix is kept, and the caller marks the row as a baseline."""
    current = {r["vehicle_key"]: fix_of(r) for r in rows}
    if previous is None:
        return list(rows), current
    fresh = [r for r in rows if previous.get(r["vehicle_key"]) != fix_of(r)]
    return fresh, current


def feed_row(feed: dict[str, Any], *, status: int | None, body: bytes | None,
             elapsed_s: float, error: str | None, truncated: bool,
             fetched_at: int, previous: dict[str, tuple] | None,
             pb2=None, key_missing: bool = False
             ) -> tuple[dict[str, Any], list[dict], dict[str, tuple] | None]:
    """(ledger row, rows to store, new comparison state). Pure: no network,
    no clock -- `fetched_at` is passed in."""
    base: dict[str, Any] = {
        "t": fetched_at,
        "iso": datetime.fromtimestamp(fetched_at, timezone.utc).isoformat(),
        "feed": feed["id"], "provider": feed.get("provider", ""),
        "states": feed.get("states"),
    }
    if key_missing:
        base.update(outcome=KEY_MISSING, returned=False, status=None,
                    elapsed_s=0.0, bytes_received=0)
        return base, [], previous

    msg = None
    rows: list[dict] = []
    unpositioned = 0
    n_entities = 0
    if status is not None and status < 400 and not truncated and body:
        msg = parse(body, pb2)
        if msg is not None:
            n_entities = len(msg.entity)
            rows, unpositioned = rows_from(msg, feed["id"], fetched_at)
    outcome = classify(status, body, truncated, msg, len(rows), n_entities)
    base.update(outcome=outcome, returned=outcome in ANSWERED, status=status,
                elapsed_s=round(elapsed_s, 3),
                bytes_received=len(body or b""))
    if error:
        base["error"] = error[:200]
    if outcome not in ANSWERED:
        # A failed poll leaves the comparison state alone: the next good poll
        # is compared with the last good one, not treated as a baseline.
        return base, [], previous

    fresh, state = new_fixes(rows, previous)
    ages = [r["header_ts"] - r["vehicle_ts"] for r in rows
            if r["header_ts"] is not None and r["vehicle_ts"] is not None]
    base.update(
        header_ts=rows[0]["header_ts"] if rows else (
            int(msg.header.timestamp) if msg.header.HasField("timestamp")
            else None),
        n_entities=n_entities,
        n_vehicles=len(rows),
        n_unpositioned=unpositioned,
        n_new=len(fresh),
        n_repeat=len(rows) - len(fresh),
        n_no_vehicle_id=sum(1 for r in rows if not r["vehicle_id"]),
        n_no_vehicle_ts=sum(1 for r in rows if r["vehicle_ts"] is None),
        baseline=previous is None,
        position_age_s_median=statistics.median(ages) if ages else None,
    )
    return base, fresh, state


# --------------------------------------------------------------------------
# the cycle, as a sentence and a health verdict
# --------------------------------------------------------------------------

def cycle_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    by = {o: sorted(r["feed"] for r in records if r["outcome"] == o)
          for o in OUTCOMES}
    asked = [r for r in records if r["outcome"] not in NOT_ASKED]
    answered = [r for r in records if r["returned"]]
    return {
        "n_feeds": len(records),
        "n_asked": len(asked),
        "n_answered": len(answered),
        "n_vehicles": sum(r.get("n_vehicles", 0) for r in answered),
        "n_new": sum(r.get("n_new", 0) for r in answered),
        "n_repeat": sum(r.get("n_repeat", 0) for r in answered),
        "feeds_by_outcome": {o: f for o, f in by.items() if f},
        # OK with nobody moving. not_positions also has n_vehicles == 0 and
        # is a different fact, named separately.
        "feeds_quiet": sorted(r["feed"] for r in answered
                              if r["outcome"] == OK
                              and r.get("n_vehicles") == 0),
        "bytes_received": sum(r.get("bytes_received", 0) for r in records),
    }


def cycle_health(s: dict[str, Any]) -> tuple[bool, str | None]:
    """Our outage or their silence. Every feed we asked failing at once is
    ours (the 70 offline minutes of 2026-10-23 looked exactly like that); one
    feed failing is a fact about that feed and the cycle is still a
    measurement."""
    if s["n_asked"] == 0:
        return False, "no feed was asked: every one lacks a credential"
    if s["n_answered"] == 0:
        return False, f"all {s['n_asked']} feeds asked failed"
    return True, None


def cycle_sentence(s: dict[str, Any]) -> str:
    out = (f"{s['n_answered']} of {s['n_asked']} feeds asked answered, "
           f"{s['n_vehicles']:,} vehicles, {s['n_new']:,} new fixes")
    if s["n_repeat"]:
        out += f" ({s['n_repeat']:,} unchanged since last poll)"
    if s["feeds_quiet"]:
        out += f"; reporting no vehicles: {', '.join(s['feeds_quiet'])}"
    fails = {o: f for o, f in s["feeds_by_outcome"].items()
             if o not in ANSWERED and o not in NOT_ASKED}
    for o, feeds in sorted(fails.items()):
        out += f"; {o}: {', '.join(feeds)}"
    if s["feeds_by_outcome"].get(NOT_POSITIONS):
        out += (f"; answered without positions: "
                f"{', '.join(s['feeds_by_outcome'][NOT_POSITIONS])}")
    if s["feeds_by_outcome"].get(KEY_MISSING):
        out += (f"; NOT ASKED, no credential configured: "
                f"{', '.join(s['feeds_by_outcome'][KEY_MISSING])}")
    return out


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def ledger_path(root: Path, when: datetime) -> Path:
    return Path(root) / DATASET / f"attempts-{when:%Y-%m-%d}.jsonl"


def append_ledger(root: Path, records: list[dict[str, Any]],
                  when: datetime) -> Path:
    path = ledger_path(root, when)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return path


def write_positions(rows: list[dict], root: Path, when: datetime) -> Path:
    """One parquet file per flush, hour-partitioned like every other archive
    here so a time-range query skips what it does not need."""
    import pyarrow as pa                    # noqa: PLC0415
    import pyarrow.parquet as pq            # noqa: PLC0415
    schema = pa.schema([
        ("feed_id", pa.string()), ("vehicle_key", pa.string()),
        ("vehicle_id", pa.string()), ("entity_id", pa.string()),
        ("label", pa.string()), ("trip_id", pa.string()),
        ("route_id", pa.string()), ("lat", pa.float64()),
        ("lon", pa.float64()), ("bearing", pa.float32()),
        ("speed", pa.float32()), ("vehicle_ts", pa.int64()),
        ("current_status", pa.int8()), ("stop_id", pa.string()),
        ("header_ts", pa.int64()), ("fetched_at", pa.int64()),
    ])
    assert tuple(schema.names) == COLUMNS
    # Sorted by feed then vehicle: the 2026-10-05 sizing found sorting worth
    # 11 per cent, because runs of one vehicle's rows compress together.
    rows = sorted(rows, key=lambda r: (r["feed_id"], r["vehicle_key"],
                                       r["fetched_at"]))
    table = pa.Table.from_pylist(rows, schema=schema)
    part = Path(root) / DATASET / f"hour={when:%Y%m%d%H}"
    part.mkdir(parents=True, exist_ok=True)
    path = part / f"positions_{when:%Y%m%dT%H%M%S}.parquet"
    pq.write_table(table, path, compression="zstd")
    return path


# --------------------------------------------------------------------------
# the network, the only impure part
# --------------------------------------------------------------------------

def fetch(url: str, key: tuple[str, str] | None
          ) -> tuple[int | None, bytes | None, float, str | None, bool]:
    """(status, body, elapsed_s, error, truncated)."""
    headers = {"User-Agent": USER_AGENT,
               "Accept": "application/x-protobuf, */*;q=0.1"}
    if key and key[0]:
        headers[key[0]] = key[1]
    req = urllib.request.Request(url, headers=headers)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = resp.read(CAP_BYTES + 1)
            return (resp.status, body[:CAP_BYTES], time.monotonic() - t0,
                    None, len(body) > CAP_BYTES)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(4096)
        except Exception:                   # noqa: BLE001
            body = b""
        return exc.code, body, time.monotonic() - t0, f"HTTP {exc.code}", False
    except Exception as exc:                # noqa: BLE001 - recorded, not raised
        return (None, None, time.monotonic() - t0,
                f"{type(exc).__name__}: {exc}"[:200], False)


class Poller:
    def __init__(self, feeds: list[dict[str, Any]], root: Path,
                 interval: float, flush_every: int, *,
                 selection_sha: str = "") -> None:
        self.feeds = feeds
        self.root = Path(root)
        self.interval = interval
        self.flush_every = max(1, flush_every)
        self.selection_sha = selection_sha
        self.pb2 = need_bindings()
        self.state: dict[str, dict[str, tuple] | None] = {
            f["id"]: None for f in feeds}
        self.buffer: list[dict] = []
        self.cycles = 0
        self.rows_written = 0
        self.files = 0
        self.running = True
        self.hb = HeartbeatLog(root, collector=COLLECTOR_NAME,
                               interval_s=interval)

    def _one(self, feed: dict[str, Any], fetched_at: int):
        key = key_for(feed["url"])
        if key == ("", ""):
            return feed_row(feed, status=None, body=None, elapsed_s=0.0,
                            error=None, truncated=False,
                            fetched_at=fetched_at,
                            previous=self.state[feed["id"]], pb2=self.pb2,
                            key_missing=True)
        status, body, elapsed, error, truncated = fetch(feed["url"], key)
        return feed_row(feed, status=status, body=body, elapsed_s=elapsed,
                        error=error, truncated=truncated,
                        fetched_at=fetched_at,
                        previous=self.state[feed["id"]], pb2=self.pb2)

    def cycle(self) -> dict[str, Any]:
        when = datetime.now(timezone.utc)
        fetched_at = int(when.timestamp())
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            results = list(pool.map(lambda f: self._one(f, fetched_at),
                                    self.feeds))
        records = []
        for feed, (rec, fresh, state) in zip(self.feeds, results):
            rec["selection_sha256"] = self.selection_sha
            self.state[feed["id"]] = state
            self.buffer.extend(fresh)
            records.append(rec)
        append_ledger(self.root, records, when)
        self.cycles += 1
        if self.cycles % self.flush_every == 0:
            self.flush()
        s = cycle_summary(records)
        ok, error = cycle_health(s)
        self.hb.poll(ok=ok, n=s["n_vehicles"], error=error)
        return s

    def flush(self) -> Path | None:
        if not self.buffer:
            return None
        path = write_positions(self.buffer, self.root,
                               datetime.now(timezone.utc))
        log.info("wrote %d fixes -> %s", len(self.buffer), path.name)
        self.rows_written += len(self.buffer)
        self.files += 1
        self.buffer = []
        return path

    def stop(self, *_: object) -> None:
        log.info("stopping after this cycle...")
        self.running = False

    def run(self, max_cycles: int | None = None) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        log.info("%d feed(s) every %.0fs -> %s", len(self.feeds),
                 self.interval, self.root / DATASET)
        log.info("session %s", self.hb.session_id)
        self.hb.start(feeds=len(self.feeds), dataset=DATASET,
                      selection_sha256=self.selection_sha)
        try:
            while self.running:
                started = time.monotonic()
                try:
                    s = self.cycle()
                    log.info("cycle %d: %s", self.cycles, cycle_sentence(s))
                except Exception as exc:    # noqa: BLE001
                    self.cycles += 1
                    self.hb.poll(ok=False, n=0, error=str(exc)[:200])
                    log.warning("cycle failed: %s", exc)
                if max_cycles and self.cycles >= max_cycles:
                    break
                # Sleep the REST of the interval, so the cadence is the
                # interval and not the interval plus however long the
                # slowest agency took to answer.
                rest = self.interval - (time.monotonic() - started)
                if self.running and rest > 0:
                    time.sleep(rest)
        finally:
            # The buffer holds at most flush_every polls of fixes. Written on
            # every exit the process gets to see; a hard kill loses it, and
            # the ledger's n_new for those polls then exceeds the archive by
            # exactly that much, which is how the loss can be measured.
            try:
                self.flush()
            finally:
                self.hb.stop(reason="signal" if not self.running
                             else "complete")
                log.info("done: %d cycle(s), %d fixes in %d file(s)",
                         self.cycles, self.rows_written, self.files)


# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S,
                    help=f"seconds between cycles "
                         f"(default {DEFAULT_INTERVAL_S:.0f})")
    ap.add_argument("--flush-every", type=int, default=DEFAULT_FLUSH,
                    help=f"cycles per parquet file (default {DEFAULT_FLUSH})")
    ap.add_argument("--once", action="store_true", help="one cycle, then exit")
    ap.add_argument("--minutes", type=float, help="stop after this long")
    ap.add_argument("--feed", action="append", default=[],
                    help="only this feed id; repeatable")
    ap.add_argument("--list", action="store_true",
                    help="print the feeds that would be polled, and exit")
    ap.add_argument("--selection", type=Path, default=SELECTION)
    ap.add_argument("--root", type=Path, default=RAW)
    ap.add_argument("--force", action="store_true",
                    help="take the collector lock even if one is held")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--log-file", type=Path,
                    help="also write logs here, rotating at 5 MB (the house "
                         "collector contract: Task Scheduler gives the "
                         "process no console)")
    args = ap.parse_args(argv)

    root_log = logging.getLogger()
    root_log.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s  %(message)s",
                                           datefmt="%H:%M:%S"))
    root_log.addHandler(console)
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        to_file = logging.handlers.RotatingFileHandler(
            args.log_file, maxBytes=5_000_000, backupCount=5,
            encoding="utf-8")
        to_file.setFormatter(logging.Formatter(
            "%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        root_log.addHandler(to_file)

    feeds, sha = load_selection(args.selection)
    feeds = narrow(feeds, tuple(args.feed))
    if args.list:
        for f in feeds:
            k = key_for(f["url"])
            note = ("" if k is None else
                    "  [key: configured]" if k[0] else "  [key: MISSING]")
            print(f"{f['id']:14s} {f.get('provider', '')[:44]:44s}"
                  f" {','.join(f.get('state_names') or [])}{note}")
        print(f"{len(feeds)} feed(s), selection sha256 {sha[:12]}")
        return 0

    if need_bindings() is None:
        log.error("gtfs-realtime-bindings is not installed: .\\tasks.ps1 roads"
                  "  (not a bare pip; on this machine `python` is ArcGIS "
                  "Pro's interpreter)")
        return 3

    lo, hi = INTERVAL_BAND_S
    if not args.once and not (lo <= args.interval <= hi):
        log.warning("interval %.0fs is outside the %.0f-%.0fs band this "
                    "poller was designed for", args.interval, lo, hi)
    log.info("selection %s, sha256 %s", args.selection.name, sha[:12])

    poller = Poller(feeds, args.root, args.interval, args.flush_every,
                    selection_sha=sha)
    max_cycles = 1 if args.once else None
    if args.minutes:
        max_cycles = max(1, int(args.minutes * 60 / args.interval))

    lock = CollectorLock(args.root, COLLECTOR_NAME,
                         session_id=poller.hb.session_id, force=args.force)
    try:
        with lock:
            poller.run(max_cycles)
    except AlreadyRunning as exc:
        log.error("%s", exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
