"""How much does one GTFS-Realtime feed actually cost, and how often is it
worth asking.

    python scripts/probe_transit_feed.py                    # WMATA bus, 20 min
    python scripts/probe_transit_feed.py --minutes 60
    python scripts/probe_transit_feed.py --interval 10 --minutes 5
    python scripts/probe_transit_feed.py --feed mdb-1853    # rail instead

Writes a measurement into data/reference/roads/ and prints a summary. The
feed's CONTENTS are never stored -- only sizes, timings and digests.

WHY THIS EXISTS BEFORE THE COLLECTOR DOES

G0 selected 189 keyless vehicle-position feeds and 2 credentialed ones. At a
30-second poll that is 191 times 2,880, which is **550,080 requests a day**,
and the only size estimate anyone had was a guess of "about 50 kB a
protobuf". Multiply the guess out and the continental arm is tens of
gigabytes a day, which is not a thing a laptop does; halve the guess and it
might be. A plan resting on a factor-of-ten guess is not a plan.

So this measures one feed. WMATA bus, because the key exists and the study
area depends on that feed more than any other.

THE SECOND QUESTION, WHICH MATTERS AS MUCH AS SIZE

**How often does the feed actually change?** Polling every 30 seconds a feed
that refreshes every 60 buys nothing but bytes, and polling every 30 a feed
that refreshes every 10 loses two thirds of the movement. Both errors are
invisible in the archive afterwards: the first looks like a dense record and
the second looks like a sparse world.

It is measured here **without parsing the protobuf**, by hashing each
response and asking how often the digest changes. Two identical bodies mean
the feed did not update between them, whatever its header claims about
itself. That also keeps this script dependency-free: `gtfs-realtime-bindings`
is a decision for G2, and a measurement that forces an install before it can
report is a measurement nobody runs.

THE LIMIT OF THAT METHOD, WHICH TWO RUNS FOUND

Identical bodies prove nothing changed. **Different bodies do not prove
something did.** A GTFS-Realtime header carries a `timestamp`, and a server
that stamps it with the moment of generation returns a different body to
every request whether or not a single vehicle moved. Under that behaviour
this method measures how often the server regenerates and reports it as how
often the fleet moves, which is the wrong quantity with the right units --
and nothing in the output would say so.

Two runs against WMATA bus, at 30 s and at 5 s, both landed at the
resolution floor: 0 per cent and 6 per cent duplicates. The handful of
duplicates at 5 s is weak evidence against a per-request timestamp, since a
stamped feed would give none at all -- but weak evidence is what it is.

**Resolving this needs the header read, which needs the protobuf parsed.**
So does keeping the archive small enough to exist, because storing raw
bodies at 203 MiB per feed per day is not affordable and filtering to the
columns the analysis needs requires decoding them first. Two independent
questions arriving at the same dependency is the reason it stops being a
preference.

WHAT IT REFUSES TO DO

It does not log the key, record its length, or put it anywhere. The artefact
says which environment variable was used and which header it went in. It
sends the key in a header and never in a query string -- a credential in a
url reaches the artefact, the manifest and every log, which this project
learned on 2026-10-05 by doing it.
"""

from __future__ import annotations

import argparse
import hashlib
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

OUT_DIR = REFERENCE / "roads"
UA = "ANGELS-capstone/1 (+github.com/Mc3urry) python-urllib"
TIMEOUT_S = 30.0
MAX_BYTES = 16 * 1024 * 1024

# Named rather than passed as a url, so that what is being measured is a
# matter of record and not of whatever was typed that afternoon.
FEEDS = {
    "mdb-1850": {
        "what": "WMATA Metrobus vehicle positions",
        "url": "https://api.wmata.com/gtfs/bus-gtfsrt-vehiclepositions.pb",
        "env": "WMATA_API_KEY",
    },
    "mdb-1853": {
        "what": "WMATA Metrorail vehicle positions",
        "url": "https://api.wmata.com/gtfs/rail-gtfsrt-vehiclepositions.pb",
        "env": "WMATA_API_KEY",
    },
}

# What the whole measurement is for: the cost of the real feed list at
# several plausible intervals.
POLLABLE_FEEDS = 191
SECONDS_PER_DAY = 86_400
INTERVALS_TO_PROJECT = (15, 30, 60, 120, 300)


def poll_once(url: str, key: str, header: str) -> dict:
    """One GET. Returns sizes and a digest, never a body."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if key:
        req.add_header(header, key)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = resp.read(MAX_BYTES + 1)
            truncated = len(body) > MAX_BYTES
            body = body[:MAX_BYTES]
            return {
                "t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "ok": True,
                "status": resp.status,
                "bytes": len(body),
                "truncated": truncated,
                "elapsed_s": round(time.monotonic() - started, 3),
                "sha256": hashlib.sha256(body).hexdigest(),
                "content_type": resp.headers.get("Content-Type", ""),
                # A protobuf starts with a field tag, not with '<' or '{'.
                # An error page served with HTTP 200 is the failure this
                # project has now met three times.
                "looks_binary": not body.lstrip()[:1] in (b"<", b"{", b"["),
            }
    except urllib.error.HTTPError as exc:
        return {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "ok": False, "status": exc.code, "reason": exc.reason,
                "elapsed_s": round(time.monotonic() - started, 3),
                "cause": "NOT INFERRED -- a status code is a fact and its "
                         "cause is an inference"}
    except Exception as exc:                # noqa: BLE001 - reported, not raised
        return {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "ok": False, "status": None,
                "error_class": type(exc).__name__, "reason": str(exc)[:200],
                "elapsed_s": round(time.monotonic() - started, 3),
                "cause": "NOT INFERRED"}


def summarise_polls(polls: list[dict], interval_s: float) -> dict:
    """Size, update cadence and projected cost. Pure: no network, no clock.

    The cadence is derived from DIGEST CHANGES, not from the feed's own
    header. Two identical bodies mean nothing moved between them, whatever
    the feed says about itself, and that is the only version of this question
    that can be answered without parsing.
    """
    ok = [p for p in polls if p.get("ok")]
    out: dict = {
        "polls_attempted": len(polls),
        "polls_returned": len(ok),
        "polls_failed": len(polls) - len(ok),
        "poll_interval_s": interval_s,
        "flags": [],
    }
    if not ok:
        out["flags"].append("NOTHING RETURNED: every poll failed, so there is "
                            "no measurement here, only an outage")
        return out

    sizes = sorted(p["bytes"] for p in ok)
    out["bytes_min"] = sizes[0]
    out["bytes_median"] = sizes[len(sizes) // 2]
    out["bytes_max"] = sizes[-1]
    out["bytes_mean"] = int(statistics.fmean(sizes))

    if any(not p.get("looks_binary") for p in ok):
        out["flags"].append("NOT BINARY: at least one response is text, which "
                            "is what an error page served with HTTP 200 looks "
                            "like. Do not treat these sizes as feed sizes")
    if sizes[0] == 0:
        out["flags"].append("EXACT ZERO bytes in at least one response -- "
                            "instrument this before believing it")

    digests = [p["sha256"] for p in ok]
    distinct = len(set(digests))
    out["distinct_bodies"] = distinct
    out["duplicate_fraction"] = round(1.0 - distinct / len(digests), 3)

    # Gaps between CHANGES, measured in polls and converted to seconds. A
    # run of identical bodies is one change at its end.
    gaps, since = [], 0
    for i in range(1, len(digests)):
        since += 1
        if digests[i] != digests[i - 1]:
            gaps.append(since)
            since = 0
    if gaps:
        g = sorted(gaps)
        out["polls_between_changes_median"] = g[len(g) // 2]
        out["update_period_s_estimate"] = round(
            g[len(g) // 2] * interval_s, 1)
    else:
        out["polls_between_changes_median"] = None
        out["update_period_s_estimate"] = None
        out["flags"].append(
            "THE BODY NEVER CHANGED across every poll. Either the feed is "
            "static, or it is stale, or the same error was returned each "
            "time. This is not evidence of a slow feed")
    # CORRECTED 2026-10-05, on the run that first exercised it.
    #
    # This fired only when EVERY body differed. A 5-second run returned 34
    # distinct bodies of 36 -- two duplicate pairs, so the condition was
    # false -- while the median gap between changes was still exactly one
    # poll. The estimate therefore equalled the poll interval and was just
    # as unresolved, and the script reported "changes about every 5 s" as
    # though it were a measurement.
    #
    # The symptom was "all distinct". The principle is "the median gap is at
    # the instrument's resolution floor", which is true whether or not a few
    # duplicates happen to land. Specifying a guard by the symptom in front
    # of you is mechanism N, and this is the fourth time.
    if out["polls_between_changes_median"] == 1 and len(digests) > 2:
        out["flags"].append(
            f"AT THE RESOLUTION FLOOR: the body changes between almost every "
            f"pair of polls, so the update period is at most {interval_s:.0f} "
            f"s and this run cannot say how much less. The estimate above is "
            f"the poll interval, not the feed")

    # The point of the whole exercise.
    med = out["bytes_median"]
    out["projection"] = {
        "feeds_assumed": POLLABLE_FEEDS,
        "note": "one feed's median size times the full pollable list. A "
                "real list will be smaller -- G2b narrows it to agencies "
                "whose service area touches a state line -- and feeds vary "
                "in size, so this is an order of magnitude and not a "
                "forecast.",
        "per_interval": {
            str(iv): {
                "requests_per_day": int(SECONDS_PER_DAY / iv) * POLLABLE_FEEDS,
                "gib_per_day": round(
                    int(SECONDS_PER_DAY / iv) * POLLABLE_FEEDS * med
                    / (1024 ** 3), 1),
            }
            for iv in INTERVALS_TO_PROJECT
        },
    }
    return out


def verdict(summary: dict, budget_gib: float = 5.0) -> str:
    """One sentence on whether the continental arm is affordable.

    A named function rather than a branch inside a print, because a verdict
    assembled in a print statement is untestable -- mechanism Q.
    """
    proj = summary.get("projection")
    if not proj:
        return "no measurement, so no verdict"
    afford = [iv for iv, v in proj["per_interval"].items()
              if v["gib_per_day"] <= budget_gib]
    if not afford:
        return (f"no interval up to {max(INTERVALS_TO_PROJECT)} s keeps "
                f"{POLLABLE_FEEDS} feeds under {budget_gib} GiB a day; the "
                f"list has to be narrowed by geography, not by rate")
    fastest = min(int(i) for i in afford)
    if fastest <= 30:
        return (f"{POLLABLE_FEEDS} feeds at {fastest} s fit under "
                f"{budget_gib} GiB a day, so the continental arm is "
                f"affordable at full rate and the narrowing is about "
                f"relevance rather than cost")
    return (f"{POLLABLE_FEEDS} feeds fit under {budget_gib} GiB a day only "
            f"at {fastest} s or slower, which is too coarse for segment "
            f"speed; narrow the list by geography and keep the fast rate")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--feed", default="mdb-1850", choices=sorted(FEEDS))
    ap.add_argument("--minutes", type=float, default=20.0)
    ap.add_argument("--interval", type=float, default=30.0,
                    help="seconds between polls (default 30)")
    ap.add_argument("--budget-gib", type=float, default=5.0,
                    help="daily bytes the verdict treats as affordable")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    feed = FEEDS[args.feed]
    key = WMATA_API_KEY if feed["env"] == "WMATA_API_KEY" else ""
    if not key:
        print(f"\n  {feed['env']} is not set, so this feed cannot be polled.")
        print("  Put it in .env. Nothing is measured without it.\n")
        return 1

    n = max(1, int(args.minutes * 60 / args.interval))
    print(f"\n  {feed['what']}")
    print(f"  {n} polls, {args.interval:.0f} s apart, "
          f"about {args.minutes:.0f} minutes.")
    print(f"  key: {feed['env']} sent in a {WMATA_KEY_HEADER} header "
          f"(its value is never printed or stored)\n")

    polls: list[dict] = []
    started = time.monotonic()
    for i in range(n):
        rec = poll_once(feed["url"], key, WMATA_KEY_HEADER)
        polls.append(rec)
        if rec.get("ok"):
            mark = "." if (len(polls) < 2 or
                           rec["sha256"] != polls[-2].get("sha256")) else "="
            print(f"  {i + 1:>4}/{n}  {rec['bytes']:>8,} bytes  "
                  f"{rec['elapsed_s']:>5.2f} s  {mark}")
        else:
            print(f"  {i + 1:>4}/{n}  FAILED  status={rec.get('status')}  "
                  f"{str(rec.get('reason'))[:60]}")
        if i < n - 1:
            time.sleep(max(0.0, args.interval -
                           (time.monotonic() - started) % args.interval))

    summary = summarise_polls(polls, args.interval)
    summary["verdict"] = verdict(summary, args.budget_gib)

    print(f"\n  returned {summary['polls_returned']} of "
          f"{summary['polls_attempted']}")
    if summary.get("bytes_median"):
        print(f"  bytes   min {summary['bytes_min']:,}  "
              f"median {summary['bytes_median']:,}  "
              f"max {summary['bytes_max']:,}")
        print(f"  distinct bodies {summary['distinct_bodies']}, "
              f"duplicates {summary['duplicate_fraction']:.0%}")
        if summary.get("update_period_s_estimate") is not None:
            print(f"  the feed changes about every "
                  f"{summary['update_period_s_estimate']:.0f} s")
        print(f"\n  {POLLABLE_FEEDS} feeds would cost, per day:")
        for iv, v in summary["projection"]["per_interval"].items():
            print(f"    every {iv:>4} s   {v['requests_per_day']:>10,} "
                  f"requests   {v['gib_per_day']:>7.1f} GiB")
    for f in summary["flags"]:
        print(f"  FLAG  {f}")
    print(f"\n  {summary['verdict']}\n")

    stamp = datetime.now(timezone.utc)
    out = args.out or (OUT_DIR /
                       f"transit-cost-{args.feed}-"
                       f"{stamp.strftime('%Y-%m-%dT%H%M%SZ')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "_what": "the cost of one GTFS-Realtime feed, measured",
        "_feed": args.feed,
        "_feed_what": feed["what"],
        "_url": feed["url"],
        "_auth": f"{feed['env']} sent in a {WMATA_KEY_HEADER} header; no "
                 f"value is recorded here or anywhere else",
        "_started_utc": polls[0]["t"] if polls else None,
        "_ended_utc": polls[-1]["t"] if polls else None,
        "summary": summary,
        "polls": polls,
    }, indent=1) + "\n", encoding="utf-8")
    print(f"  wrote {out}\n")
    return 0 if summary["polls_returned"] else 1


if __name__ == "__main__":
    sys.exit(main())
