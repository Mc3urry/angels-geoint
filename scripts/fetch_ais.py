"""Download the MarineCadastre daily national AIS files a pass needs.

    python scripts/fetch_ais.py                 # every date with a SAR scene
    python scripts/fetch_ais.py --date 2024-09-25
    python scripts/fetch_ais.py --list          # what it would fetch
    python scripts/fetch_ais.py --clip          # then run clip_ais.py

THE DATES COME FROM THE SCENES, NOT FROM YOU

Typing a date is one more place to be wrong, and a wrong date here does not
error -- it downloads a real file full of real vessels that were nowhere near
the radar, matching finds almost no partners, and the run reports a sea full
of dark vessels. So by default the dates are read out of the Sentinel-1
filenames already on disk, which is the only copy of that information nobody
has retyped.

NO TERMS TO ACCEPT

fetch_reference.py lists AIS as MANUAL because its AccessAIS custom-order tool
requires a form. The BULK daily files do not -- they are public domain (CC0)
and served as plain static files, which is why this script can exist at all.

ABOUT 320 MB PER DAY

One day of all US waters, to extract a few hundred square miles of Chesapeake
and Delaware Bay. That ratio is why clip_ais.py exists and why nothing
downstream ever touches a national file again. Downloads resume: interrupted
transfers pick up with a Range request rather than starting over.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError as _e:          # pragma: no cover - import plumbing
    if _e.name != "_bootstrap":
        raise

from angels.config import AIS_FRONTIER, RAW, REFERENCE

BASE = "https://coast.noaa.gov/htdata/CMSP/AISDataHandler"
DEST = REFERENCE / "ais"

# A national day is 250-400 MB. Anything far under that is a truncated
# transfer or an error page saved with a .zip extension, and it would fail
# later inside DuckDB with a message about Parquet rather than about the
# download.
MIN_PLAUSIBLE_BYTES = 100 * 1024 * 1024


def url_for(day: date) -> str:
    return f"{BASE}/{day.year}/AIS_{day:%Y_%m_%d}.zip"


def dest_for(day: date) -> Path:
    return DEST / f"AIS_{day:%Y_%m_%d}.zip"


def dates_from_scenes(sar_dir: Path | None = None) -> list[date]:
    """Acquisition dates of every Sentinel-1 scene on disk.

    Field 4 of the SAFE name is the acquisition start -- the same field
    detect_ships.py parses for the instant AIS is interpolated to. Read from
    the filename rather than the file's modification time, which is when it
    was downloaded.
    """
    sar_dir = sar_dir or (RAW / "sar")
    out: set[date] = set()
    for p in sorted(sar_dir.glob("*.zip")):
        try:
            stamp = p.name.split("_")[4][:8]
            out.add(date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8])))
        except (IndexError, ValueError):
            continue
    return sorted(out)


def already_have(day: date) -> bool:
    p = dest_for(day)
    return p.exists() and p.stat().st_size >= MIN_PLAUSIBLE_BYTES


def download(day: date, *, timeout: float = 60.0) -> tuple[bool, str]:
    """Fetch one national day, resuming a partial file if there is one."""
    import httpx

    url, out = url_for(day), dest_for(day)
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_suffix(".zip.part")
    have = part.stat().st_size if part.exists() else 0

    headers = {"Range": f"bytes={have}-"} if have else {}
    t0 = datetime.now()

    try:
        with httpx.stream("GET", url, headers=headers, timeout=timeout,
                          follow_redirects=True) as r:
            if r.status_code == 404:
                return False, ("404 -- not published. Check the frontier with "
                               "scripts/check_ais_lag.py")
            if have and r.status_code == 200:
                # The server ignored the Range header and is sending the whole
                # file. Appending would splice two copies together into
                # something that unzips to nonsense, so start clean.
                have = 0
                part.unlink(missing_ok=True)
            elif have and r.status_code != 206:
                return False, f"HTTP {r.status_code} on resume"
            elif not have and r.status_code != 200:
                return False, f"HTTP {r.status_code}"

            total = int(r.headers.get("content-length", 0)) + have
            done = have
            with part.open("ab" if have else "wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = 100 * done / total
                        print(f"\r    {day}  {pct:5.1f}%  "
                              f"{done >> 20:,}/{total >> 20:,} MB", end="")
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

    size = part.stat().st_size
    if size < MIN_PLAUSIBLE_BYTES:
        return False, (f"only {size >> 20} MB -- too small for a national day. "
                       f"Left as {part.name} rather than renamed, so a later "
                       f"run does not mistake it for a complete file.")

    part.replace(out)
    secs = max((datetime.now() - t0).total_seconds(), 0.001)
    return True, f"{size >> 20:,} MB in {secs / 60:.1f} min ({size / secs / 1e6:.1f} MB/s)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date", action="append", metavar="YYYY-MM-DD",
                    help="a specific day; repeatable. Default: every date "
                         "with a SAR scene on disk")
    ap.add_argument("--list", action="store_true", help="show and stop")
    ap.add_argument("--clip", action="store_true",
                    help="run clip_ais.py afterwards")
    args = ap.parse_args()

    if args.date:
        try:
            days = [date.fromisoformat(d) for d in args.date]
        except ValueError as exc:
            print(f"\n  bad date: {exc}\n")
            return 2
    else:
        days = dates_from_scenes()
        if not days:
            print(f"\n  No Sentinel-1 scenes in {RAW / 'sar'}, so no dates to")
            print("  derive. Run scripts/fetch_sar.py --download 1 first, or")
            print("  name a day with --date.\n")
            return 1

    frontier = date.fromisoformat(AIS_FRONTIER)
    usable = [d for d in days if d <= frontier]
    stranded = [d for d in days if d > frontier]

    print(f"\n  {len(days)} date(s) from {len(dates_from_scenes())} scene "
          f"date(s) on disk" if not args.date else f"\n  {len(days)} date(s)")
    if stranded:
        print(f"\n  {len(stranded)} past the AIS frontier of {AIS_FRONTIER} "
              f"and not published:")
        for d in stranded:
            print(f"    {d}  skipped")

    todo = [d for d in usable if not already_have(d)]
    have = [d for d in usable if already_have(d)]
    for d in have:
        print(f"    {d}  already downloaded")

    if not todo:
        print("\n  Nothing to fetch.\n")
        return 0

    print(f"\n  fetching {len(todo)} day(s) -> {DEST}")
    print(f"  about {len(todo) * 320 / 1024:.1f} GB at roughly 320 MB each\n")
    for d in todo:
        print(f"    {d}  {url_for(d)}")
    if args.list:
        print()
        return 0

    print()
    ok = 0
    for d in todo:
        good, msg = download(d)
        print(f"\r    {d}  {'OK  ' if good else 'FAIL'}  {msg}"
              + " " * 20)
        ok += good

    print(f"\n  {ok} of {len(todo)} downloaded.")
    if ok < len(todo):
        print("  Re-run to resume the rest; partial transfers pick up where")
        print("  they stopped.")

    if args.clip and ok:
        print("\n  clipping to AOI_SEA...\n")
        import subprocess
        return subprocess.call([sys.executable,
                                str(Path(__file__).with_name("clip_ais.py"))])

    print("\n  Next: python scripts/clip_ais.py\n")
    return 0 if ok == len(todo) else 1


if __name__ == "__main__":
    sys.exit(main())
