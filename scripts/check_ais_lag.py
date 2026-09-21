"""How far back must a SAR pass be before AIS exists to compare it against?

    python scripts/check_ais_lag.py
    python scripts/check_ais_lag.py --passes        # cross-check scenes on disk
    python scripts/check_ais_lag.py -v              # show every request

THE PROBLEM THIS EXISTS TO CATCH

The maritime argument is a COMPARISON: what the radar saw against what was
reported. One side is a Sentinel-1 acquisition, available within hours. The
other is MarineCadastre AIS, which NOAA's own FAQ puts at 145 to 165 days
behind collection.

So a scene downloaded today cannot be matched today. Nothing says so.
fetch_sar.py ranks passes by recency, detect_ships.py finds vessels in them,
and the missing half surfaces at the point where the project was supposed to
produce a result. The fix is to choose the SAR window to fit the AIS that
EXISTS. Sentinel-1's archive goes back years and costs nothing extra to
search, so that is a free choice as long as it is made deliberately.

THE SECOND PROBLEM, WHICH THE FIRST VERSION OF THIS SCRIPT HAD

It reported "nothing found within 400 days" and offered two guesses as to why.
That output is indistinguishable between three quite different worlds: NOAA
has published nothing for over a year, the URL pattern moved, or the server
refuses the kind of request being made. It reported an ERROR AS AN ABSENCE --
the same failure this whole project is about, committed by the tool meant to
measure it.

So every run now begins with a CONTROL: a date whose file certainly exists,
requested exactly the way the real probes are. If the control fails, the
answer is "this cannot see the server", which is not a fact about AIS. Only
once a control passes does a negative result mean anything.

WHY SEVERAL SOURCES

MarineCadastre is mid-migration. The legacy daily CSV zips live under
coast.noaa.gov; an experimental GeoParquet product lives in Azure blob
storage. Which of them currently reaches furthest forward is a question for
the servers, not for a constant in this file -- so each is probed, with its
own control, and the one that reaches furthest wins.

No file is downloaded. HEAD where it is allowed, and a one-byte ranged GET
where it is not, because plenty of static hosts answer HEAD with 403 while
serving the file perfectly well. A daily national is roughly 320 MB and the
question is only whether it is there.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, timedelta

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

COAST = "https://coast.noaa.gov/htdata/CMSP/AISDataHandler"
AZURE = "https://ocmgeodatastor1.blob.core.windows.net/marinecadastre"

# Their FAQ (June 2026) states 145-165 days from collection to delivery. Kept
# as the expectation to compare a measurement against, never as the answer.
DOCUMENTED_LAG_DAYS = (145, 165)



@dataclass(frozen=True)
class Source:
    """One naming convention, and a date that must exist under it.

    The control is the whole point. A probe with no control cannot tell a
    server that has no recent data from a server it is asking incorrectly,
    and both look like an empty sea.
    """

    name: str
    template: str            # takes a single date via strftime
    control: date

    def url(self, day: date) -> str:
        return day.strftime(self.template)


SOURCES = (
    Source("legacy daily CSV zip",
           f"{COAST}/%Y/AIS_%Y_%m_%d.zip",
           control=date(2022, 6, 15)),
    Source("geoparquet daily",
           f"{AZURE}/ais%Y/AIS_%Y_%m_%d.parquet",
           control=date(2024, 6, 15)),
    Source("geoparquet daily (lowercase)",
           f"{AZURE}/ais%Y/ais_%Y_%m_%d.parquet",
           control=date(2024, 6, 15)),
)


def probe(url: str, timeout: float = 25.0) -> tuple[bool, str]:
    """Does this URL serve a file? Returns (exists, what the server said).

    HEAD first. A 403, 405 or 501 to a HEAD usually means the method is
    refused rather than the file missing, so those fall through to a one-byte
    ranged GET -- which either returns 206 with one byte, or 200 with a body
    this deliberately never reads.
    """
    import httpx

    try:
        r = httpx.head(url, timeout=timeout, follow_redirects=True)
        if r.status_code == 200:
            return True, "200 (HEAD)"
        if r.status_code not in (403, 405, 501):
            return False, f"{r.status_code} (HEAD)"
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__} (HEAD)"

    try:
        with httpx.stream("GET", url, timeout=timeout, follow_redirects=True,
                          headers={"Range": "bytes=0-0"}) as r:
            ok = r.status_code in (200, 206)
            return ok, f"{r.status_code} (ranged GET)"
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__} (ranged GET)"


def newest_available(exists, today: date, earliest: date) -> date | None:
    """The most recent date whose file exists, by bisection.

    THE CONTROL IS THE LOWER BRACKET. It was already verified present, so the
    frontier is somewhere in [earliest, today] and bisection over that whole
    span always finds it -- about eleven requests for a four-year interval.

    The version this replaces searched backwards from today up to a fixed 400
    days and gave up. On the real archive that produced "no source has
    anything within 400 days -- a real publishing gap", stated confidently,
    while the frontier sat just beyond the edge of the search. The horizon of
    the search had been mistaken for the edge of the data: the same error one
    more level up, and the reason the arbitrary constant is gone rather than
    enlarged.

    Availability is assumed MONOTONIC -- everything older than the frontier
    present, everything newer absent. That is how a publishing lag behaves. An
    isolated missing day inside the archive can only make this answer
    conservative, never optimistic, and conservative is the safe direction: it
    sends the SAR window further back than strictly necessary, which costs
    download time. The opposite error would send it forward into dates with no
    ground truth and would not surface until matching produced nothing.
    """
    if today <= earliest:
        return earliest
    if exists(today):
        return today

    lo, hi = earliest, today               # present, absent
    while (hi - lo).days > 1:
        mid = lo + timedelta(days=(hi - lo).days // 2)
        if exists(mid):
            lo = mid
        else:
            hi = mid
    return lo


def looks_annual(newest: date) -> bool:
    """Is the frontier a year boundary rather than a rolling lag?

    A rolling delay puts the frontier on an arbitrary day. A frontier sitting
    on 31 December means the archive is published a year at a time, which is a
    materially different planning fact: there is no "wait a few more weeks",
    only "wait for the next annual release", and no announced date for it.

    Checked with a little tolerance, because the last days of a year can be
    missing for ordinary reasons without changing the cadence.
    """
    return newest.month == 12 and newest.day >= 26


def survey(sources, today: date, verbose: bool = False):
    """Control each source, then find the frontier of the ones that answer.

    Returns (results, any_control_passed) where results is a list of
    (source, control_status, newest_or_None).
    """
    results = []
    any_control = False

    for s in sources:
        ok, said = probe(s.url(s.control))
        if verbose:
            print(f"      control {s.url(s.control)} -> {said}")
        if not ok:
            results.append((s, said, None))
            continue
        any_control = True

        def exists(d: date, _s=s) -> bool:
            got, what = probe(_s.url(d))
            if verbose:
                print(f"      {d} -> {what}")
            return got

        results.append((s, said, newest_available(exists, today, s.control)))

    return results, any_control


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--passes", action="store_true",
                    help="also check the SAR scenes already on disk")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="show every request and what the server said")
    args = ap.parse_args()

    today = date.today()
    print(f"\n  today is {today}\n")
    print("  Each source is checked against a date that MUST exist before its")
    print("  recent dates are believed. A failed control is a fact about this")
    print("  script, not about AIS.\n")

    results, any_control = survey(SOURCES, today, verbose=args.verbose)

    print(f"    {'source':<32}{'control':<22}frontier")
    for s, said, newest in results:
        front = str(newest) if newest else ("-" if said.startswith(("2", "20"))
                                            else "not checked")
        print(f"    {s.name:<32}{said:<22}{front}")
    print()

    if not any_control:
        print("  NO CONTROL PASSED. Every source refused a file that is known")
        print("  to exist, so this run says nothing about how current AIS is.")
        print("  Likely causes, in order: the host is blocking automated")
        print("  requests, the naming moved again, or there is no route out")
        print("  of this machine to those hosts.")
        print()
        print("  Open one in a browser and compare:")
        for s in SOURCES:
            print(f"    {s.url(s.control)}")
        print()
        print(f"  The Azure container can also be listed:\n"
              f"    {AZURE}?restype=container&comp=list&maxresults=20\n")
        return 2

    live = [(s, n) for s, _, n in results if n]

    best_source, newest = max(live, key=lambda p: p[1])
    lag = (today - newest).days
    lo, hi = DOCUMENTED_LAG_DAYS
    verdict = "as documented" if lo <= lag <= hi else "DIFFERENT from the FAQ"

    print(f"  furthest forward           {best_source.name}")
    print(f"  newest AIS day available   {newest}")
    print(f"  lag                        {lag} days ({verdict}; FAQ says "
          f"{lo}-{hi})")

    if looks_annual(newest):
        print()
        print(f"  CADENCE: the frontier is {newest}, the last day of a year.")
        print("  That is not a rolling delay of a few months with the tail")
        print("  trimmed -- it is annual publication. The next tranche is a")
        print("  whole year arriving at once, on no announced date, so there")
        print(f"  is no point waiting for {newest.year + 1} to trickle in. Build")
        print(f"  the study inside {newest.year} or earlier.")

    if newest == best_source.control:
        print()
        print("  WARNING: the frontier landed exactly on the control date, so")
        print("  nothing newer than the control could be found. Either this")
        print("  source stopped there, or its naming changed after that date.")
        print("  Move the control forward and re-run before trusting this.")
    print()
    print("  A Sentinel-1 pass can be matched against AIS only if acquired")
    print(f"  on or before  ->  {newest}")
    print(f"  pattern       ->  {best_source.url(newest)}")
    print()

    if args.passes:
        from angels.config import RAW
        sar = sorted((RAW / "sar").glob("*.zip"))
        if not sar:
            print("  No scenes on disk to check.\n")
            return 0
        print(f"  {len(sar)} scene(s) on disk:\n")
        stranded = 0
        for p in sar:
            try:
                stamp = p.name.split("_")[4][:8]
                when = date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))
            except (IndexError, ValueError):
                print(f"    ?           {p.name[:52]}")
                continue
            ok = when <= newest
            stranded += not ok
            flag = "matchable" if ok else f"NO AIS for {(when - newest).days}d"
            print(f"    {when}  {flag:<22}{p.name[:42]}")
        print()
        if stranded:
            print(f"  {stranded} scene(s) cannot be matched yet. They are the")
            print("  forward half of the study and keep their value, but when")
            print("  their AIS appears is NOT KNOWN -- see the cadence note")
            print("  above rather than assuming the FAQ's figure.")
            print("  The analysis window has to be built from dates that")
            print(f"  already have both halves: on or before {newest}.\n")
        else:
            print("  Every scene on disk has AIS available.\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
