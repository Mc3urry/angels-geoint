"""Fetch the reference datasets into data/reference/.

Some sources are a direct download and this script just gets them. Others need
a human -- accepting terms, registering, or picking a region -- and no script
can do that for you. Rather than pretend otherwise, those are listed with
instructions and the script checks whether the file has arrived yet.

So this is a downloader for the easy half and a checklist for the rest.

    python scripts/fetch_reference.py              # get everything automatic
    python scripts/fetch_reference.py --list       # status of all sources
    python scripts/fetch_reference.py --only buzzfeed
    python scripts/fetch_reference.py --phase 2    # just what Phase 2 needs

Everything is clipped to one region. See angels/config.py -- REGION, AOI_AIR
and AOI_SEA are the organising principle of the whole project, and every
download here is chosen to serve them.

Nothing here is committed. data/reference/ is gitignored, which is why this
script exists -- it is what makes the repo reproducible for anyone who clones
it, including you on a different machine in March.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
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
import httpx

from angels.config import REFERENCE

GITHUB_BF = ("https://raw.githubusercontent.com/BuzzFeedNews/"
             "2017-08-spy-plane-finder/master/data")


@dataclass
class Source:
    key: str
    what: str                       # one line: what it is and why you want it
    phase: int
    files: dict[str, str] = field(default_factory=dict)   # dest -> url
    manual: str = ""                # instructions when there is no direct URL
    expect: list[str] = field(default_factory=list)       # dest paths to check

    @property
    def targets(self) -> list[str]:
        return list(self.files) or self.expect


SOURCES: list[Source] = [

    # -- automatic ---------------------------------------------------------

    Source(
        key="buzzfeed",
        phase=2,
        what=("Surveillance-aircraft training set, plus the FAA registry. "
              "train.csv is 97 confirmed federal aircraft against 500 others; "
              "planes_features.csv is 19,799 aircraft with engineered flight "
              "features. faa-registration.csv carries the registrant NAME "
              "field, which is where shell-company patterns appear."),
        files={
            "buzzfeed/train.csv":                f"{GITHUB_BF}/train.csv",
            "buzzfeed/feds.csv":                 f"{GITHUB_BF}/feds.csv",
            "buzzfeed/planes_features.csv":      f"{GITHUB_BF}/planes_features.csv",
            "buzzfeed/candidates.csv":           f"{GITHUB_BF}/candidates.csv",
            "buzzfeed/candidates_annotated.csv": f"{GITHUB_BF}/candidates_annotated.csv",
            "buzzfeed/faa-registration.csv":     f"{GITHUB_BF}/faa-registration.csv",
        },
    ),

    Source(
        key="airports",
        phase=4,
        what=("Global airport locations. Your distance-to-airport control "
              "variable -- dark events cluster near airports for entirely "
              "boring reasons, and the model has to account for that."),
        files={"airports/airports.csv":
               "https://davidmegginson.github.io/ourairports-data/airports.csv",
               "airports/runways.csv":
               "https://davidmegginson.github.io/ourairports-data/runways.csv"},
    ),

    # -- manual ------------------------------------------------------------

    Source(
        key="opensky-aircraft",
        phase=2,
        what="ICAO24 to registration, operator and type. Feeds the dossier view.",
        manual=("Browse https://opensky-network.org/datasets/metadata/ and take "
                "the newest aircraft-database-complete-YYYY-MM.csv. The filename "
                "carries a date so it cannot be hardcoded here, and their monthly "
                "updates are currently on hold.\n"
                "Save as: aircraft-database.csv"),
        expect=["opensky/aircraft-database.csv"],
    ),

    Source(
        key="opensky-trino",
        phase=2,
        what=("One day of every Trino table, as a static download. Lets you "
              "write and test the historical query layer offline while the "
              "access application is in review. Check whether it includes the "
              "MLAT tables -- if so you can build the whole matching layer "
              "before being approved for anything."),
        manual=("https://opensky-network.org/data/scientific -> item 11, "
                "'Complete One Day Trino Tables Snapshot (March 2026)' -> "
                "Dataset Source.\n"
                "LARGE. If per-table files are offered, take state_vectors_data4 "
                "and any MLAT table first and skip the rest.\n"
                "Save under: opensky/trino-snapshot/"),
        expect=["opensky/trino-snapshot"],
    ),

    Source(
        key="dc-airspace",
        phase=4,
        what=("DC Special Flight Rules Area and Flight Restricted Zone. The "
              "sharpest enforced airspace boundary in the country, inside your "
              "AOI. This is your aviation independent variable."),
        manual=("https://dcatlas.dcgis.dc.gov/metadata/AirSpacePly.html -> the "
                "download link for the AirSpacePly layer (shapefile or GeoJSON).\n"
                "Save as: boundaries/dc_airspace.geojson"),
        expect=["boundaries/dc_airspace.geojson"],
    ),

    Source(
        key="faa-sua",
        phase=4,
        what=("FAA Special Use Airspace: prohibited, restricted and warning "
              "areas, including P-56 over the Capitol. Second boundary class, "
              "finer scale than the SFRA."),
        manual=("https://ais-faa.opendata.arcgis.com/datasets/faa::special-use-airspace/explore "
                "-> Download -> GeoJSON.\n"
                "Save as: boundaries/faa_sua.geojson"),
        expect=["boundaries/faa_sua.geojson"],
    ),

    Source(
        key="eez",
        phase=4,
        what=("Exclusive Economic Zone polygons. The maritime independent "
              "variable, and the standard citation in this literature."),
        manual=("https://marineregions.org/downloads.php -> World EEZ. "
                "Requires accepting their terms and giving an email, which is "
                "why this cannot be scripted.\n"
                "Save under: boundaries/eez/"),
        expect=["boundaries/eez"],
    ),

    Source(
        key="xview3",
        phase=3,
        what=("~1,000 annotated Sentinel-1 scenes built for dark-vessel "
              "detection. NOTE: its scenes are global IUU hotspots and almost "
              "certainly do not cover your region. That is fine and it is the "
              "right split of duties -- xView3 is where you TRAIN and BENCHMARK, "
              "your own Sentinel-1 scenes over AOI_SEA are where you DEPLOY."),
        manual=("https://iuu.xview.us/ -> register, then download. VERY large; "
                "start with the validation split rather than the full training "
                "set.\n"
                "Save under: xview3/"),
        expect=["xview3"],
    ),

    Source(
        key="ais",
        phase=3,
        what=("Bulk historical AIS, US waters. The most forgiving place to "
              "develop the maritime pipeline before pointing it anywhere harder."),
        manual=("https://coast.noaa.gov/htdata/CMSP/AISDataHandler/2024/ \n"
                "There is NO region to choose -- since 2015 the files are daily "
                "and NATIONAL: AIS_YYYY_MM_DD.zip, about 320MB each, 116.7GB for "
                "a full year. You pick DAYS, then clip to AOI_SEA yourself.\n"
                "Take the 7 days in config.STUDY_START..STUDY_END (~2.2GB), then "
                "run scripts/clip_ais.py.\n"
                "Save under: ais/"),
        expect=["ais"],
    ),

    Source(
        key="coastline",
        phase=3,
        what="Coastline for SAR land masking. Mask quality drives your false-positive rate.",
        manual=("https://www.naturalearthdata.com/downloads/10m-physical-vectors/ "
                "-> Land, or GSHHG for higher resolution.\n"
                "Save under: boundaries/coastline/"),
        expect=["boundaries/coastline"],
    ),

    Source(
        key="mpa",
        phase=4,
        what="Marine protected area boundaries. A sharper class of maritime discontinuity.",
        manual=("https://www.protectedplanet.net/en/thematic-areas/wdpa -> "
                "filter to marine, download. Requires a free account.\n"
                "Save under: boundaries/mpa/"),
        expect=["boundaries/mpa"],
    ),
]


# --------------------------------------------------------------------------

def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


def present(root: Path, rel: str) -> int | None:
    """Bytes if the target exists, else None. Directories are summed."""
    p = root / rel
    if p.is_file():
        return p.stat().st_size
    if p.is_dir():
        total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        return total if total else None
    return None


def download(url: str, dest: Path, *, chunk: int = 1 << 16) -> int:
    """Stream to a .part file, then rename. An interrupted download never
    leaves something that looks complete.

    Progress is only drawn to a terminal. Redirect the output to a file and
    carriage returns turn into thousands of lines of noise.

    Percentages come from r.num_bytes_downloaded, not from the bytes written.
    content-length is the COMPRESSED size and iter_bytes yields DECOMPRESSED
    bytes, so comparing the two reports things like "338% complete".
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tty = sys.stdout.isatty()
    got = 0

    with httpx.stream("GET", url, follow_redirects=True, timeout=120.0) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        with tmp.open("wb") as fh:
            for block in r.iter_bytes(chunk):
                fh.write(block)
                got += len(block)
                if tty:
                    if total:
                        pct = min(100.0, 100 * r.num_bytes_downloaded / total)
                        print(f"\r    {dest.name}  {pct:5.1f}%  {human(got)}",
                              end="", flush=True)
                    else:
                        print(f"\r    {dest.name}  {human(got)}",
                              end="", flush=True)

    if tty:
        print(f"\r    {dest.name}  {human(got)}" + " " * 20)
    else:
        print(f"    {dest.name}  {human(got)}")

    tmp.replace(dest)
    return got


def show_status(root: Path, sources: list[Source]) -> None:
    for s in sources:
        sizes = [present(root, t) for t in s.targets]
        have = sum(1 for x in sizes if x is not None)
        total = sum(x for x in sizes if x is not None)
        kind = "auto" if s.files else "MANUAL"
        mark = "ok " if have == len(s.targets) else ("-- " if have == 0 else ".. ")
        print(f"{mark}[{kind:>6}] phase {s.phase}  {s.key}"
              f"   {have}/{len(s.targets)}"
              + (f"  {human(total)}" if total else ""))
        for t, sz in zip(s.targets, sizes):
            if sz is None:
                print(f"        missing: {t}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--list", action="store_true", help="status only, download nothing")
    ap.add_argument("--only", help="one source key")
    ap.add_argument("--phase", type=int, help="only sources for this phase")
    ap.add_argument("--force", action="store_true", help="re-download existing files")
    args = ap.parse_args()

    root = REFERENCE
    root.mkdir(parents=True, exist_ok=True)

    sources = SOURCES
    if args.only:
        sources = [s for s in sources if s.key == args.only]
        if not sources:
            print(f"unknown key: {args.only}")
            print("known:", ", ".join(s.key for s in SOURCES))
            return 1
    if args.phase:
        sources = [s for s in sources if s.phase == args.phase]

    if args.list:
        print(f"reference data in {root}\n")
        show_status(root, sources)
        return 0

    got, skipped, failed = 0, 0, 0
    manual: list[Source] = []

    for s in sources:
        if not s.files:
            if not all(present(root, t) for t in s.expect):
                manual.append(s)
            continue

        print(f"\n{s.key}  (phase {s.phase})")
        for rel, url in s.files.items():
            dest = root / rel
            if dest.exists() and not args.force:
                print(f"    {dest.name}  already have {human(dest.stat().st_size)}")
                skipped += 1
                continue
            try:
                download(url, dest)
                got += 1
            except Exception as exc:
                # One dead URL should not stop the rest. Sources move; report
                # it plainly and carry on.
                print(f"\n    FAILED {rel}: {exc}")
                failed += 1

    print(f"\ndownloaded {got}, already had {skipped}, failed {failed}")

    if manual:
        print(f"\n{'=' * 68}")
        print(f"{len(manual)} source(s) need you. No script can accept terms of "
              f"use or pick a region for you.")
        print("=" * 68)
        for s in manual:
            print(f"\n  {s.key}  (phase {s.phase})")
            print(f"    {s.what}")
            for line in s.manual.split("\n"):
                print(f"    > {line}")
        print(f"\nEverything goes under {root}")
        print("Re-run with --list to check what has arrived.")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
