"""Is there an independent observation channel over our region?

THE QUESTION THIS ANSWERS, AND WHY IT IS THE ONE THAT MATTERS

The thesis rests on a distinction between two kinds of data:

    REPORT       what a platform says about itself. Unauthenticated, and
                 trivially falsified. ADS-B, AIS, TLEs, manifests.
    OBSERVATION  where something was sensed to be, by someone other than
                 itself. Radar, MLAT, SAR. No consent required, nothing to
                 forge.

Every detector in core/detectors/ that runs today -- kinematics, identity --
only ever compares reports against THEMSELVES. That is the self-consistency
half, and it catches a spoof careless enough to contradict itself. The other
half, matching.py, subtracts observation from report and is the reason the
architecture separates the two types at all. Without an observation channel it
cannot run, and the project is half a thesis.

OpenSky closed the obvious door: no historical MLAT over the mid-Atlantic
(Strohmeier, September 2026). But OpenSky is not the only aggregator, and
"no historical MLAT" is not "no MLAT". Community networks run their own
multilateration from their own feeders, and they also relay TIS-B -- FAA
ground surveillance, rebroadcast. Both are observations in the strict sense.

So the question is not "can I have OpenSky's archive" but "does an observation
channel EXIST over Washington, that I could start archiving tonight". Nine
days from now, an answer of yes is worth more than the application would have
been.

WHAT COUNTS AS AN OBSERVATION

readsb tags every aircraft with how its CURRENT POSITION was derived. The
taxonomy is the whole answer:

    adsb_icao, adsb_icao_nt, adsb_other      the aircraft said so       REPORT
    adsr_icao, adsr_other                    relayed, still self-said   REPORT
    mode_s                                   no position at all         neither

    mlat            position solved from arrival-time differences across
                    several ground receivers. The aircraft transmitted
                    something, but NOT its position -- the position is
                    computed about it, without its cooperation.  OBSERVATION

    tisb_icao       a non-ADS-B target tracked by SECONDARY radar and
                    rebroadcast by the FAA.                     OBSERVATION
    tisb_trackfile  a target held by PRIMARY or Mode A/C radar, identified
                    by a track number rather than an ICAO address. The
                    strongest case: the target may carry no cooperative
                    equipment whatsoever.                       OBSERVATION
    tisb_other      TIS-B on a non-ICAO address.                OBSERVATION

An aircraft of type `mlat` is, by definition, one whose position you could not
have obtained from what it broadcast. That is not a proxy for the thing this
project is about. It is the thing.

A NOTE ON tisb_trackfile AND IDENTITY

Track-file targets have no ICAO24 -- they are a radar track number, and the
number is reassigned freely. So matching them to a Report cannot be done by
identifier; it has to be done positionally, which is precisely what
Track.position_at() and matching.py were designed for. If the counts below
are dominated by tisb_trackfile, that is good news for the thesis and a
reminder that matching must not be written as a dictionary lookup on id.

    python scripts/probe_observations.py              # 5 rounds, ~1 min
    python scripts/probe_observations.py --polls 20   # a firmer sample
    python scripts/probe_observations.py --json out.json

No credentials. Both networks are free and unauthenticated; both rate-limit to
roughly one request per second, which this respects.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone


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
import httpx

from angels.config import AOI_AIR, REGION_NAME, box_area_km2

# -- what the type field means ---------------------------------------------

REPORT_TYPES = {
    "adsb_icao", "adsb_icao_nt", "adsb_other",
    "adsr_icao", "adsr_other", "adsc",
}
OBSERVATION_TYPES = {
    "mlat", "tisb_icao", "tisb_trackfile", "tisb_other",
}
NO_POSITION_TYPES = {"mode_s", "other"}

EXPLAIN = {
    "adsb_icao": "aircraft broadcasting its own position (ADS-B)",
    "adsb_icao_nt": "ADS-B from a non-transponder emitter (ground vehicle)",
    "adsb_other": "ADS-B on a non-ICAO (often anonymised) address",
    "adsr_icao": "ADS-B relayed from another datalink (UAT)",
    "adsr_other": "ADS-B relayed, non-ICAO address",
    "adsc": "ADS-C, satellite position downlink",
    "mode_s": "transponder replies only -- no position transmitted",
    "other": "Basestation/SBS feed, provenance unknown",
    "mlat": "position MULTILATERATED from receiver geometry",
    "tisb_icao": "non-ADS-B target held by SECONDARY radar, via TIS-B",
    "tisb_trackfile": "target held by PRIMARY/Mode A-C radar, via TIS-B",
    "tisb_other": "TIS-B target on a non-ICAO address",
}


def classify(t: str) -> str:
    if t in OBSERVATION_TYPES:
        return "observation"
    if t in REPORT_TYPES:
        return "report"
    if t in NO_POSITION_TYPES:
        return "no position"
    return "unknown"


# -- the networks ----------------------------------------------------------
#
# Both speak the ADSBexchange v2 shape: {"ac": [...]} or {"aircraft": [...]}.
# Both take a CIRCLE (lat, lon, radius in nautical miles), not a bounding box,
# so the region has to be converted -- see circle_for() below.

#
# Several candidate URLs per network, tried in order until one answers. These
# community APIs move: airplanes.live went feeder-only, adsb.fi renumbered v2
# to v3. A single hard-coded URL turns an API change into what looks like an
# empty sky, which is the one failure this script must never produce.
NETWORKS: dict[str, list[str]] = {
    "adsb.fi": [
        "https://opendata.adsb.fi/api/v3/lat/{lat}/lon/{lon}/dist/{nm}",
        "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{nm}",
    ],
    "adsb.lol": [
        "https://api.adsb.lol/v2/point/{lat}/{lon}/{nm}",
        "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{nm}",
    ],
    "airplanes.live": [
        "https://api.airplanes.live/v2/point/{lat}/{lon}/{nm}",
    ],
}


def circle_for(box: tuple[float, float, float, float]) -> tuple[float, float, float]:
    """Smallest circle covering the box: centre lat/lon and radius in nm.

    These APIs are radial and our config is rectangular, so the circle must
    CIRCUMSCRIBE the box rather than fit inside it -- otherwise the probe
    would miss the corners and quietly undercount. The consequence is that
    results include some aircraft outside AOI_AIR; they are filtered back out
    below, because a count over a footprint that is not the study footprint
    would not be comparable to anything else in the project.
    """
    lomin, lamin, lomax, lamax = box
    clat, clon = (lamin + lamax) / 2, (lomin + lomax) / 2
    half_ns_km = (lamax - lamin) / 2 * 110.57
    half_ew_km = (lomax - lomin) / 2 * 111.32 * math.cos(math.radians(clat))
    radius_km = math.hypot(half_ns_km, half_ew_km)
    return clat, clon, radius_km / 1.852


def fetch(client: httpx.Client, name: str, lat: float, lon: float,
          nm: float, *, working: dict[str, str]
          ) -> tuple[list[dict], str | None]:
    """Try each candidate URL for this network until one answers.

    Once a URL works it is remembered in `working`, so later rounds cost one
    request rather than walking the list again.
    """
    urls = [working[name]] if name in working else NETWORKS[name]
    last = "no candidate URLs"

    for template in urls:
        url = template.format(lat=round(lat, 4), lon=round(lon, 4),
                              nm=int(math.ceil(nm)))
        try:
            r = client.get(url, timeout=20.0, headers={
                "User-Agent": "ANGELS/0.1 (academic research; "
                              "github.com/mcculleyjoshua)",
                "Accept": "application/json",
            })
            if r.status_code != 200:
                last = f"HTTP {r.status_code}"
                time.sleep(1.1)
                continue
            body = r.json()
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(1.1)
            continue

        # The networks disagree on the key and have changed it between
        # versions. Accept either rather than pinning to one and breaking on
        # a version bump we did not ask for.
        for key in ("ac", "aircraft"):
            if isinstance(body.get(key), list):
                working[name] = template
                return body[key], None
        last = f"no aircraft list in response (keys: {list(body)[:6]})"
        time.sleep(1.1)

    return [], last


def in_box(a: dict, box: tuple[float, float, float, float]) -> bool:
    lomin, lamin, lomax, lamax = box
    try:
        lat, lon = float(a["lat"]), float(a["lon"])
    except (KeyError, TypeError, ValueError):
        return False
    return lomin <= lon <= lomax and lamin <= lat <= lamax


# -- reporting -------------------------------------------------------------

def caveats(counts: Counter) -> list[str]:
    """What this source's numbers can and cannot support.

    A CORRECTION, LEFT IN ON PURPOSE. An earlier version of this script
    treated the absence of `mode_s` targets as proof that a feed was filtered
    to positioned ADS-B, and therefore incapable of returning an observation.
    That reasoning was simply wrong, and wrong in an instructive way:

        These endpoints are RADIUS queries. Selecting aircraft within N miles
        of a point requires a position to test. A mode_s target has no
        position at all -- that is what mode_s MEANS -- so it cannot be
        returned by any spatial query, from any provider, ever. Its absence is
        arithmetic, not evidence.

    The lesson generalises past this script, and belongs in the write-up: an
    absence only means something once you have shown the observation was
    POSSIBLE. Half of this project is gap detection, which is the same
    question wearing a different hat, and getting it wrong here first was
    cheap.

    What the absence of mlat and tisb_* DOES mean is handled below -- those
    types carry positions, so a radius query would return them if the feed
    had them.
    """
    out: list[str] = []
    total = sum(counts.values())
    if total < 20:
        out.append("sample too small to conclude anything")
        return out

    if not any(classify(t) == "observation" for t in counts):
        out.append(
            "no mlat and no tisb_* over this sample. Both carry positions, so "
            "a radius query WOULD have returned them -- this is a real "
            "absence from this feed, not a query artefact.")
        out.append(
            "expected, though: aggregators are built from 1090 feeder data "
            "and routinely drop TIS-B and ADS-R, because those targets are "
            "transmitted for a specific nearby client aircraft and would "
            "appear as duplicate ghost tracks in a pooled global feed.")
    return out


def bar(n: int, total: int, width: int = 24) -> str:
    if total <= 0:
        return ""
    filled = int(round(width * n / total))
    return "#" * filled + "." * (width - filled)


def report(name: str, counts: Counter, seen_ids: dict[str, set],
           rounds: int, errors: list[str], ok_rounds: int) -> dict:
    total = sum(counts.values())
    print(f"\n  {name}")
    print("  " + "-" * 72)

    if errors:
        shown = Counter(errors).most_common(3)
        for e, n in shown:
            print(f"    error x{n}: {e}")

    # An unreachable network and a network reporting nothing are completely
    # different findings, and collapsing them is how you conclude there is no
    # observation channel when what actually happened is that a proxy refused
    # the connection. Kept apart here and in the verdict below.
    if ok_rounds == 0:
        print(f"    UNREACHABLE -- {rounds}/{rounds} rounds failed. "
              f"This says nothing about coverage.")
        return {"total": 0, "observation": 0, "types": {},
                "reachable": False, "ok_rounds": 0, "usable": False}

    if not total:
        print(f"    reached in {ok_rounds}/{rounds} rounds, but no aircraft "
              f"inside the study box")
        return {"total": 0, "observation": 0, "types": {},
                "reachable": True, "ok_rounds": ok_rounds, "usable": False}

    obs = sum(n for t, n in counts.items() if classify(t) == "observation")

    for t, n in counts.most_common():
        kind = classify(t)
        mark = " <-- OBSERVATION" if kind == "observation" else ""
        pct = 100 * n / total
        print(f"    {t:<16}{n:>7}  {pct:>5.1f}%  {bar(n, total)}{mark}")
        print(f"    {'':<16}{EXPLAIN.get(t, '?')}")

    print("  " + "-" * 72)
    print(f"    {'positions':<16}{total:>7}   over {rounds} round(s)")
    print(f"    {'observations':<16}{obs:>7}   {100 * obs / total:>5.1f}%")

    uniq = {t: len(s) for t, s in seen_ids.items()}
    obs_uniq = sum(n for t, n in uniq.items() if classify(t) == "observation")
    print(f"    {'distinct craft':<16}{sum(uniq.values()):>7}   "
          f"of which {obs_uniq} observed independently")

    notes = caveats(counts)
    if notes:
        print()
        for n in notes:
            print(f"    note: {n}")

    return {"total": total, "observation": obs, "types": dict(counts),
            "distinct": uniq, "reachable": True, "ok_rounds": ok_rounds,
            "usable": total >= 20}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--polls", type=int, default=5,
                    help="rounds per network (default 5)")
    ap.add_argument("--interval", type=float, default=10.0,
                    help="seconds between rounds (default 10)")
    ap.add_argument("--json", type=str, help="also write the raw counts here")
    args = ap.parse_args()

    clat, clon, nm = circle_for(AOI_AIR)

    print(f"\n  ANGELS -- observation channel probe")
    print(f"  region {REGION_NAME}, air box {AOI_AIR}")
    print(f"  {box_area_km2(AOI_AIR):,.0f} km^2, probed as a "
          f"{nm:.0f} nm circle at {clat:.3f}, {clon:.3f}")
    print(f"  {args.polls} round(s) x {len(NETWORKS)} network(s), "
          f"{args.interval:.0f}s apart\n")

    counts: dict[str, Counter] = defaultdict(Counter)
    ids: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    errors: dict[str, list[str]] = defaultdict(list)
    ok_rounds: Counter = Counter()
    working: dict[str, str] = {}

    with httpx.Client(follow_redirects=True) as client:
        for i in range(args.polls):
            line = f"  round {i + 1}/{args.polls}"
            for name in NETWORKS:
                aircraft, err = fetch(client, name, clat, clon, nm,
                                      working=working)
                if err:
                    errors[name].append(err)
                    line += f"   {name}: {err}"
                    time.sleep(1.1)     # both networks: ~1 req/s
                    continue

                ok_rounds[name] += 1
                inside = [a for a in aircraft if in_box(a, AOI_AIR)]
                n_obs = 0
                for a in inside:
                    t = str(a.get("type", "unknown"))
                    counts[name][t] += 1
                    key = str(a.get("hex") or a.get("r") or a.get("flight") or "?")
                    ids[name][t].add(key)
                    if classify(t) == "observation":
                        n_obs += 1
                line += f"   {name}: {len(inside):>4} in box, {n_obs:>3} observed"
                time.sleep(1.1)

            print(line)
            if i < args.polls - 1:
                time.sleep(max(0.0, args.interval - 2.2 * len(NETWORKS)))

    # -- results ----------------------------------------------------------

    print("\n" + "=" * 76)
    print("  RESULTS")
    print("=" * 76)

    summary = {}
    for name in NETWORKS:
        summary[name] = report(name, counts[name], ids[name],
                               args.polls, errors[name], ok_rounds[name])

    # -- verdict ----------------------------------------------------------

    print("\n" + "=" * 76)
    print("  VERDICT")
    print("=" * 76 + "\n")

    if not any(s["reachable"] for s in summary.values()):
        print("  INCONCLUSIVE -- neither network could be reached.")
        print()
        print("  Every request failed before any aircraft data came back, so")
        print("  this run says nothing at all about whether an observation")
        print("  channel exists. Do not read it as a negative result.")
        print()
        print("  Usual causes, in order of likelihood:")
        print("    - a network/proxy policy blocking these hosts (403 on")
        print("      CONNECT is the signature; corporate and campus networks")
        print("      and sandboxes commonly do this)")
        print("    - no internet connection")
        print("    - the API moved; check the URLs in NETWORKS above")
        print()
        print("  Try again from an unrestricted connection before drawing any")
        print("  conclusion.\n")
        return 2

    best = max(summary.items(), key=lambda kv: kv[1]["observation"])
    name, s = best

    if s["observation"] == 0:
        unreachable = [n for n, v in summary.items() if not v["reachable"]]
        usable = [n for n, v in summary.items() if v.get("usable")]

        if not usable:
            print("  INCONCLUSIVE -- no source returned enough data to say.\n")
            for n in unreachable:
                print(f"    {n:<18} unreachable -- no evidence either way")
            print()
            print("  This is NOT a finding about the sky over Washington.\n")
            return 2

        print("  NO OBSERVATION CHANNEL IS AVAILABLE FROM AGGREGATORS.")
        print(f"  {len(usable)} independent source(s) agree: "
              f"{', '.join(usable)}.")
        if unreachable:
            print(f"  ({', '.join(unreachable)} could not be reached, but the "
                  f"others suffice.)")
        print()
        print("  Read this carefully, because it is a finding about the")
        print("  SUPPLY CHAIN, not about the airspace:")
        print()
        print("    mlat and tisb_* both carry positions, so a radius query")
        print("    would have returned them had the feeds contained any. The")
        print("    absence is real. But it is also EXPECTED, and not because")
        print("    the sky over Washington is unusual.")
        print()
        print("    TIS-B is a client-directed service. An FAA ground station")
        print("    transmits it for the benefit of one specific nearby")
        print("    ADS-B-In aircraft, and its contents are scoped to that")
        print("    aircraft's vicinity. Pooled into a global aggregate it")
        print("    produces duplicate ghost tracks, so aggregators drop it.")
        print("    You cannot buy or query your way to it.")
        print()
        print("    MLAT is scarce here for a different reason: since the 2020")
        print("    ADS-B mandate, essentially everything in Class B airspace")
        print("    broadcasts its own position, leaving few targets that need")
        print("    multilateration in the first place.")
        print()
        print("  So the aggregator route is closed for STRUCTURAL reasons.")
        print("  Another API will not fix it. The channel has to be received.")
        print()
        print("  WHAT TO DO: put up a receiver.")
        print()
        print("    A 1090 MHz RTL-SDR running readsb decodes TIS-B directly")
        print("    (DF18, the messages aggregators strip) whenever a ground")
        print("    station is in line of sight -- and the DC-Baltimore area")
        print("    has dense ADS-B ground infrastructure and heavy GA")
        print("    traffic, which is exactly the condition TIS-B needs.")
        print("    Adding a second dongle on 978 MHz UAT picks up the")
        print("    UAT-link TIS-B as well.")
        print()
        print("    Roughly $60 of hardware. It makes the project independent")
        print("    of whether any community network stays open, gives you a")
        print("    dataset nobody's API carries, and turns 'I queried an API'")
        print("    into 'I built the sensor'.")
        print()
        print("  Fallbacks if the receiver is not possible: ask OpenSky which")
        print("  regions DO have historical MLAT and move the study area, or")
        print("  move the independent channel to maritime, where Sentinel-1")
        print("  SAR is free, global, and needs no hardware.\n")
        return 1

    obs_types = {t: n for t, n in s["types"].items()
                 if classify(t) == "observation"}

    print(f"  An independent observation channel EXISTS over {REGION_NAME}.")
    print(f"  Best source: {name}, {s['observation']} observed positions "
          f"({100 * s['observation'] / s['total']:.1f}% of traffic).\n")
    for t, n in sorted(obs_types.items(), key=lambda kv: -kv[1]):
        print(f"    {t:<16}{n:>6}   {EXPLAIN[t]}")

    if "tisb_trackfile" in obs_types:
        print()
        print("  tisb_trackfile is present, which is the strongest form: a")
        print("  primary-radar track with no ICAO address at all. Those")
        print("  cannot be matched to a Report by identifier -- matching.py")
        print("  must pair them POSITIONALLY via Track.position_at().")

    print()
    print("  What this unblocks: adapters/aviation/*.observations() can be")
    print("  implemented against this instead of OpenSky Trino, and")
    print("  detectors/matching.py becomes writable. You are no longer")
    print("  waiting on anyone's review queue.")
    print()
    print("  Next: start a third collector against this network tonight.")
    print("  Observations cannot be backfilled either.\n")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"t": datetime.now(timezone.utc).isoformat(),
                       "box": list(AOI_AIR), "rounds": args.polls,
                       "networks": summary}, fh, indent=2)
        print(f"  raw counts -> {args.json}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
