"""Writing live AIS to disk, in the schema the bulk archive already uses.

WHY THIS SCHEMA AND NOT THE AVIATION ONE

The aviation collector stores POLLED SNAPSHOTS -- every state vector in the
box, every thirty seconds -- because that is the shape OpenSky hands over.
Copying that here would have been the obvious move, and it would have been
wrong twice.

First, AIS is a PUSH feed: messages arrive when a vessel transmits, and
re-sampling them onto a fixed grid destroys the reporting cadence. That
cadence is not incidental -- it is the measurement the maritime reception
grid is built on (adapters/maritime/coverage.py): in water a receiver covers,
consecutive reports from one vessel arrive about a minute apart, and where
coverage fades the same vessels report in bursts. Resample to 30 s and that
signal is gone, replaced by an artefact of how often we chose to look.

Second, and decisively: this project ALREADY HAS a maritime archive format.
MarineCadastre's bulk AIS is one row per report with a fixed set of column
names, and ais.py, coverage.py, clip_ais.py, ais_coverage.py and
match_maritime.py all read it. Writing anything else would have created a
second maritime format that every one of those tools would need teaching, for
no gain. So the live collector writes ais.COLUMNS, one row per position
report, and the entire existing analysis stack runs on live-collected data
without being touched.

    MMSI  BaseDateTime  LAT  LON  SOG  COG  Heading  VesselName  IMO
    CallSign  VesselType  Status  Length  Width  Draft  Cargo
    TransceiverClass

WHAT IS AND IS NOT DEDUPLICATED

One row per (MMSI, BaseDateTime), and the memory of what has been written is
PER VESSEL AND PERSISTENT, not per output file.

That distinction was a bug before it was a design note. The collector reads
the whole table once a second and queues every vessel in it, so a vessel that
reports every three minutes is offered ~180 times with the same timestamp.
A dedup set cleared on each flush caught the repeats inside a two-minute
window and missed the ones that straddled it -- roughly two rows per report
instead of one. Nothing would have crashed; the archive would simply have
contained duplicate positions at zero-second intervals, and the reception
grid, which measures coverage by the MEDIAN GAP BETWEEN ONE VESSEL'S
REPORTS, would have read that as a feed twice as attentive as it is. An
error that flatters the coverage measurement is the worst kind this project
can make, because coverage is what licenses every dark-vessel claim.

So the archive keeps the last written timestamp per MMSI, and a vessel is
written again only when its own clock has moved on. The map is bounded by
the number of vessels heard, and entries untouched for an hour are dropped.

STATIC DATA IS CARRIED, NOT STORED SEPARATELY

Name, type and dimensions arrive in their own messages, minutes apart from
the positions. The bulk format repeats them on every row, so this does too,
filling from whatever the vessel has most recently declared. A row whose
static fields are empty means the vessel had not identified itself YET, not
that it refused to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:                                    # pragma: no cover
    from angels.adapters.maritime.aisstream import Vessel

log = logging.getLogger("ingest")

# The bulk AIS column names, in the bulk order. Imported rather than retyped
# so the two can never drift; a mismatch here would be silent, because
# read_parquet selects by name and a missing column simply comes back null.
from angels.adapters.maritime.ais import COLUMNS  # noqa: E402

# Kept out of ais.COLUMNS deliberately. `received_at` is OUR clock -- when the
# socket handed us the message -- and BaseDateTime is the vessel's own. They
# differ by the network, and the difference is the only measure of relay lag
# this archive can ever have. The bulk files have no equivalent, so it is an
# extra column rather than a redefinition of an existing one.
EXTRA = ("received_at",)


def schema():
    """pyarrow schema. Imported lazily: the test suite and the front end both
    import this module without ever writing a file."""
    import pyarrow as pa

    return pa.schema([
        ("MMSI", pa.string()),
        ("BaseDateTime", pa.timestamp("s", tz="UTC")),
        ("LAT", pa.float64()),
        ("LON", pa.float64()),
        ("SOG", pa.float64()),
        ("COG", pa.float64()),
        ("Heading", pa.float64()),
        ("VesselName", pa.string()),
        ("IMO", pa.string()),
        ("CallSign", pa.string()),
        ("VesselType", pa.int32()),
        ("Status", pa.int32()),
        ("Length", pa.float64()),
        ("Width", pa.float64()),
        ("Draft", pa.float64()),
        ("Cargo", pa.int32()),
        ("TransceiverClass", pa.string()),
        ("received_at", pa.timestamp("s", tz="UTC")),
    ])


def row_from_vessel(v: Vessel, received_at: float) -> dict[str, Any] | None:
    """One archive row from a vessel's current state, or None if it has no
    position to record.

    The timestamp is the vessel's OWN t_position, not the wall clock at the
    moment we serialise it. Using now() would compress every vessel in a
    flush into the same instant and manufacture a cadence of zero.
    """
    if not v.t_position:
        return None
    lat, lon = v.lat, v.lon
    if lat is None or lon is None:
        return None
    # 91 and 181 are the AIS "not available" sentinels; they are positions in
    # the sense that a number is present, and nowhere in the sense that
    # matters. ais.row_to_report drops them on read; dropping them on write
    # too keeps the archive from carrying rows nothing will ever use.
    if abs(lat) >= 91.0 or abs(lon) >= 181.0:
        return None

    return {
        "MMSI": str(v.mmsi),
        "BaseDateTime": datetime.fromtimestamp(v.t_position, tz=UTC),
        "LAT": float(lat),
        "LON": float(lon),
        "SOG": v.sog_kn,
        "COG": v.cog_deg,
        "Heading": v.heading_deg,
        "VesselName": v.name,
        "IMO": None,                       # aisstream does not relay it
        "CallSign": v.call_sign,
        "VesselType": v.ship_type,
        "Status": v.nav_status,
        "Length": v.length_m,
        "Width": v.width_m,
        "Draft": v.draught_m,
        "Cargo": None,
        "TransceiverClass": v.ais_class,
        "received_at": datetime.fromtimestamp(received_at, tz=UTC),
    }


@dataclass
class AISArchive:
    """Buffers rows and writes hour-partitioned Parquet.

    Same `hour=YYYYMMDDHH/` layout as the aviation archive, because the two
    are browsed, backed up and reasoned about by the same person, and a second
    directory convention is a second thing to remember at 2 a.m.
    """

    root: Path
    dataset: str = "maritime-live"
    #: How many rows to hold before writing. A national subscription produces
    #: thousands a minute, and one file per handful would make a directory
    #: that takes longer to list than to read.
    flush_rows: int = 20_000
    #: ...but never hold them longer than this, however quiet the sea. A
    #: collector killed with an hour of unwritten buffer has lost an hour.
    flush_seconds: float = 120.0

    #: How long a vessel is remembered after its last written report. Long
    #: enough to cover any real reporting interval -- an anchored Class A is
    #: three minutes, a Class B at rest six -- and short enough that a busy
    #: season does not accumulate a map of every MMSI ever heard.
    forget_after_s: float = 3600.0

    rows: list[dict[str, Any]] = field(default_factory=list)
    #: MMSI -> the BaseDateTime already written for it, unix seconds.
    last_t: dict[str, int] = field(default_factory=dict)
    #: MMSI -> when we last touched it, for expiry.
    last_touch: dict[str, float] = field(default_factory=dict)
    n_written: int = 0
    n_files: int = 0
    n_duplicates: int = 0
    last_flush: float = 0.0

    def add(self, row: dict[str, Any] | None, now: float | None = None) -> bool:
        """Queue one row. False if this vessel's clock has not moved on.

        Strictly greater, not merely different: aisstream can redeliver an
        older position after a newer one, and accepting it would write a
        report that travels backwards in time. Every reader sorts by
        (MMSI, BaseDateTime), so that row would not crash anything -- it
        would silently become a negative interval in the cadence.
        """
        if row is None:
            return False
        mmsi = row["MMSI"]
        t = int(row["BaseDateTime"].timestamp())
        prev = self.last_t.get(mmsi)
        if prev is not None and t <= prev:
            self.n_duplicates += 1
            return False
        self.last_t[mmsi] = t
        self.last_touch[mmsi] = now if now is not None else t
        self.rows.append(row)
        return True

    def forget(self, now: float) -> int:
        """Drop vessels not heard for forget_after_s. Returns how many."""
        cutoff = now - self.forget_after_s
        stale = [m for m, seen in self.last_touch.items() if seen < cutoff]
        for m in stale:
            self.last_touch.pop(m, None)
            self.last_t.pop(m, None)
        return len(stale)

    def due(self, now: float) -> bool:
        if not self.rows:
            return False
        return (len(self.rows) >= self.flush_rows
                or now - self.last_flush >= self.flush_seconds)

    def flush(self, now: float, *, when: datetime | None = None) -> Path | None:
        """Write the buffer. Returns the path, or None if there was nothing.

        Partitioned by the hour the rows were WRITTEN, not the hour each row
        belongs to. A flush spanning an hour boundary therefore puts a few
        stragglers in the newer partition -- which the readers do not care
        about, since every one of them filters on BaseDateTime rather than
        trusting the directory name.
        """
        import pyarrow as pa
        import pyarrow.parquet as pq

        if not self.rows:
            return None
        when = when or datetime.now(UTC)
        part = self.root / self.dataset / f"hour={when:%Y%m%d%H}"
        part.mkdir(parents=True, exist_ok=True)
        path = part / f"ais_{when:%Y%m%dT%H%M%S}.parquet"

        cols = {name: [r.get(name) for r in self.rows]
                for name in (*COLUMNS, *EXTRA)}
        table = pa.Table.from_pydict(cols, schema=schema())
        pq.write_table(table, path, compression="snappy")

        self.n_written += len(self.rows)
        self.n_files += 1
        self.rows.clear()
        self.last_flush = now
        # What has been written is NOT forgotten here. See the module
        # docstring: clearing it per flush was the bug that would have
        # doubled every slow-reporting vessel.
        self.forget(now)
        return path

    @property
    def pending(self) -> int:
        return len(self.rows)
