"""Maritime limit lines, and distance to them. The governance geometry.

THE RESEARCH QUESTION LIVES HERE

"Does non-cooperative behaviour concentrate at governance discontinuities?"
is a question about distance to lines that exist only in law: the 3 nm state
seaward limit, the 12 nm territorial sea, the 24 nm contiguous zone, the
200 nm EEZ. Nothing in the radar or the AIS knows about them. They come from
NOAA's Maritime Limits and Boundaries, and this module turns them into a
distance any point can be asked for.

NO GEOPANDAS

The analysis needs one operation -- distance from a point to a set of
polylines -- and the geospatial stack is a heavy optional dependency that
this project keeps out of the core. So shapefiles are read directly (the
format is a documented sequence of big-endian and little-endian records) and
GeoJSON with json. Both come out as the same list of lon/lat vertex runs.

DISTANCE IS TO A SEGMENT, NOT TO A VERTEX

Limit lines are sparse: consecutive vertices can be tens of kilometres
apart. Nearest-VERTEX distance would put a point 20 km from a line it is
sitting on. Every distance here is point-to-segment, in metres, on a local
equirectangular approximation -- good to a few metres at these latitudes
and far better than the geolocation of anything being measured.

A GRID INDEX, BECAUSE THE DENOMINATOR IS BIG

Candidates are hundreds; the searched-water cells they are compared against
are over a million. Each query touches only the segments in the nine grid
cells around the point, which is the difference between a minute and a day.
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path

M_PER_DEG_LAT = 111_195.0

# Shapefile shape types that carry vertex runs. Points and multipoints are
# not lines and are skipped rather than silently treated as one.
POLYLINE, POLYGON = 3, 5
POLYLINE_Z, POLYGON_Z = 13, 15
POLYLINE_M, POLYGON_M = 23, 25
LINEAR = {POLYLINE, POLYGON, POLYLINE_Z, POLYGON_Z, POLYLINE_M, POLYGON_M}


def read_geojson(path: Path) -> list[list[tuple[float, float]]]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    parts: list[list[tuple[float, float]]] = []

    def walk(geom) -> None:
        if not geom:
            return
        kind = geom.get("type")
        if kind == "GeometryCollection":
            for g in geom.get("geometries", []):
                walk(g)
        elif kind == "LineString":
            parts.append([(float(x), float(y)) for x, y, *_ in geom["coordinates"]])
        elif kind == "MultiLineString":
            for line in geom["coordinates"]:
                parts.append([(float(x), float(y)) for x, y, *_ in line])
        elif kind == "Polygon":
            for ring in geom["coordinates"]:
                parts.append([(float(x), float(y)) for x, y, *_ in ring])
        elif kind == "MultiPolygon":
            for poly in geom["coordinates"]:
                for ring in poly:
                    parts.append([(float(x), float(y)) for x, y, *_ in ring])

    if doc.get("type") == "FeatureCollection":
        for f in doc.get("features", []):
            walk(f.get("geometry"))
    elif doc.get("type") == "Feature":
        walk(doc.get("geometry"))
    else:
        walk(doc)
    return [p for p in parts if len(p) >= 2]


def read_shapefile(path: Path) -> list[list[tuple[float, float]]]:
    """Every vertex run in the file, attributes discarded."""
    return [run for _, runs in ((None, r) for r in read_shapefile_parts(path))
            for run in runs]


def read_shapefile_parts(path: Path) -> list[list[list[tuple[float, float]]]]:
    """Vertex runs from an ESRI .shp. Geometry only; the .dbf is not read.

    One entry per RECORD, each a list of that record's vertex runs, so a
    record can still be paired with its row in the .dbf.

    The format: a 100-byte header, then records of a big-endian header and a
    little-endian shape. For the linear types the shape is a box, a part
    count, a point count, the part start offsets, then the points.
    """
    data = Path(path).read_bytes()
    if len(data) < 100 or struct.unpack(">i", data[:4])[0] != 9994:
        raise ValueError(f"{path.name} is not a shapefile (bad magic)")

    records: list[list[list[tuple[float, float]]]] = []
    pos = 100
    while pos + 8 <= len(data):
        _, words = struct.unpack(">ii", data[pos:pos + 8])
        pos += 8
        end = pos + words * 2
        shape_type = struct.unpack("<i", data[pos:pos + 4])[0]
        parts: list[list[tuple[float, float]]] = []
        if shape_type in LINEAR:
            n_parts, n_points = struct.unpack("<ii", data[pos + 36:pos + 44])
            off = pos + 44
            starts = list(struct.unpack(f"<{n_parts}i",
                                        data[off:off + 4 * n_parts]))
            off += 4 * n_parts
            xy = struct.unpack(f"<{2 * n_points}d",
                               data[off:off + 16 * n_points])
            bounds = starts + [n_points]
            for a, b in zip(bounds, bounds[1:]):
                run = [(xy[2 * i], xy[2 * i + 1]) for i in range(a, b)]
                if len(run) >= 2:
                    parts.append(run)
        records.append(parts)
        pos = end
    return records


def read_dbf(path: Path) -> list[dict[str, str]]:
    """The attribute table beside a shapefile, as plain strings.

    dBASE III: a 32-byte header, 32 bytes per field, then fixed-width
    records each preceded by a deletion flag. Values are returned as
    stripped text -- the caller knows which ones are numbers, and this stays
    out of the business of guessing.

    NOAA's maritime limits need this. Every line in the file looks the same
    geometrically; only the attributes say which is the 12 nm territorial
    sea, which the 24 nm contiguous zone, and which the EEZ. Merging them,
    as a geometry-only read must, answers a different question from the one
    asked.
    """
    data = Path(path).read_bytes()
    if len(data) < 32:
        raise ValueError(f"{path.name} is too short to be a dbf")
    n_rec, hdr_len, rec_len = struct.unpack("<IHH", data[4:12])
    fields: list[tuple[str, int]] = []
    pos = 32
    while pos < len(data) and data[pos] != 0x0D:
        name = data[pos:pos + 11].split(b"\x00")[0].decode("latin-1")
        fields.append((name, data[pos + 16]))
        pos += 32

    out = []
    for i in range(n_rec):
        start = hdr_len + i * rec_len
        rec = data[start:start + rec_len]
        if len(rec) < rec_len:
            break
        off = 1                      # the deletion flag
        row = {}
        for name, width in fields:
            row[name] = rec[off:off + width].decode("latin-1").strip()
            off += width
        out.append(row)
    return out


def read_shapefile_records(path: Path):
    """[(attributes, [vertex runs])] for a shapefile with its .dbf.

    Records and rows are paired by position, which is what the format
    guarantees. A missing .dbf gives empty attributes rather than an error:
    the geometry is still usable, just unlabelled.
    """
    path = Path(path)
    shapes = read_shapefile_parts(path)
    dbf = path.with_suffix(".dbf")
    rows = read_dbf(dbf) if dbf.exists() else []
    out = []
    for i, runs in enumerate(shapes):
        out.append((rows[i] if i < len(rows) else {}, runs))
    return out


def read_lines(path: Path) -> list[list[tuple[float, float]]]:
    path = Path(path)
    if path.suffix.lower() in (".geojson", ".json"):
        return read_geojson(path)
    if path.suffix.lower() == ".shp":
        return read_shapefile(path)
    raise ValueError(f"cannot read {path.name}: want .geojson or .shp")


def _segment_distance_m(px: float, py: float, ax: float, ay: float,
                        bx: float, by: float, kx: float) -> float:
    """Point-to-segment distance in metres, locally flat."""
    axm, aym = (ax - px) * kx, (ay - py) * M_PER_DEG_LAT
    bxm, bym = (bx - px) * kx, (by - py) * M_PER_DEG_LAT
    dx, dy = bxm - axm, bym - aym
    den = dx * dx + dy * dy
    if den == 0:
        return math.hypot(axm, aym)
    t = max(0.0, min(1.0, -(axm * dx + aym * dy) / den))
    return math.hypot(axm + t * dx, aym + t * dy)


class LineSet:
    """A set of polylines, with fast distance-to-nearest in metres."""

    # Index cell. 0.1 deg is about 11 km: big enough that the nine cells
    # around a point hold any segment that could be nearest for the ranges
    # this project asks about, small enough to keep each cell short.
    CELL_DEG = 0.1

    def __init__(self, parts, name: str = "") -> None:
        self.name = name
        self.segments: list[tuple[float, float, float, float]] = []
        for run in parts:
            for (ax, ay), (bx, by) in zip(run, run[1:]):
                self.segments.append((ax, ay, bx, by))
        self._index: dict[tuple[int, int], list[int]] = {}
        for i, (ax, ay, bx, by) in enumerate(self.segments):
            c0, c1 = sorted((self._c(ax), self._c(bx)))
            r0, r1 = sorted((self._c(ay), self._c(by)))
            # A long segment is registered in every cell its bounding box
            # spans, or a query in the middle of it would find nothing.
            for c in range(c0, c1 + 1):
                for r in range(r0, r1 + 1):
                    self._index.setdefault((c, r), []).append(i)

    @classmethod
    def from_file(cls, path: Path, name: str = "") -> "LineSet":
        path = Path(path)
        return cls(read_lines(path), name or path.stem)

    def _c(self, v: float) -> int:
        return math.floor(round(v / self.CELL_DEG, 9))

    def __len__(self) -> int:
        return len(self.segments)

    def distance_m(self, lon: float, lat: float, *, max_rings: int = 40
                   ) -> float:
        """Metres to the nearest segment; inf if none within max_rings.

        Searches outward ring by ring and continues for ONE ring past the
        first hit: a segment one ring further out can still be nearer than
        one in the far corner of the ring that hit. Stopping at the first hit
        is the classic grid-search error and it is silent -- the answer is
        merely a little too large, which for a distance-to-boundary study
        would move points into the wrong band.

        max_rings caps the search at about 440 km, past the EEZ. Beyond that
        the answer is inf, which callers must treat as "further than this
        study can say", never as zero.
        """
        if not self.segments:
            return float("inf")
        kx = M_PER_DEG_LAT * max(math.cos(math.radians(lat)), 0.01)
        col, row = self._c(lon), self._c(lat)
        best = float("inf")
        seen: set[int] = set()
        hit_ring: int | None = None
        for ring in range(max_rings + 1):
            for dc in range(-ring, ring + 1):
                for dr in range(-ring, ring + 1):
                    if ring and max(abs(dc), abs(dr)) != ring:
                        continue            # only the new ring's cells
                    for i in self._index.get((col + dc, row + dr), ()):
                        if i in seen:
                            continue
                        seen.add(i)
                        ax, ay, bx, by = self.segments[i]
                        d = _segment_distance_m(lon, lat, ax, ay, bx, by, kx)
                        best = min(best, d)
            if hit_ring is None and best < float("inf"):
                hit_ring = ring
            elif hit_ring is not None and ring > hit_ring:
                break
        return best
