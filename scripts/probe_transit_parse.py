"""What a GTFS-Realtime feed costs once it is parsed, and when it really moves.

    .\\tasks.ps1 roads                           # the dependency, once
    python scripts/probe_transit_parse.py        # WMATA bus, 10 polls
    python scripts/probe_transit_parse.py --polls 30 --interval 10

Writes a measurement into data/reference/roads/. Vehicle positions are held
in memory to size them and are never written out.

TWO QUESTIONS, ONE RUN

**How big is the archive going to be.** `probe_transit_feed.py` measured the
wire: 73,838 bytes a poll, stable within one per cent, which is 203 MiB per
feed per day at 30 seconds. Everything this project has collected since
September is about a gigabyte, so storing raw bodies for even the two WMATA
feeds would add half the existing archive daily, and 191 feeds would be
1.11 TiB a month. The archive cannot hold bodies. It can hold the six or
seven columns the analysis needs, the way the air collector already keeps
parquet rather than raw adsb.fi JSON -- and the ratio between those two is
what decides whether the continental arm is possible.

**When does the feed actually change.** The digest method could not answer
this and said so: two runs, at 30 s and 5 s, both landed at the resolution
floor. The reason is a limit of the method. Identical bodies prove nothing
changed; **different bodies do not prove something did**, because a header
`timestamp` written at generation makes every response unique whether or not
a bus moved.

Parsing separates those. The feed header has one timestamp and every vehicle
carries its own, so this run can distinguish:

    header moves, vehicles do not   the server regenerates on request, and
                                    the digest method was measuring that
    vehicles move too               the fleet really is updating that fast
    vehicle stamps lag the header   the real freshness, which is what sets
                                    a sensible poll interval

The third is the useful one. If a vehicle's position is already 40 seconds
old when it arrives, polling every 5 seconds collects the same stale fix
eight times and calls it eight observations.

WHAT IT DOES NOT DO

It does not keep positions. It sizes them and discards them. This is a
measurement of cost and cadence, not a collection run -- G2 is the
collection run, and it will be written against these numbers rather than
against a guess, which is the whole reason this file exists.
"""

from __future__ import annotations

import argparse
import io
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import REFERENCE, WMATA_API_KEY, WMATA_KEY_HEADER
from scripts.probe_transit_feed import FEEDS, POLLABLE_FEEDS

OUT_DIR = REFERENCE / "roads"
UA = "ANGELS-capstone/1 (+github.com/Mc3urry) python-urllib"
TIMEOUT_S = 30.0
SECONDS_PER_DAY = 86_400

# The columns a segment-speed analysis needs, and nothing else. Every field
# dropped here is a field the archive does not carry for every vehicle for
# every poll for a month, which is where the size goes.
COLUMNS = ("vehicle_id", "trip_id", "route_id", "lat", "lon", "bearing",
           "speed", "vehicle_ts", "current_status", "stop_id", "fetched_at")

# How many polls go into one file. The air collector flushes every five
# minutes, which at a 30-second poll is ten, and the first version of this
# measurement sized ONE poll per file -- a unit the collector will never
# write. Across ten polls the same vehicle ids, trip ids and route ids repeat
# ten times, and dictionary encoding is exactly what parquet does with that.
DEFAULT_FLUSH = 10


def need_bindings():
    """Import the protobuf bindings, or say how to get them and stop.

    Not a traceback. `tasks.ps1` exists because a bare `pip install` on this
    machine goes to ArcGIS Pro's interpreter, finds no wheel, and fails in a
    way that reads as the project being broken.
    """
    try:
        from google.transit import gtfs_realtime_pb2  # noqa: PLC0415
        return gtfs_realtime_pb2
    except ImportError:
        print("\n  gtfs-realtime-bindings is not installed.\n")
        print("    .\\tasks.ps1 roads\n")
        print("  Not a bare pip: on this machine `python` is ArcGIS Pro's")
        print("  3.14t, which has no wheels for it.\n")
        return None


def fetch(url: str, key: str, header: str) -> tuple[bytes | None, dict]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if key:
        req.add_header(header, key)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = resp.read()
            return body, {"ok": True, "status": resp.status,
                          "raw_bytes": len(body),
                          "elapsed_s": round(time.monotonic() - started, 3)}
    except Exception as exc:                # noqa: BLE001 - reported, not raised
        return None, {"ok": False, "error_class": type(exc).__name__,
                      "reason": str(exc)[:200],
                      "elapsed_s": round(time.monotonic() - started, 3),
                      "cause": "NOT INFERRED"}


def rows_from(msg) -> tuple[list[dict], int, int]:
    """(rows, header_timestamp, entities). One row per positioned vehicle."""
    header_ts = int(getattr(msg.header, "timestamp", 0) or 0)
    rows: list[dict] = []
    for ent in msg.entity:
        if not ent.HasField("vehicle"):
            continue
        v = ent.vehicle
        if not v.HasField("position"):
            continue
        p = v.position
        rows.append({
            "vehicle_id": v.vehicle.id or ent.id,
            "trip_id": v.trip.trip_id or "",
            "route_id": v.trip.route_id or "",
            "lat": float(p.latitude),
            "lon": float(p.longitude),
            # Absent is not zero. A bearing of 0.0 is due north and a bearing
            # that was never sent is unknown; collapsing them would put every
            # silent vehicle on a northward heading.
            "bearing": float(p.bearing) if p.HasField("bearing") else None,
            "speed": float(p.speed) if p.HasField("speed") else None,
            "vehicle_ts": int(v.timestamp) if v.HasField("timestamp") else None,
            "current_status": int(v.current_status)
            if v.HasField("current_status") else None,
            "stop_id": v.stop_id or "",
        })
    return rows, header_ts, len(msg.entity)


def parquet_bytes(rows: list[dict], *, sort: bool = False) -> int:
    """How many bytes these rows occupy written the way the archive writes.

    Measured through pyarrow rather than estimated, and written to memory
    rather than to disk, because a number this decision rests on should not
    also depend on a filesystem.

    `sort` clusters each vehicle's rows together before writing. Parquet
    encodes a sorted, repeating column far better than a shuffled one, and
    whether that is worth doing is a question to measure rather than assume
    -- which is the entire reason this file exists.
    """
    import pyarrow as pa                     # noqa: PLC0415
    import pyarrow.parquet as pq             # noqa: PLC0415
    if not rows:
        return 0
    if sort:
        rows = sorted(rows, key=lambda r: (r["vehicle_id"],
                                           r.get("fetched_at") or 0))
    table = pa.Table.from_pylist(rows)
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression="snappy")
    return len(sink.getvalue())


def summarise(polls: list[dict], interval_s: float) -> dict:
    """Pure. Cost, cadence and freshness from what the polls recorded."""
    ok = [p for p in polls if p.get("ok") and p.get("parsed")]
    out: dict = {"polls_attempted": len(polls), "polls_parsed": len(ok),
                 "poll_interval_s": interval_s, "flags": []}
    if not ok:
        out["flags"].append("NOTHING PARSED: no measurement here, only an "
                            "outage or a dependency that is missing")
        return out

    raw = sorted(p["raw_bytes"] for p in ok)
    pqs = sorted(p["parquet_bytes"] for p in ok)
    out["raw_bytes_median"] = raw[len(raw) // 2]
    out["parquet_bytes_median"] = pqs[len(pqs) // 2]
    out["compression_ratio"] = round(
        out["raw_bytes_median"] / max(out["parquet_bytes_median"], 1), 2)
    out["vehicles_median"] = sorted(p["vehicles"] for p in ok)[len(ok) // 2]
    out["entities_median"] = sorted(p["entities"] for p in ok)[len(ok) // 2]
    out["bytes_per_vehicle_parquet"] = round(
        out["parquet_bytes_median"] / max(out["vehicles_median"], 1), 1)

    # Does the header move on its own, and do the vehicles corroborate it?
    #
    # REFINED 2026-10-06. The first version flagged a header that changed
    # every poll as ambiguous -- the feed regenerating, or a timestamp
    # written at generation -- and left it there. But the vehicles answer it:
    # if most of them also changed, the header is moving because the fleet
    # is, and there is nothing left to warn about. A flag that keeps firing
    # after the evidence has settled the question is rule 16's false alarm,
    # and this one was firing on every single run.
    heads = [p["header_ts"] for p in ok]
    distinct_heads = len(set(heads))
    out["distinct_header_timestamps"] = distinct_heads
    fracs = [p["vehicles_changed"] / max(p["vehicles"], 1) for p in ok
             if p.get("vehicles_changed") is not None and p.get("vehicles")]
    corroborated = statistics.fmean(fracs) >= 0.5 if fracs else None
    if distinct_heads == len(heads) and len(heads) > 2:
        if corroborated:
            out["header_corroborated_by_vehicles"] = True
            out["note_on_header"] = (
                "the header timestamp changes every poll AND most vehicles "
                "change with it, so the feed is regenerating because the "
                "fleet is moving. Not a stamped header, and the digest-based "
                "cadence estimates were measuring something real.")
        else:
            out["flags"].append(
                "THE HEADER TIMESTAMP CHANGES EVERY POLL WHILE THE VEHICLES "
                "DO NOT. That is a timestamp written at generation, and every "
                "digest-based cadence estimate about this feed was measuring "
                "the server rather than the fleet")
    if distinct_heads == 1 and len(heads) > 2:
        out["flags"].append(
            "THE HEADER TIMESTAMP NEVER CHANGED across every poll, so the "
            "feed is static, stale, or cached in front of us")

    # Do the vehicles move, and how fresh are they when they arrive?
    moved = [p["vehicles_changed"] for p in ok[1:] if p.get("vehicles_changed")
             is not None]
    if moved:
        out["vehicles_changed_median"] = sorted(moved)[len(moved) // 2]
        out["vehicles_changed_fraction"] = round(
            out["vehicles_changed_median"] / max(out["vehicles_median"], 1), 3)
    lags = [l for p in ok for l in p.get("lag_samples", [])]
    if lags:
        lags.sort()
        out["position_age_s"] = {
            "p10": lags[int(0.10 * (len(lags) - 1))],
            "median": lags[len(lags) // 2],
            "p90": lags[int(0.90 * (len(lags) - 1))],
            "max": lags[-1],
        }
        out["note_on_age"] = (
            "seconds between a vehicle's own timestamp and the feed header. "
            "If the median age exceeds the poll interval, a faster poll "
            "collects the same stale fix repeatedly and counts it as new "
            "observations.")

    # Batched, which is the unit the collector writes.
    flushes = [p for p in polls if p.get("flush")]
    if flushes:
        per_poll = [f["flush"]["bytes"] / f["flush"]["polls"] for f in flushes]
        per_poll_s = [f["flush"]["bytes_sorted"] / f["flush"]["polls"]
                      for f in flushes]
        out["batched"] = {
            "polls_per_file": flushes[0]["flush"]["polls"],
            "files": len(flushes),
            "bytes_per_poll": int(statistics.fmean(per_poll)),
            "bytes_per_poll_sorted": int(statistics.fmean(per_poll_s)),
            "ratio_vs_raw": round(out["raw_bytes_median"]
                                  / max(statistics.fmean(per_poll), 1), 2),
            "ratio_vs_raw_sorted": round(out["raw_bytes_median"]
                                         / max(statistics.fmean(per_poll_s), 1),
                                         2),
            "gain_over_per_poll": round(
                out["parquet_bytes_median"]
                / max(statistics.fmean(per_poll), 1), 2),
            "note": "one file per flush, which is what the collector writes. "
                    "The per-poll figure above sizes a file the collector "
                    "will never produce.",
        }
        if out["batched"]["gain_over_per_poll"] < 1.05:
            out["flags"].append(
                "BATCHING BOUGHT ALMOST NOTHING: the repeated identifiers "
                "are not compressing across polls the way dictionary "
                "encoding should. Check the column types before concluding "
                "the feed is incompressible")

    # Project from the batched figure when there is one: the projection has
    # to be of what will actually be stored, not of a convenient unit.
    med = (out["batched"]["bytes_per_poll_sorted"] if out.get("batched")
           else out["parquet_bytes_median"])
    out["projection_parsed"] = {
        "feeds_assumed": POLLABLE_FEEDS,
        "bytes_per_poll_used": med,
        "basis": ("batched and sorted, as the collector writes"
                  if out.get("batched") else
                  "one file per poll, which the collector does NOT write"),
        "per_interval": {
            str(iv): {
                "requests_per_day": int(SECONDS_PER_DAY / iv) * POLLABLE_FEEDS,
                "gib_per_day": round(int(SECONDS_PER_DAY / iv)
                                     * POLLABLE_FEEDS * med / (1024 ** 3), 2),
            } for iv in (15, 30, 60, 120, 300)
        },
        "note": "one feed's parsed size times the full pollable list. The "
                "real list is smaller and feeds vary, so this is an order of "
                "magnitude and not a forecast.",
    }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--feed", default="mdb-1850", choices=sorted(FEEDS))
    ap.add_argument("--polls", type=int, default=10)
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--flush", type=int, default=DEFAULT_FLUSH,
                    help=f"polls per parquet file, as the collector writes "
                         f"them (default {DEFAULT_FLUSH})")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    pb = need_bindings()
    if pb is None:
        return 1

    feed = FEEDS[args.feed]
    key = WMATA_API_KEY if feed["env"] == "WMATA_API_KEY" else ""
    if not key:
        print(f"\n  {feed['env']} is not set. Nothing is measured without it.\n")
        return 1

    print(f"\n  {feed['what']}")
    print(f"  {args.polls} polls, {args.interval:.0f} s apart")
    print(f"  key: {feed['env']} in a {WMATA_KEY_HEADER} header "
          f"(never printed or stored)\n")

    polls: list[dict] = []
    prev: dict[str, tuple] | None = None
    buf: list[dict] = []          # rows awaiting a flush
    buf_polls = 0
    for i in range(args.polls):
        body, rec = fetch(feed["url"], key, WMATA_KEY_HEADER)
        if body is not None:
            try:
                msg = pb.FeedMessage()
                msg.ParseFromString(body)
                rows, header_ts, entities = rows_from(msg)
                fetched = int(time.time())
                for r in rows:
                    r["fetched_at"] = fetched
                rec.update({
                    "parsed": True, "header_ts": header_ts,
                    "entities": entities, "vehicles": len(rows),
                    "parquet_bytes": parquet_bytes(rows),
                })
                buf.extend(rows)
                buf_polls += 1
                if buf_polls >= args.flush:
                    rec["flush"] = {
                        "polls": buf_polls, "rows": len(buf),
                        "bytes": parquet_bytes(buf),
                        "bytes_sorted": parquet_bytes(buf, sort=True),
                    }
                    buf, buf_polls = [], 0
                # Freshness: a vehicle's own stamp against the header's.
                rec["lag_samples"] = [header_ts - r["vehicle_ts"]
                                      for r in rows
                                      if r["vehicle_ts"] and header_ts
                                      and 0 <= header_ts - r["vehicle_ts"]
                                      < 3600][:400]
                now = {r["vehicle_id"]: (r["lat"], r["lon"], r["vehicle_ts"])
                       for r in rows}
                rec["vehicles_changed"] = (
                    None if prev is None else
                    sum(1 for k, v in now.items() if prev.get(k) != v))
                prev = now
            except Exception as exc:        # noqa: BLE001
                rec.update({"parsed": False,
                            "parse_error": f"{type(exc).__name__}: "
                                           f"{str(exc)[:120]}"})
        polls.append(rec)

        if rec.get("parsed"):
            ch = rec.get("vehicles_changed")
            print(f"  {i + 1:>3}/{args.polls}  raw {rec['raw_bytes']:>7,}  "
                  f"parquet {rec['parquet_bytes']:>7,}  "
                  f"{rec['vehicles']:>5} vehicles  "
                  f"{'' if ch is None else f'{ch:>5} moved'}")
        else:
            print(f"  {i + 1:>3}/{args.polls}  FAILED  "
                  f"{rec.get('reason') or rec.get('parse_error')}")
        if i < args.polls - 1:
            time.sleep(args.interval)

    s = summarise(polls, args.interval)
    print(f"\n  parsed {s['polls_parsed']} of {s['polls_attempted']}")
    if s.get("parquet_bytes_median"):
        print(f"  raw      median {s['raw_bytes_median']:>9,} bytes")
        print(f"  parquet  median {s['parquet_bytes_median']:>9,} bytes   "
              f"{s['compression_ratio']}x smaller")
        print(f"  {s['vehicles_median']} vehicles of {s['entities_median']} "
              f"entities, {s['bytes_per_vehicle_parquet']} bytes each")
        if "vehicles_changed_fraction" in s:
            print(f"  between polls, {s['vehicles_changed_median']} vehicles "
                  f"changed ({s['vehicles_changed_fraction']:.0%})")
        if "position_age_s" in s:
            a = s["position_age_s"]
            print(f"  position age  p10 {a['p10']}s  median {a['median']}s  "
                  f"p90 {a['p90']}s  max {a['max']}s")
        if s.get("batched"):
            b = s["batched"]
            print(f"\n  batched {b['polls_per_file']} polls to a file, "
                  f"{b['files']} file(s):")
            print(f"    per poll          {b['bytes_per_poll']:>8,} bytes   "
                  f"{b['ratio_vs_raw']}x raw")
            print(f"    per poll, sorted  {b['bytes_per_poll_sorted']:>8,} "
                  f"bytes   {b['ratio_vs_raw_sorted']}x raw")
            print(f"    against one file per poll: "
                  f"{b['gain_over_per_poll']}x better")
        print(f"\n  {POLLABLE_FEEDS} feeds, stored parsed, per day")
        print(f"  (basis: {s['projection_parsed']['basis']}):")
        for iv, v in s["projection_parsed"]["per_interval"].items():
            print(f"    every {iv:>4} s   {v['gib_per_day']:>7.2f} GiB")
    for f in s["flags"]:
        print(f"  FLAG  {f}")

    stamp = datetime.now(timezone.utc)
    out = args.out or (OUT_DIR / f"transit-parsed-{args.feed}-"
                                 f"{stamp.strftime('%Y-%m-%dT%H%M%SZ')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    for p in polls:
        p.pop("lag_samples", None)
    out.write_text(json.dumps({
        "_what": "cost and cadence of one GTFS-Realtime feed, parsed",
        "_feed": args.feed, "_feed_what": feed["what"], "_url": feed["url"],
        "_auth": f"{feed['env']} in a {WMATA_KEY_HEADER} header; no value "
                 f"recorded anywhere",
        "_columns_kept": list(COLUMNS),
        "summary": s, "polls": polls,
    }, indent=1) + "\n", encoding="utf-8")
    print(f"\n  wrote {out}\n")
    return 0 if s["polls_parsed"] else 1


if __name__ == "__main__":
    sys.exit(main())
