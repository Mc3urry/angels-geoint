"""AIS ingest. The reporting half of the maritime comparison.

PHASE 3. MarineCadastre bulk downloads, clipped to AOI_SEA and stored as
Parquet by scripts/clip_ais.py, turned into the core's Reports and Tracks.

    load(paths, t0=, t1=) -> list[Report]
    to_tracks(reports, split_gap_s=) -> list[Track]
    tracks_at(paths, t, window_s=) -> list[Track]      # what matching wants

WHAT AIS ACTUALLY IS

A vessel broadcasting its own position, identity and intent, unauthenticated,
to anyone listening. Class A transponders are mandatory above 300 gross tons
on international voyages and report every 2-10 seconds underway; Class B is
voluntary, cheaper, lower-powered, and reports every 30 seconds to 3 minutes.
The difference matters here: a Class B vessel is both less likely to be heard
and more likely to be small enough for the radar to miss, and treating the two
as one population confuses a reporting property with a sensing one.

THREE TRAPS, ALL OF WHICH PRODUCE PLAUSIBLE OUTPUT

1. SENTINEL VALUES. AIS encodes "not available" as in-range numbers. Heading
   511, SOG 102.3, COG 360, latitude 91, longitude 181. None of them is an
   error; all of them parse. Take 511 as a heading and every vessel without a
   gyro reading dead-reckons east-south-east; take 102.3 knots as a speed and
   it does so at 52 m/s. Both produce positions, and neither complains.

2. SPLITTING, AND THE HOLE IT CLOSES. One MMSI over a day is not one track.
   A vessel berths, goes quiet for six hours, then sails.

   Track.position_at already defends the MIDDLE of such a gap: its
   interpolation penalty is proportional to how far apart the bracketing
   reports are and peaks halfway between them, so a fix three hours into a
   42 km silence comes back claiming 21 km of uncertainty and
   core.detectors.matching refuses it.

   The penalty falls to zero at both ENDS, and that is where the damage is.
   Measured on exactly that six-hour berth, five minutes before the second
   report:

       unsplit :   607 m claimed uncertainty, 41.6 km from truth -> ACCEPTED
       split   : no usable fix on either segment                 -> refused

   A vessel that sat alongside until it sailed is placed near its destination
   with half a kilometre of claimed precision. Matching then fails to find it
   where it really was -- calling a real detection dark -- and records a miss
   where it never went. One silence, two fabricated results, no warning.

   Splitting is what closes that. The two mechanisms are complementary, not
   redundant: the penalty covers the middle, the split covers the ends.

   But the silence is itself the signal core.detectors.gaps exists to find,
   so splitting is NOT a property of the data. It is a choice belonging to the
   caller: matching wants split tracks, gap detection wants whole ones.
   to_tracks takes split_gap_s=None to keep them intact.

3. THE TIME WINDOW. A clipped national day is still millions of rows and
   matching needs a few minutes either side of one instant. Filtering in SQL
   before anything is materialised is the difference between a second and a
   swap file.

POSITION UNCERTAINTY

AIS does not report it, so it is assumed, and the assumption is stated rather
than buried: 20 m, covering civil GPS error and the timestamp quantisation in
the bulk files. Deliberately NOT inflated for the distance between a ship's
transponder and its radar centroid -- that is a function of hull length and
core.detectors.matching already adds it from the length carried below. Putting
it in both places would double-count it and quietly widen every match radius
by a ship.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ...config import KNOTS_TO_MPS
from ...core.models import Position, Report, Track

# Assumed, not measured. See the module docstring.
POSITION_UNCERTAINTY_M = 20.0

# Silence longer than this starts a new track. Class B reports at up to 3
# minutes even when under way, and a vessel can pass behind terrain or out of
# a receiver's range briefly, so anything shorter splits healthy tracks. Half
# an hour of silence, though, is a vessel that has stopped, moored, or left
# coverage -- and interpolating across it invents a position.
SPLIT_GAP_S = 1800.0

# AIS "not available" encodings. In range, well formed, and wrong.
SOG_UNAVAILABLE = 102.3           # knots
COG_UNAVAILABLE = 360.0           # degrees
HEADING_UNAVAILABLE = 511         # degrees
LAT_UNAVAILABLE = 91.0
LON_UNAVAILABLE = 181.0

# Above this a "speed" is not a speed. The fastest vessels in commercial
# service run about 45 knots; anything beyond is a decoding artefact or a
# corrupted message, and feeding it to dead reckoning throws the vessel
# kilometres in seconds.
MAX_PLAUSIBLE_SOG_KNOTS = 60.0


class AISReadError(RuntimeError):
    """The AIS store could not be read.

    Distinct from finding no vessels. An unreadable file and an empty sea must
    never arrive downstream looking the same -- that mistake has been made in
    this project twice and is the thing the project is about.
    """


@dataclass(frozen=True)
class AISReport(Report):
    """A Report carrying what only AIS knows.

    length_m is read by core.detectors.matching.vessel_length_m through
    getattr, so the core stays unaware that AIS exists while still sizing its
    match radius by the hull it is looking for.
    """

    length_m: float | None = None
    width_m: float | None = None
    draft_m: float | None = None
    name: str | None = None
    vessel_type: int | None = None
    nav_status: int | None = None
    transceiver: str | None = None          # "A" | "B"

    @property
    def is_class_b(self) -> bool:
        return (self.transceiver or "").upper().startswith("B")


# --------------------------------------------------------------------------
# sanitising
# --------------------------------------------------------------------------

def _num(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f            # NaN


def clean_sog_mps(sog_knots) -> float | None:
    """Speed over ground in m/s, or None where AIS meant 'unknown'."""
    v = _num(sog_knots)
    if v is None or v < 0 or v >= SOG_UNAVAILABLE or v > MAX_PLAUSIBLE_SOG_KNOTS:
        return None
    return v * KNOTS_TO_MPS


def clean_heading_deg(cog, heading) -> float | None:
    """Direction of travel, preferring course over ground.

    COG is where the vessel is GOING; heading is where it is POINTING, and in
    a cross-current those differ by several degrees. Dead reckoning needs the
    first. Heading is the fallback only because a stopped vessel often reports
    no COG at all.
    """
    for raw, bad in ((cog, COG_UNAVAILABLE), (heading, HEADING_UNAVAILABLE)):
        v = _num(raw)
        if v is None or v >= bad or v < 0:
            continue
        return v % 360.0
    return None


def _positive(v) -> float | None:
    f = _num(v)
    return f if f is not None and f > 0 else None


def _int(v) -> int | None:
    f = _num(v)
    return int(f) if f is not None else None


def _utc(v) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


# The order clip_ais.py's COPY preserves from the MarineCadastre schema. Named
# explicitly and selected by name rather than read positionally, because a
# column added upstream would otherwise shift every field by one and produce a
# fleet at plausible-looking wrong positions.
COLUMNS = ("MMSI", "BaseDateTime", "LAT", "LON", "SOG", "COG", "Heading",
           "VesselName", "IMO", "CallSign", "VesselType", "Status",
           "Length", "Width", "Draft", "Cargo", "TransceiverClass")


def row_to_report(row: dict) -> AISReport | None:
    """One AIS row to a Report, or None if it does not describe a position."""
    mmsi = row.get("MMSI")
    t = _utc(row.get("BaseDateTime"))
    lat, lon = _num(row.get("LAT")), _num(row.get("LON"))

    if mmsi in (None, "", 0) or t is None or lat is None or lon is None:
        return None
    if abs(lat) >= LAT_UNAVAILABLE or abs(lon) >= LON_UNAVAILABLE:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None

    return AISReport(
        platform_id=str(int(mmsi)) if isinstance(mmsi, (int, float))
        else str(mmsi).strip(),
        position=Position(
            lat=lat, lon=lon, t=t,
            uncertainty_m=POSITION_UNCERTAINTY_M,
            speed_mps=clean_sog_mps(row.get("SOG")),
            heading_deg=clean_heading_deg(row.get("COG"), row.get("Heading")),
            alt_m=_positive(row.get("Draft")),      # draft, for the sea domain
        ),
        source="ais",
        length_m=_positive(row.get("Length")),
        width_m=_positive(row.get("Width")),
        draft_m=_positive(row.get("Draft")),
        name=(str(row["VesselName"]).strip() or None)
        if row.get("VesselName") else None,
        vessel_type=_int(row.get("VesselType")),
        nav_status=_int(row.get("Status")),
        transceiver=(str(row["TransceiverClass"]).strip() or None)
        if row.get("TransceiverClass") else None,
    )


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def _parquet_files(paths) -> list[Path]:
    """Every ais.parquet under the given files, globs or directories."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    out: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            out.extend(sorted(p.rglob("*.parquet")))
        elif any(ch in str(p) for ch in "*?["):
            out.extend(sorted(Path().glob(str(p))))
        elif p.suffix == ".parquet":
            out.append(p)
    return out


def load(paths, *, t0: datetime | None = None, t1: datetime | None = None,
         bbox: tuple[float, float, float, float] | None = None
         ) -> list[AISReport]:
    """Reports from the clipped Parquet store, filtered in SQL.

    t0/t1 bound the time window and bbox the area. Both are pushed into the
    query rather than applied after, because a clipped national day is still
    millions of rows and matching needs minutes of it.

    A path that does not exist raises. A path that exists and holds no rows in
    the window returns an empty list. Those are different answers and the
    caller is entitled to tell them apart.
    """
    files = _parquet_files(paths)
    if not files:
        raise AISReadError(
            f"no Parquet found under {paths}. Run scripts/clip_ais.py first; "
            f"an empty result here would be indistinguishable from a sea with "
            f"no vessels in it."
        )

    try:
        import duckdb
    except ImportError as exc:                # pragma: no cover
        raise AISReadError(
            "duckdb is required to read the AIS store. "
            "pip install -e '.[dev]'"
        ) from exc

    cols = ", ".join(f'"{c}"' for c in COLUMNS)
    src = ", ".join(f"'{f.as_posix()}'" for f in files)
    where = []
    if t0 is not None:
        where.append(f"\"BaseDateTime\" >= TIMESTAMP '{t0:%Y-%m-%d %H:%M:%S}'")
    if t1 is not None:
        where.append(f"\"BaseDateTime\" <= TIMESTAMP '{t1:%Y-%m-%d %H:%M:%S}'")
    if bbox is not None:
        lomin, lamin, lomax, lamax = bbox
        where.append(f'"LON" BETWEEN {lomin} AND {lomax}')
        where.append(f'"LAT" BETWEEN {lamin} AND {lamax}')

    # union_by_name: days clipped from the legacy CSV and from the 2024
    # GeoParquet agree on names but not on every type (Length is an integer
    # in one and a float in the other). Without it DuckDB casts every file to
    # the FIRST file's types, so which days were loaded would decide whether
    # a 23.5 m vessel is 23.5 m long.
    sql = (f"SELECT {cols} FROM read_parquet([{src}], union_by_name=true)"
           + (" WHERE " + " AND ".join(where) if where else "")
           + ' ORDER BY "MMSI", "BaseDateTime"')

    try:
        rows = duckdb.sql(sql).fetchall()
    except Exception as exc:
        raise AISReadError(
            f"could not read the AIS store: {exc}\n"
            f"  This is a failure, not an absence of vessels."
        ) from exc

    out: list[AISReport] = []
    for r in rows:
        rep = row_to_report(dict(zip(COLUMNS, r)))
        if rep is not None:
            out.append(rep)
    return out


# --------------------------------------------------------------------------
# tracks
# --------------------------------------------------------------------------

def to_tracks(reports, *, split_gap_s: float | None = SPLIT_GAP_S,
              min_reports: int = 1) -> list[Track]:
    """Group Reports by MMSI into time-ordered Tracks.

    split_gap_s=None keeps each MMSI whole, which is what core.detectors.gaps
    needs -- the silence is the thing it is looking for, and splitting on it
    would delete every gap before the detector ran.

    Set, it starts a new track after that much silence, which is what matching
    needs: Track.position_at interpolates a straight line between consecutive
    reports, and across a six-hour berth that line runs through open water.
    """
    by_id: dict[str, list[AISReport]] = {}
    for r in reports:
        by_id.setdefault(r.platform_id, []).append(r)

    tracks: list[Track] = []
    for mmsi, rs in by_id.items():
        rs.sort(key=lambda r: r.position.t)

        # Exact duplicate timestamps happen where two receivers heard the same
        # message. Left in, they make a zero-length interval that position_at
        # short-circuits, which is harmless, and inflate report counts, which
        # is not -- coverage modelling later reads those counts as evidence of
        # how well a vessel was heard.
        deduped = [r for i, r in enumerate(rs)
                   if i == 0 or r.position.t != rs[i - 1].position.t]

        if split_gap_s is None:
            segments = [deduped]
        else:
            segments, current = [], [deduped[0]]
            for prev, cur in zip(deduped, deduped[1:]):
                gap = (cur.position.t - prev.position.t).total_seconds()
                if gap > split_gap_s:
                    segments.append(current)
                    current = [cur]
                else:
                    current.append(cur)
            segments.append(current)

        for seg in segments:
            if len(seg) >= min_reports:
                tracks.append(Track(mmsi, "sea", list(seg)))

    tracks.sort(key=lambda tr: (tr.t_start, tr.platform_id))
    return tracks


def tracks_at(paths, t: datetime, *, window_s: float = 1800.0,
              bbox: tuple[float, float, float, float] | None = None,
              split_gap_s: float | None = SPLIT_GAP_S) -> list[Track]:
    """Tracks that can be interpolated to instant t. What matching wants.

    The window has to be wide enough that a vessel reporting sparsely still
    has a report on each side of t -- Track.position_at returns None before a
    track's first report and dead-reckons after its last, and a dead-reckoned
    fix is weaker evidence than an interpolated one. Half an hour either side
    covers Class B's slowest cadence with room to spare.

    Too wide is not free either: it admits vessels that were nowhere near t,
    which position_at then declines to place, and each one depresses the
    detection rate as a track with no fix. Those are excluded from the
    denominator by matching.associate, so the cost is only work -- but it is
    worth knowing that the window is a real parameter and not a formality.
    """
    half = timedelta(seconds=window_s)
    return to_tracks(load(paths, t0=t - half, t1=t + half, bbox=bbox),
                     split_gap_s=split_gap_s)
