"""G1: snapshot the declared layer of the land domain -- WZDx work zones.

    python scripts/fetch_wzdx.py --once              # one cycle, then exit
    python scripts/fetch_wzdx.py                     # every 10 min, forever
    python scripts/fetch_wzdx.py --interval 300      # every 5 min
    python scripts/fetch_wzdx.py --minutes 60        # run for an hour
    python scripts/fetch_wzdx.py --feed mcdot        # one feed, repeatable

WHAT THIS IS, IN THE THREE-LAYER MODEL

    cooperative   WZDx road events  <- THIS FILE  + GTFS-RT vehicle positions
    independent   transit-derived segment speed, detector aggregates
    mask          camera presence, AADT exposure denominator

An agency publishing a work zone is making a voluntary, unauthenticated claim
about its own road. Nobody checks it. That is the same epistemic shape as AIS
and ADS-B, which is why the land domain belongs in this project at all, and
the discrepancy against independently observed slowdowns is the product.

WHY A SNAPSHOT AND NOT A STREAM

A work zone persists for days or weeks. Storing every event on every poll
would write the same lane closure 144 times a day, and the aviation collector's
answer -- store raw, partition by hour -- would turn a few hundred kilobytes of
truth into tens of megabytes of repetition. So a body is written **only when
its digest changes**, and every attempt is recorded whether or not it was.

THE PER-FEED LEDGER, WHICH IS THE POINT OF THIS FILE

Twenty-five feeds behind one heartbeat is a trap, and it is the 18 missing air
hours wearing different clothes. If Oklahoma's feed 404s for three days while
twenty-four others answer, the cycle heartbeat says `ok=True` every time, the
archive simply contains no Oklahoma work zones, and G6 reports that as a
finding. It is not a finding. It is our own blindness, and after the fact
there is no way to tell the two apart.

    a ledger line with returned=true and n_events=0   -> that road was clear
    a ledger line with returned=false                 -> we were not looking
    no ledger line at all                             -> the cycle never ran

So `fetch_wzdx.py` appends one line per feed per cycle to
`data/raw/roads/wzdx/attempts-<date>.jsonl`, flushed and fsynced like the
heartbeat log, for exactly the reason `angels/core/uptime.py` gives: the polls
immediately before a crash are the ones you need, and a buffered writer loses
them. `road_discrepancy.py` reads this to refuse a conclusion about a feed
that was not answering.

THE VERSION GUARD, WITH THREE OUTCOMES RATHER THAN TWO

The registry says a feed is WZDx 4.1. The feed's own body may say something
else, or may say nothing. Those are three different facts:

    agrees     the body declares a version and it matches the registry
    disagrees  the body declares a version and it does not       -> flagged
    absent     the body declares no version at all               -> recorded

The registry's claim is **never substituted for the body's silence.** A value
copied out of its source and left to drift is mechanism D, and two feeds in
this very registry (Oklahoma, Florida) were excluded by G0 precisely because
their url and their registry row disagreed about what they were.

v4 puts it in `feed_info.version`; v3 used `road_event_feed_info.version`.
Both are tried and which one answered is recorded -- the probe stored `null`
and printed nothing for three days because it only knew the v3 name, which is
an instance of mechanism F and is not being repeated here.

NO NETWORK IN THE TESTS

Everything that decides anything is a pure function of bytes and status codes:
`classify`, `declared_version`, `version_verdict`, `feed_record`,
`cycle_summary`, `cycle_health`, `cycle_sentence`. The fetch itself is a
`urlopen` in a `for`, and a test of it would be a test of a mock.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import os
import signal
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Re-runs this script under the interpreter that has ANGELS installed, if the
# one invoking it does not. See scripts/_bootstrap.py.
#
# The name check matters. `import _bootstrap` only resolves when scripts/ is on
# sys.path, which is true when this file is RUN and false when the test suite
# IMPORTS it as scripts.<name>. Swallowing every ModuleNotFoundError here would
# also swallow the one _bootstrap raises about 'angels' itself -- turning a
# clear "wrong interpreter" message back into a confusing one.
#
# THIS BLOCK IS NOT WRAPPED IN AN `if`, AND THE FIRST VERSION OF THIS FILE WAS.
#
# It read `if __name__ == "__main__" or "scripts" not in __name__:` around the
# try, which looked like it was handling the import-as-a-module case. The
# except clause above already handles that case, so the wrapper bought nothing
# -- and `tests/test_bootstrap.py` scans `tree.body` for a top-level `ast.Try`,
# so a try nested one level down is invisible to it. Both of that file's guards
# failed on this script's first run.
#
# They were right to. A variant of the house pattern that defeats the guard
# without disabling it is worse than no pattern: the guard stays green on
# every other file, so nothing announces that one file is now unprotected.
# `classify_candidates.py` shipped a `sys.path.insert` in place of this block
# on 2026-09-25 for the same reason, which is why these two tests exist.
try:
    import _bootstrap  # noqa: F401  (must precede the angels imports)
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

# Everything below the bootstrap, never above it: _bootstrap re-executes this
# script under the project interpreter, and an import placed earlier runs under
# whatever Python the user typed and dies before the switch can happen.
from angels.config import RAW, REFERENCE
from angels.core.uptime import AlreadyRunning, CollectorLock, HeartbeatLog

# Imported rather than restated. This fetcher and the registry selection
# disagreeing about what counts as a known version is precisely the drift
# mechanism E is about -- a fix landing on one side of a pair.
#
# AND THERE IS NO FALLBACK, DELIBERATELY. The first version of this line read
#
#     try:
#         from scripts.fetch_road_registries import KNOWN_WZDX_VERSIONS
#     except ImportError:
#         KNOWN_WZDX_VERSIONS = ("4", "4.1", "4.2")
#
# which is a second copy of a constant, installed silently, in the file whose
# own docstring cites mechanism D three paragraphs above it. Add a version to
# the registry's tuple and this fetcher would keep rejecting feeds on that
# spec, with nothing anywhere saying which copy had been used. An ImportError
# here means `scripts` is not importable, which is a real problem worth
# stopping for -- the bootstrap above puts the repo root on `sys.path`
# precisely so that it never happens.
from scripts.fetch_road_registries import KNOWN_WZDX_VERSIONS

log = logging.getLogger("wzdx")

# --------------------------------------------------------------------------
# constants, so that every decision this file makes is readable without
# running it. Same reason fetch_road_registries.py puts its selection rules
# at module scope: a rule buried in a conditional is a rule nobody audits.
# --------------------------------------------------------------------------

COLLECTOR_NAME = "roads-wzdx"
DATASET = "roads/wzdx"
REGISTRY = REFERENCE / "roads" / "wzdx-registry.json"

# 5 to 15 minutes. A work zone's lifetime is days; the quantity being measured
# is how often each AGENCY refreshes its claim, and that cannot be resolved
# faster than it is published. 10 minutes sits in the middle of the band so
# the first day's ledger can measure the real cadence per feed and the
# interval can then be set from data rather than from this comment.
DEFAULT_INTERVAL_S = 600.0
INTERVAL_BAND_S = (300.0, 900.0)

# Cap anyway: an endpoint answering with something unbounded should cost one
# truncated record and not the process.
#
# MEASURED 2026-10-06, AND THE FIRST GUESS WAS WRONG. The cap was 8 MiB, on
# the reasoning that "a feed is not expected to be large". Two state DOTs --
# North Carolina and Wisconsin -- hit it exactly on the first live cycle, so
# their bodies arrived chopped mid-JSON and were classified `not_json`, which
# blamed them for our ceiling. Observed sizes among the feeds that fit:
# Indiana 4.7 MB, Kansas 1.86 MB, Utah 0.62 MB, North Dakota 0.28 MB. 64 MiB
# is generous against those and still bounded.
CAP_BYTES = 64 * 1024 * 1024

TIMEOUT_S = 30.0
USER_AGENT = ("angels-geoint/0.1 (senior capstone, GIS; contact via "
              "github.com/Mc3urry/angels-geoint)")

# WZDx 4.x then 3.x. Order matters only for which name is recorded first;
# both are tried and the answering key is reported either way.
FEED_INFO_KEYS = ("feed_info", "road_event_feed_info")

# TWO VOCABULARIES FOR ONE SPECIFICATION, discovered on the first live cycle.
#
# `KNOWN_WZDX_VERSIONS` is ("4", "4.1", "4.2") because that is how the DOT
# registry CSV writes them, and G0 selects on that column. Five feeds --
# Kansas, North Dakota, Utah, Illinois and Indiana -- declare `"4.0"` in their
# own bodies, which is how the SPECIFICATION writes the same thing.
#
# String comparison then rejected all five, and the rejection was invisible
# because it looked principled: `"4.0" != "4"` is true, and this project has
# three separate entries about not coercing a version to a float. The guard was
# right about the coercion and wrong about the vocabulary -- the constant was
# built from the catalogue rather than from the thing the catalogue describes,
# which is rule 20 pointing at a version column instead of a feed count.
#
# Its effect was not conservative. Five feeds and about 9.2 MB of real work
# zone data dropped out of the counted layer, which in the land domain means
# five states quietly declaring nothing. A check wrong in the safe direction
# is mechanism S, and "safe" here meant understating the cooperative layer in
# precisely the direction that flatters the hypothesis.
#
# So: translate, do not coerce. The registry's shorthand maps into the spec's
# notation, the table is derived from the imported tuple so the two cannot
# drift, and the raw strings from both sides stay in the ledger.
#
# Aliases are added when OBSERVED in the ledger, never in anticipation. "4.0"
# is here because five feeds sent it today.
SPEC_FROM_REGISTRY = {"4": "4.0"}
KNOWN_SPEC_VERSIONS = tuple(sorted(
    {SPEC_FROM_REGISTRY.get(v, v) for v in KNOWN_WZDX_VERSIONS}))


def canonical_version(v: str | None) -> str | None:
    """A version string in the specification's notation, or None.

    Never a float. `float("4.10") == float("4.1")` is True and those are not
    the same specification; this maps between two spellings of one spec and
    does nothing else.
    """
    if v is None:
        return None
    return SPEC_FROM_REGISTRY.get(str(v), str(v))


# Outcomes. One name per distinguishable fact, because a shared return value
# for "no" and "cannot tell" is mechanism A and is the oldest entry in the
# catalogue.
OK = "ok"                       # parsed, is WZDx, events counted (may be 0)
HTTP_ERROR = "http_error"       # a status, or a transport failure
TRUNCATED = "truncated"         # OUR cap cut it off. Not their fault, and not
                                # a claim about their JSON. Never stored.
NOT_JSON = "not_json"           # bytes arrived whole and are not JSON
NOT_WZDX = "not_wzdx"           # JSON arrived with no FeatureCollection shape
VERSION_UNKNOWN = "version_unknown"   # WZDx, but on a spec not read for this
                                      # project. Body kept, events NOT counted.

COUNTED_OUTCOMES = (OK,)

# Bodies worth keeping even though their events are not summed. A body on an
# unread specification is the first thing anyone wants when a feed changes
# spec. A TRUNCATED body is deliberately absent: storing a chopped body under
# a digest of its chopped bytes would put a partial answer in the archive
# wearing a whole answer's name, which is the 483 polygons again.
STORED_OUTCOMES = (OK, VERSION_UNKNOWN)

# Every feed changing its bytes on every cycle across twenty-five independent
# agencies is not twenty-five agencies working in lockstep; it is a timestamp
# written at generation. Below this fraction the run is ordinary.
ALL_CHANGED_FRACTION = 0.95


# --------------------------------------------------------------------------
# the registry, and the digest of it
# --------------------------------------------------------------------------

def load_registry(path: Path = REGISTRY) -> dict[str, Any]:
    """The G0 selection, with the digest of the CSV it was selected from.

    Rule 4: a published measurement carries a digest of its input. The feed
    list is an input to every row of the ledger, and a ledger that does not
    say which list it was polling cannot be read in February -- the registry
    will have been refetched by then and the set will have moved.
    """
    if not path.exists():
        raise SystemExit(
            f"\n  No registry at {path}\n"
            f"  Run: python scripts/fetch_road_registries.py\n")
    art = json.loads(path.read_text(encoding="utf-8"))
    if "selected" not in art:
        raise SystemExit(f"\n  {path} has no `selected` list.\n")
    return art


def feeds_from(art: dict[str, Any],
               only: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """The selected feeds, optionally narrowed by `feedName`.

    A `--feed` that matches nothing is an error and not an empty run. An empty
    run writes a cycle of zero attempts, which looks on disk exactly like a
    cycle in which nothing was wrong -- the same shape as a glob that matched
    no files passing a completeness check.
    """
    rows = list(art["selected"])
    if not only:
        return rows
    wanted = {o.lower() for o in only}
    picked = [r for r in rows if str(r.get("feedName", "")).lower() in wanted]
    missing = wanted - {str(r.get("feedName", "")).lower() for r in picked}
    if missing:
        names = ", ".join(sorted(str(r.get("feedName")) for r in rows))
        raise SystemExit(
            f"\n  No selected feed named: {', '.join(sorted(missing))}\n"
            f"  Available: {names}\n")
    return picked


# --------------------------------------------------------------------------
# reading what a body says about itself
# --------------------------------------------------------------------------

def declared_version(obj: Any) -> tuple[str | None, str | None]:
    """The spec version the body itself declares, and which key held it.

    Returns `(None, None)` when nothing declares one. The version is returned
    as a **string** and never coerced: `float("4.10") == float("4.1")` is True
    and those are not the same specification. The registry's own version
    column was read as a string for the same reason, and `STATE` in the
    TIGERweb boundary probe is a third instance of the identical trap.
    """
    if not isinstance(obj, dict):
        return None, None
    for key in FEED_INFO_KEYS:
        block = obj.get(key)
        if isinstance(block, dict) and block.get("version") is not None:
            return str(block["version"]), key
    return None, None


def version_verdict(declared: str | None, registry: str | None) -> str:
    """Five outcomes, and the fifth was bought with five states.

    `absent` is NOT `agrees`. A feed that declares nothing has told us nothing,
    and filling the silence with the registry's claim would make a catalogue
    row look like a measurement -- mechanism D, and why two feeds were
    excluded by G0 in the first place.

    `agrees-by-alias` exists because the first live cycle found five feeds
    declaring `"4.0"` against a registry saying `"4"`. Those are one
    specification in two notations. Reporting them as `disagrees` buried a
    real disagreement (St Charles County, 4.1 against a registry claiming 4)
    among five false ones, and a flag that cries wolf five times out of six
    is rule 16's failure. The notation difference is still named rather than
    smoothed away, because it is a fact about the registry worth having.
    """
    if declared is None:
        return "absent"
    if registry is None:
        return "registry-silent"
    if str(declared) == str(registry):
        return "agrees"
    if canonical_version(declared) == canonical_version(registry):
        return "agrees-by-alias"
    return "disagrees"


def event_count(obj: Any) -> int | None:
    """Number of road events, or None if this is not a WZDx FeatureCollection.

    **Zero is not None.** A feed that returned an empty `features` list has
    said "no work zones on my roads right now", which is a finding; a body
    with no `features` key at all has said nothing and may not be WZDx. The
    air domain's `heartbeats present, no aircraft` distinction is the same
    distinction, and collapsing it here would let a misconfigured endpoint
    read as a quiet highway.
    """
    if not isinstance(obj, dict):
        return None
    feats = obj.get("features")
    if not isinstance(feats, list):
        return None
    return len(feats)


def looks_like_html(body: bytes) -> bool:
    """Maryland's CHART answered HTTP 200 with a web page three times.

    An error page served with a 200 has a length, parses as nothing, and
    averages into a size estimate in the comfortable direction. It is checked
    for by content rather than by status because the status was 200.
    """
    head = body[:512].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def classify(status: int | None, body: bytes | None,
             declared: str | None, truncated: bool = False) -> str:
    """One outcome name per distinguishable fact about one feed.

    `truncated` is checked FIRST, and the first version of this function did
    not take the argument at all. North Carolina and Wisconsin both hit the
    8 MiB cap exactly on the first live cycle, so their bodies arrived chopped
    mid-object, failed to parse, and were filed as `not_json` -- a claim about
    their data when the only fact in evidence was about our ceiling. The
    archive was spared by luck rather than by design: `not_json` happened not
    to be in the stored set, so a half body never reached the disk.
    """
    if truncated:
        return TRUNCATED
    if status is None or body is None or not (200 <= status < 300):
        return HTTP_ERROR
    if looks_like_html(body):
        return NOT_WZDX
    try:
        obj = json.loads(body.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return NOT_JSON
    if event_count(obj) is None:
        return NOT_WZDX
    if (declared is not None
            and canonical_version(declared) not in KNOWN_SPEC_VERSIONS):
        return VERSION_UNKNOWN
    return OK


# --------------------------------------------------------------------------
# one feed, one cycle
# --------------------------------------------------------------------------

def digest(body: bytes) -> str:
    """Of the bytes exactly as they arrived. Verifies the archive."""
    return hashlib.sha256(body).hexdigest()


def content_digest(obj: Any) -> str | None:
    """Of the road events alone, insensitive to order and to the envelope.

    MEASURED 2026-10-06, FROM TWO CYCLES 15.6 MINUTES APART.

    Eleven of twenty-three feeds changed their bytes in that window. Five of
    those eleven changed nothing about any road:

        iddot, modot, msdot, stcharlesco_v4   `feed_info.update_date` advanced
                                              and the features were byte
                                              identical, in the same order
        necdot                                the same 182 features, shuffled

    That is 581,926 bytes stored -- 36 per cent of the cycle's recurring
    traffic -- for bodies carrying no new claim about any road. A whole-body
    digest cannot tell those from the six feeds where lane closures genuinely
    appeared and vanished (dedot +8 events, njdot -60).

    Worse than the storage: it makes `changed` mean the wrong thing. The
    quantity this layer exists to track is **when an agency's declaration
    about a road changes**, and a timestamp rewritten on every request answers
    that question "always". This is the WMATA header timestamp exactly -- a
    header that advanced every poll while the fleet sat still -- one domain
    over, and it is a cooperative-reporting defect in its own right: a
    declaration of freshness that the content does not support.

    Feature ORDER is excluded deliberately. A GeoJSON FeatureCollection is a
    set; necdot returning its 182 events in a different sequence is not news.
    Sorted by `(id, canonical form)` rather than `id` alone, so the ordering
    stays total even if a feed ships two features under one id -- which would
    be a real defect, and one that must not be allowed to make this digest
    unstable and so report churn forever.

    Returns None when there are no features to digest, which is `not WZDx`
    and is a different fact from an empty feed.
    """
    if not isinstance(obj, dict):
        return None
    feats = obj.get("features")
    if not isinstance(feats, list):
        return None
    canonical = json.dumps(
        sorted(feats, key=lambda f: (str(f.get("id") if isinstance(f, dict)
                                         else f),
                                     json.dumps(f, sort_keys=True))),
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def feed_record(feed: dict[str, Any], *, status: int | None,
                body: bytes | None, elapsed_s: float,
                error: str | None = None,
                truncated: bool = False) -> dict[str, Any]:
    """Everything learned about one feed in one cycle, as one ledger row.

    Deliberately flat and deliberately verbose. This row is the only account
    of this moment that will exist, and every field here answers a question
    somebody will ask in February with no way to re-fetch the answer.
    """
    obj: Any = None
    if body is not None and not looks_like_html(body):
        try:
            obj = json.loads(body.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            obj = None

    declared, version_key = declared_version(obj)
    registry_version = feed.get("version")
    outcome = classify(status, body, declared, truncated)
    n_events = event_count(obj) if outcome in COUNTED_OUTCOMES else None

    rec: dict[str, Any] = {
        "t": int(datetime.now(timezone.utc).timestamp()),
        "iso": datetime.now(timezone.utc).isoformat(),
        "feed": feed.get("feedName"),
        "org": feed.get("issuingOrganization"),
        "state": feed.get("state"),
        "returned": outcome != HTTP_ERROR,
        "outcome": outcome,
        "status": status,
        "elapsed_s": round(elapsed_s, 3),
        # bytes_received is what came off the wire. bytes_stored is set by the
        # writer and is 0 when the body was unchanged. Two names because one
        # field holding whichever happened to be convenient is how `n_polls`
        # published a file count against 19,329 attempted polls.
        "bytes_received": len(body) if body is not None else 0,
        "truncated": truncated,
        # Two digests, two questions. `sha256` verifies the archived bytes;
        # `content_sha256` is what "did this agency's claim change" means, and
        # the difference between them is how four feeds were found rewriting
        # `update_date` over identical road events.
        "sha256": digest(body) if body else None,
        "content_sha256": content_digest(obj),
        "n_events": n_events,
        # The raw strings from both sides, and the canonical form beside them.
        # Keeping only the canonical form would hide that a registry row and a
        # feed spell one specification two ways, which is a fact about the
        # registry; keeping only the raw strings is what made five feeds look
        # like disagreements.
        "version_declared": declared,
        "version_canonical": canonical_version(declared),
        "version_key": version_key,
        "version_registry": registry_version,
        "version_verdict": version_verdict(declared, registry_version),
        "error": error,
    }
    return rec


# --------------------------------------------------------------------------
# the cycle, and the sentence that reports it
# --------------------------------------------------------------------------

def cycle_summary(records: list[dict[str, Any]],
                  changed: set[str] | None = None,
                  first_seen: set[str] | None = None,
                  envelope_only: set[str] | None = None) -> dict[str, Any]:
    """Counts over one cycle, each named for exactly what it holds.

    `changed` is the set of feed names whose digest differed from the previous
    stored body. Passed in rather than derived, because "changed" is a fact
    about the archive and not about this cycle's bytes, and a function that
    went to disk would not be testable without one.
    """
    changed = changed or set()
    first_seen = first_seen or set()
    envelope_only = envelope_only or set()
    by_outcome: dict[str, int] = {}
    for r in records:
        by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1

    returned = [r for r in records if r["returned"]]
    counted = [r for r in records if r["n_events"] is not None]
    events = sum(r["n_events"] for r in counted)

    s: dict[str, Any] = {
        "n_feeds_attempted": len(records),
        "n_feeds_returned": len(returned),
        "n_feeds_counted": len(counted),
        "n_feeds_changed": len(changed),
        # Of those changes, how many were the first body we ever held for that
        # feed. On an empty archive this equals n_feeds_changed and the cycle
        # has measured no change rate at all.
        "n_feeds_first_seen": len(first_seen),
        "n_feeds_refreshed": len(changed - first_seen),
        # Bodies that moved while no road did: `feed_info.update_date`
        # advancing, or the same features in a different order. Not stored,
        # and recorded here because it is a fact about the feed -- a claim of
        # freshness its own content does not support.
        "n_feeds_envelope_only": len(envelope_only),
        "feeds_envelope_only": sorted(envelope_only),
        "n_events_total": events,
        "by_outcome": by_outcome,
        "feeds_failed": sorted(r["feed"] for r in records
                               if not r["returned"]),
        "feeds_changed": sorted(changed),
        "feeds_truncated": sorted(r["feed"] for r in records
                                  if r["outcome"] == TRUNCATED),
        "feeds_uncountable": sorted(
            r["feed"] for r in records
            if r["returned"] and r["n_events"] is None),
        "version_disagreements": sorted(
            r["feed"] for r in records if r["version_verdict"] == "disagrees"),
        # Separate from the disagreements, because one notation is the
        # registry's and the other is the specification's and neither party is
        # wrong. Recorded so the registry's shorthand stays visible; not
        # flagged, because it is neither actionable nor rare.
        "version_notation_differs": sorted(
            r["feed"] for r in records
            if r["version_verdict"] == "agrees-by-alias"),
        "feeds_without_declared_version": sorted(
            r["feed"] for r in records if r["version_verdict"] == "absent"),
        "flags": [],
    }
    if counted:
        s["events_median"] = statistics.median(
            r["n_events"] for r in counted)
        s["events_max"] = max(r["n_events"] for r in counted)
    s["flags"] = cycle_flags(s)
    return s


def cycle_flags(s: dict[str, Any]) -> list[str]:
    """Only things a reader must act on.

    Rule 16: a flag's currency is the reader's attention, and a check that
    fires on a healthy cycle spends what the next real one will need. A run
    where most feeds answered, a few changed and nobody contradicted the
    registry produces an empty list, and that emptiness is the signal.
    """
    flags: list[str] = []
    attempted = s["n_feeds_attempted"]
    returned = s["n_feeds_returned"]

    if attempted == 0:
        flags.append("NO FEEDS ATTEMPTED: a cycle of zero polls is not a "
                     "quiet network, it is a selection that matched nothing")
        return flags

    if returned == 0:
        flags.append("NOTHING RETURNED: every feed failed, so this cycle is "
                     "an outage on our side and not a measurement of theirs")
        return flags

    if s["n_feeds_counted"] == 0:
        flags.append("NOTHING COUNTABLE: feeds answered but none parsed as "
                     "WZDx, so n_events_total is 0 for want of data rather "
                     "than for want of work zones")

    # Refreshes, not first sightings. A cycle against an empty archive changes
    # everything by definition and has measured no change rate; accusing the
    # agencies of lockstep on the strength of it would be the instrument
    # reporting its own initial condition as a finding.
    refreshed = s.get("n_feeds_refreshed", s["n_feeds_changed"])
    if refreshed and refreshed / returned >= ALL_CHANGED_FRACTION:
        flags.append(
            f"NEARLY EVERY FEED REFRESHED ({refreshed} of {returned}): "
            f"twenty-five independent agencies do not refresh in lockstep. "
            f"Suspect a generation timestamp in the body, which would make "
            f"every cycle look like new information and defeat the "
            f"change-only storage")

    if s["version_disagreements"]:
        flags.append(
            f"VERSION DISAGREEMENT, feed vs registry: "
            f"{', '.join(s['version_disagreements'])}. The body's claim is "
            f"the one to believe; the registry row is a catalogue entry")

    if s["feeds_truncated"]:
        flags.append(
            f"TRUNCATED BY OUR OWN CAP: {', '.join(s['feeds_truncated'])}. "
            f"These bodies were cut off at {CAP_BYTES // (1024 * 1024)} MiB "
            f"and are NOT stored, because a chopped body under a digest of "
            f"its chopped bytes is a partial answer wearing a whole answer's "
            f"name. Raise CAP_BYTES; this is our ceiling and not their fault")

    return flags


def _were(n: int) -> str:
    """"1 was blind" and "3 were blind".

    A cosmetic fix to a sentence nobody will reread is usually not worth the
    line. These sentences are the lab notebook -- they are what the worklog
    quotes and what a reader three months from now sees instead of the
    archive -- so they are written as prose and held to it.
    """
    return "was" if n == 1 else "were"


def cycle_sentence(s: dict[str, Any]) -> str:
    """One sentence per distinguishable cycle, per rule 13.

    Six cases, six sentences, and a test asserts the six are distinct. A
    swallowed branch here looks perfectly correct in isolation, which is why
    the count is what catches it rather than the reading.
    """
    attempted = s["n_feeds_attempted"]
    returned = s["n_feeds_returned"]
    counted = s["n_feeds_counted"]
    changed = s["n_feeds_changed"]
    events = s["n_events_total"]

    # THE CORRECTION OF 2026-10-06, AFTER THE FIRST LIVE CYCLE.
    #
    # The sentence used to read "23 of 25 feeds answered with 9225 events".
    # Every number in it was correct and the sentence was not: the 9,225 came
    # from 15 feeds, not 23. Eight answered and were not countable -- two
    # truncated, two unparseable, one not WZDx, three on a notation the guard
    # then rejected -- so the reader was handed a yield attached to the wrong
    # denominator. `n_feeds_counted` was sitting in the summary unused.
    #
    # That is mechanism U: a correct number under a shape that answers a
    # question nobody asked. The denominator of a rate has to appear beside it.
    yield_ = (f"{events} events from {counted} countable" if counted != returned
              else f"{events} events")

    # A cycle that is mostly first sightings says so, because "20 changed"
    # against an empty archive is the instrument's initial condition and not a
    # fact about anybody's roads.
    first = s.get("n_feeds_first_seen", 0)
    if first and first == changed:
        changed_clause = f"{changed} stored for the first time"
    elif first:
        changed_clause = (f"{changed} changed, {first} of them a first "
                          f"sighting")
    else:
        changed_clause = f"{changed} changed"

    # Not flagged -- for the four feeds doing it, it happens every cycle, and
    # rule 16 says a warning that always fires teaches the reader to skip the
    # list. Stated in the sentence instead, where it is information rather
    # than an alarm.
    envelope = s.get("n_feeds_envelope_only", 0)
    if envelope:
        changed_clause += (f" ({envelope} more moved their update_date over "
                           f"identical road events)")

    if attempted == 0:
        return "no feeds were attempted, so this cycle measured nothing"
    if returned == 0:
        return (f"all {attempted} feeds failed: we were looking and nobody "
                f"answered, which is our outage to explain")
    if returned < attempted:
        blind = attempted - returned
        return (f"{returned} of {attempted} feeds answered, {yield_}, "
                f"{changed_clause}; {blind} {_were(blind)} blind this cycle "
                f"and {'is' if blind == 1 else 'are'} named in the ledger")
    if changed == 0:
        return (f"all {attempted} feeds answered, {yield_}, and not one body "
                f"changed, so nothing was stored this cycle")
    if changed == returned:
        return (f"all {attempted} feeds answered, {yield_}, and "
                f"{changed_clause} -- every one of them, which is more "
                f"coordination than twenty-five agencies have")
    return (f"all {attempted} feeds answered, {yield_}, {changed_clause} "
            f"and were stored")


def cycle_health(s: dict[str, Any]) -> tuple[bool, str | None]:
    """What the heartbeat records for this cycle.

    `ok=False` means WE were not successfully looking. One feed refusing is
    their silence and belongs in the ledger; every feed refusing is almost
    certainly our network, and the air domain's 2,621 `getaddrinfo failed`
    polls are what that looks like when it is recorded properly.
    """
    if s["n_feeds_attempted"] == 0:
        return False, "no feeds attempted"
    if s["n_feeds_returned"] == 0:
        return False, (f"all {s['n_feeds_attempted']} feeds failed: "
                       f"{', '.join(s['feeds_failed'][:5])}")
    return True, None


def blind_feeds(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Feeds that never once returned across these ledger rows.

    The question `road_discrepancy.py` has to ask before reading an absence of
    work zones as an absence of work zones. A feed in here contributed no
    observation over the window, and any statistic computed across it is
    reporting our blindness as their behaviour -- mechanism J, "missing" that
    means "missing where I looked".
    """
    attempted: dict[str, int] = {}
    returned: dict[str, int] = {}
    for r in rows:
        name = r.get("feed")
        attempted[name] = attempted.get(name, 0) + 1
        if r.get("returned"):
            returned[name] = returned.get(name, 0) + 1
    return {name: n for name, n in attempted.items() if not returned.get(name)}


def decorative_update_date(rows: list[dict[str, Any]],
                           min_cycles: int = 3) -> dict[str, int]:
    """Feeds whose bytes keep moving while their road events never do.

    A cross-cycle question, so it lives here rather than in a per-cycle flag:
    one envelope-only cycle is ordinary, and a feed that has done nothing else
    for a day is telling us its `update_date` is decorative.

    That matters beyond storage. If G4 or G6 ever wants "how current is this
    agency's declaration", `feed_info.update_date` answers "seconds old" for
    these feeds and means nothing by it -- which is the same defect as a
    transit header stamped at generation, and the same defect, one level up,
    as the unverified self-reporting this whole project is about. The freshest
    thing in the feed is the claim about its own freshness.
    """
    envelope: dict[str, int] = {}
    real: dict[str, int] = {}
    for r in rows:
        name = r.get("feed")
        if r.get("envelope_only"):
            envelope[name] = envelope.get(name, 0) + 1
        elif r.get("stored_as"):
            real[name] = real.get(name, 0) + 1
    return {name: n for name, n in envelope.items()
            if n >= min_cycles and not real.get(name)}


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def ledger_path(root: Path, when: datetime) -> Path:
    return root / DATASET / f"attempts-{when:%Y-%m-%d}.jsonl"


def append_ledger(root: Path, records: list[dict[str, Any]],
                  when: datetime | None = None) -> Path:
    """Append-only, flushed and fsynced, one line per feed per cycle.

    Line-delimited JSON rather than parquet, for the reason `uptime.py` gives
    about heartbeats: a buffered columnar writer loses whatever it holds when
    the process is killed, and the cycles immediately before a crash are
    exactly the ones needed to explain the gap.
    """
    when = when or datetime.now(timezone.utc)
    path = ledger_path(root, when)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return path


def body_path(root: Path, feed: str, when: datetime, sha: str) -> Path:
    """Hour-partitioned, and content-addressed by the first 12 hex of the digest.

    THE DEFECT THIS NAME EXISTS FOR, 2026-10-06.

    The first version was `{feed}_{timestamp}.json.gz`, to the second, which
    felt like enough. It is not. A round-trip check -- store a body, store an
    unchanged copy, store a different body, then decompress the first file and
    compare it to the bytes handed in -- returned False on its first run,
    because all three calls landed inside the same second and the third
    silently overwrote the first. The archive held the newest body under the
    oldest body's name and nothing anywhere said so.

    This is the 2026-10-02 decision verbatim, one directory over: the probe
    wrote `probe-<date>.json`, three runs the same afternoon wrote one
    filename, and the file on disk held one endpoint while the other two
    survived only in a chat transcript. The resolution was raised to the
    second then. Raising a resolution is a guard specified by the symptom in
    front of you, which is mechanism N, and this is what the next instance of
    it looks like.

    So the digest goes in the name and the collision cannot occur: two
    different bodies cannot share a path, and the same body never reaches here
    twice because `store_body` returns early when the digest is unchanged. The
    filename now says which body it holds, which is worth more than the
    tidiness it costs.
    """
    part = root / DATASET / f"hour={when:%Y%m%d%H}"
    return part / f"{feed}_{when:%Y%m%dT%H%M%S}_{sha[:12]}.json.gz"


def _sidecar(root: Path, feed: str) -> Path:
    return root / DATASET / "_last" / f"{feed}.json"


def last_seen(root: Path, feed: str) -> dict[str, Any]:
    """What we last held for this feed: both digests, or an empty mapping.

    Read from a small sidecar rather than by hashing the newest file, so that
    an interrupted write cannot make an unchanged feed look changed forever.
    """
    p = _sidecar(root, feed)
    if not p.exists():
        return {}
    try:
        got = json.loads(p.read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def last_digest(root: Path, feed: str) -> str | None:
    """The CONTENT digest we last stored, which is what change means here."""
    return last_seen(root, feed).get("content_sha256")


def store_body(root: Path, feed: str, body: bytes, sha: str,
               content_sha: str | None = None,
               when: datetime | None = None) -> dict[str, Any]:
    """Write the body iff its digest differs from the last stored one.

    Returns three separately-named facts, and the reason it is not a tuple of
    two is the `n_polls` lesson:

        digest_is_new   the FEED's content differs from what we last saw.
                        A fact about the agency.
        bytes_stored    how many bytes went to disk this call, 0 when none
                        did. A fact about our archive.
        path            where they are, or where they already were.

    The first version returned `(path_or_None, bytes_stored)` and the caller
    read `path is not None` as "the feed changed". Those come apart: a feed
    that reverts to a body already on disk under this second's name has
    genuinely changed while needing no write, and the caller would have
    recorded it as unchanged AND left the sidecar naming the wrong digest. One
    variable holding whichever of two quantities happened to be convenient is
    exactly the shape that published a file count under the name `n_polls`
    against 19,329 attempted polls.

    gzip because these are JSON text and the ratio is worth measuring rather
    than assuming -- `bytes_received` and `bytes_stored` are both in the
    ledger, so the first day's run answers the storage question with data the
    way G2a replaced a 1.11 TiB/month guess with a 22 GiB/month measurement.
    """
    when = when or datetime.now(timezone.utc)
    # A feed with no features has no content digest, so it falls back to the
    # body's. Such a body is not stored anyway (TRUNCATED and NOT_WZDX are not
    # in STORED_OUTCOMES), but the fallback keeps the function total rather
    # than relying on a caller's discipline.
    content_sha = content_sha or sha
    seen = last_seen(root, feed)
    previous, previous_body = seen.get("content_sha256"), seen.get("sha256")

    out: dict[str, Any] = {"path": None, "bytes_stored": 0,
                           "digest_is_new": False, "first_sighting": False,
                           "envelope_only": False}

    if previous == content_sha:
        # Nothing about any road changed. If the BYTES changed anyway, that is
        # the envelope moving -- `feed_info.update_date`, or the features in a
        # different order -- and it is a fact about the feed worth recording
        # even though the body is not worth keeping. The ledger gets it; the
        # archive does not. Four feeds did exactly this between the only two
        # cycles this collector has ever run.
        out["envelope_only"] = previous_body is not None and previous_body != sha
        if out["envelope_only"]:
            _write_sidecar(root, feed, sha, content_sha, when,
                           name=seen.get("path", ""))
        return out

    out["digest_is_new"] = True
    # A FOURTH FACT, AND THE FLAG THAT NEEDED IT.
    #
    # "we have never seen this feed before" is not "this feed changed". On the
    # first cycle against an empty archive every feed is the former, and the
    # lockstep flag -- which accuses the agencies of refreshing in unison and
    # suspects a generation timestamp -- compares changes against answers. On
    # 2026-10-06 it did not fire, at 20 of 23, purely because three feeds were
    # uncountable; at 23 of 23 it would have fired and been wrong, blaming
    # twenty-five agencies for an archive that was empty an hour earlier.
    #
    # A guard that is correct only because of an unrelated accident has not
    # been tested, which is the same thing mechanism Z said about a convention
    # that had been right eleven times.
    out["first_sighting"] = previous is None
    path = body_path(root, feed, when, sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    out["path"] = path

    if not path.exists():
        blob = gzip.compress(body)
        path.write_bytes(blob)
        out["bytes_stored"] = len(blob)

    # Written whether or not bytes were needed. The sidecar answers "what did
    # we last see for this feed", which is true either way; one updated only
    # on write would disagree with the archive and make the next cycle
    # re-decide a question already answered.
    _write_sidecar(root, feed, sha, content_sha, when, name=path.name)
    return out


def _write_sidecar(root: Path, feed: str, sha: str, content_sha: str,
                   when: datetime, name: str = "") -> None:
    side = _sidecar(root, feed)
    side.parent.mkdir(parents=True, exist_ok=True)
    side.write_text(json.dumps({
        "sha256": sha,                 # the last bytes we saw
        "content_sha256": content_sha,  # the last road events we saw
        "iso": when.isoformat(),
        "path": name,                  # the body that holds those events
    }), encoding="utf-8")


# --------------------------------------------------------------------------
# the network, which is the only impure part
# --------------------------------------------------------------------------

def fetch(url: str) -> tuple[int | None, bytes | None, float, str | None, bool]:
    """GET one feed. Returns (status, body, elapsed_s, error, truncated).

    No credentials are ever sent: every feed in the selected list is keyless
    by construction, which is what G0's `needAPIKey` rule selected on. A feed
    that starts demanding one will 401 and that is a fact for the ledger.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/geo+json, application/json;q=0.9, */*;q=0.1",
    })
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = resp.read(CAP_BYTES + 1)
            truncated = len(body) > CAP_BYTES
            return (resp.status, body[:CAP_BYTES],
                    time.monotonic() - t0, None, truncated)
    except urllib.error.HTTPError as exc:
        # A 4xx/5xx still carries a body, and the body often says why.
        try:
            body = exc.read(CAP_BYTES)
        except Exception:
            body = b""
        return (exc.code, body, time.monotonic() - t0,
                f"HTTP {exc.code}", False)
    except Exception as exc:
        return (None, None, time.monotonic() - t0,
                f"{type(exc).__name__}: {exc}"[:200], False)


class Snapshotter:
    def __init__(self, feeds: list[dict[str, Any]], root: Path,
                 interval: float, *, registry_sha: str = "") -> None:
        self.feeds = feeds
        self.root = Path(root)
        self.interval = interval
        self.registry_sha = registry_sha
        self.cycles = 0
        self.stored = 0
        self.bytes_stored = 0
        self.running = True
        self.hb = HeartbeatLog(root, collector=COLLECTOR_NAME,
                               interval_s=interval)

    def cycle(self) -> dict[str, Any]:
        when = datetime.now(timezone.utc)
        records: list[dict[str, Any]] = []
        changed: set[str] = set()
        first_seen: set[str] = set()
        envelope_only: set[str] = set()

        for feed in self.feeds:
            name = str(feed.get("feedName"))
            status, body, elapsed, error, truncated = fetch(str(feed["url"]))
            rec = feed_record(feed, status=status, body=body,
                              elapsed_s=elapsed, error=error,
                              truncated=truncated)
            rec["registry_sha256"] = self.registry_sha
            rec["bytes_stored"] = 0

            if rec["outcome"] in STORED_OUTCOMES and body:
                # VERSION_UNKNOWN bodies ARE stored: the first thing anyone
                # wants when a feed changes specification is the body from
                # before it did. Not counted, not discarded. TRUNCATED bodies
                # are NOT in that set -- see the constant.
                st = store_body(self.root, name, body, rec["sha256"],
                                rec["content_sha256"], when)
                # `changed` tracks the FEED's road events, so it follows
                # digest_is_new and not whether bytes reached the disk.
                if st["digest_is_new"]:
                    changed.add(name)
                    rec["stored_as"] = st["path"].name
                if st["first_sighting"]:
                    first_seen.add(name)
                if st["envelope_only"]:
                    envelope_only.add(name)
                rec["first_sighting"] = st["first_sighting"]
                rec["envelope_only"] = st["envelope_only"]
                rec["bytes_stored"] = st["bytes_stored"]
                if st["bytes_stored"]:
                    self.stored += 1
                    self.bytes_stored += st["bytes_stored"]

            records.append(rec)

        append_ledger(self.root, records, when)
        summary = cycle_summary(records, changed, first_seen, envelope_only)
        self.cycles += 1

        ok, error = cycle_health(summary)
        self.hb.poll(ok=ok, n=summary["n_events_total"], error=error)
        return summary

    def stop(self, *_: object) -> None:
        log.info("stopping after this cycle...")
        self.running = False

    def run(self, max_cycles: int | None = None) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        log.info("%d feed(s) every %.0fs -> %s",
                 len(self.feeds), self.interval, self.root / DATASET)
        log.info("session %s", self.hb.session_id)
        # Heartbeat from the first poll, not retrofitted after a gap nobody
        # can explain. That is the whole lesson of the 18 missing air hours.
        self.hb.start(feeds=len(self.feeds), dataset=DATASET,
                      registry_sha256=self.registry_sha)

        try:
            while self.running:
                try:
                    s = self.cycle()
                    log.info("cycle %d: %s", self.cycles, cycle_sentence(s))
                    for f in s["flags"]:
                        log.warning("  FLAG %s", f)
                except Exception as exc:
                    # One bad cycle must not end a multi-day run, and a failed
                    # cycle is still a heartbeat: we were awake and we asked.
                    self.cycles += 1
                    self.hb.poll(ok=False, n=0, error=str(exc)[:200])
                    log.warning("cycle failed: %s", exc)

                if max_cycles and self.cycles >= max_cycles:
                    break
                if self.running:
                    time.sleep(self.interval)
        finally:
            self.hb.stop(reason="signal" if not self.running else "complete")
            log.info("done: %d cycle(s), %d body/bodies stored, %.1f KiB",
                     self.cycles, self.stored, self.bytes_stored / 1024)


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S,
                    help=f"seconds between cycles "
                         f"(default {DEFAULT_INTERVAL_S:.0f})")
    ap.add_argument("--once", action="store_true",
                    help="one cycle, then exit")
    ap.add_argument("--minutes", type=float, help="stop after this long")
    ap.add_argument("--feed", action="append", default=[],
                    help="only this feedName; repeatable")
    ap.add_argument("--registry", type=Path, default=REGISTRY)
    ap.add_argument("--root", type=Path, default=RAW)
    ap.add_argument("--force", action="store_true",
                    help="take the collector lock even if one is held")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")

    lo, hi = INTERVAL_BAND_S
    if not args.once and not (lo <= args.interval <= hi):
        log.warning("interval %.0fs is outside the %.0f-%.0fs band this "
                    "layer was designed for; a work zone's lifetime is days, "
                    "so faster buys repetition and slower loses the agency's "
                    "own refresh cadence", args.interval, lo, hi)

    art = load_registry(args.registry)
    feeds = feeds_from(art, tuple(args.feed))
    log.info("registry %s, CSV sha256 %s",
             args.registry.name, str(art.get("_sha256", "?"))[:12])

    snap = Snapshotter(feeds, args.root, args.interval,
                       registry_sha=str(art.get("_sha256", "")))

    max_cycles = 1 if args.once else None
    if args.minutes:
        max_cycles = max(1, int(args.minutes * 60 / args.interval))

    lock = CollectorLock(args.root, COLLECTOR_NAME,
                         session_id=snap.hb.session_id, force=args.force)
    try:
        with lock:
            snap.run(max_cycles)
    except AlreadyRunning as exc:
        log.error("%s", exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
