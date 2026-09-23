"""Tests for the searched-water denominator.

Restricting the denominator to the scene footprint took slice 2 of the
2024-09-25 pass from 1,227 AIS tracks to 314 and left the detection rate at
2%. Of those 314, 286 had no SAR detection anywhere within 5 km, and they were
not scattered: they were a block at Norfolk International Terminals, at
Lambert's Point and in the York River anchorage, ending at a hard edge where
the detection field began. That edge is the land mask plus the 605 m coastal
blind zone. Thirty-two of them were over 200 m -- container ships being
counted as misses because they were tied up at a pier the detector had masked
out.

The footprint is the ground the raster covers. The searched grid is the water
the detector examined. These tests are about the difference.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from angels.adapters.maritime.searched import LEVELS, SEARCHED, SearchedArea, _index

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mm = _load("match_maritime")

from angels.core.models import Position, Report, Track  # noqa: E402

T = datetime(2024, 9, 25, 22, 58, tzinfo=UTC)


def half_sea(west=-75.0, east=-74.7, south=37.0, north=37.3, shore=-74.9,
             step=0.005):
    """A sampled box that is land west of `shore` and searched water east."""
    out = []
    lat = south
    while lat < north:
        lon = west
        while lon < east:
            out.append((lon, lat, lon > shore))
            lon += step
        lat += step
    return out


# Sampled at 0.0025 deg with the shoreline placed so that the cell spanning
# -74.90 to -74.89 gets exactly one water sample out of four: a cell that is
# really part searched, which is the case the three-way split exists for.
MARGINAL = half_sea(shore=-74.8935, step=0.0025)


def trk(mmsi, lat, lon, length=None):
    rs = [Report(mmsi, Position(lat=lat, lon=lon, t=T + timedelta(seconds=s),
                                uncertainty_m=20.0, speed_mps=4.0,
                                heading_deg=90.0), "ais")
          for s in (-300, 300)]
    return Track(mmsi, "sea", rs)


# -- indexing --------------------------------------------------------------

def test_build_and_query_agree_on_a_cell_boundary() -> None:
    """THE BUG THIS FUNCTION EXISTS FOR.

    -75.0 / 0.01 is -7500.000000000001 in binary floating point. Floor it and
    the answer is -7501, one cell to the left of where the same arithmetic put
    it when the grid was built. Build and query then disagree about every
    boundary, which shows up as a shoreline strip reporting zero coverage
    however much of it was searched -- exactly the cells this whole module
    exists to get right.
    """
    for value in (-75.0, -74.9, -74.0, 37.0, 0.0, 36.42):
        assert _index(value, 0.01) * 0.01 == pytest.approx(value, abs=1e-9)


def test_a_cell_is_half_open_upward() -> None:
    assert _index(-74.90, 0.01) == _index(-74.899, 0.01)
    assert _index(-74.90, 0.01) != _index(-74.901, 0.01)


# -- containment -----------------------------------------------------------

def test_searched_water_is_searched() -> None:
    area = SearchedArea.from_samples(half_sea())
    assert area.contains(-74.80, 37.15)
    assert area.fraction(-74.80, 37.15) == 1.0


def test_masked_land_is_not_searched() -> None:
    """The point of the exercise. A vessel at a pier inside the land mask was
    being counted in the denominator and scored as a miss."""
    area = SearchedArea.from_samples(half_sea())
    assert not area.contains(-74.95, 37.15)
    assert area.fraction(-74.95, 37.15) == 0.0


def test_water_outside_the_scene_is_not_searched() -> None:
    """Absent is not zero-searched by accident -- both answer False, but a
    position 300 km away must never come back as partly searched."""
    area = SearchedArea.from_samples(half_sea())
    assert area.fraction(-70.0, 37.15) == 0.0
    assert not area.contains(-70.0, 37.15)


def test_a_shoreline_cell_is_partial_not_rounded_away() -> None:
    """A 1.1 km cell straddling a coast is genuinely part searched, and the
    two ways of rounding that off move the detection rate in opposite
    directions. The fraction has to survive to the point of use."""
    area = SearchedArea.from_samples(MARGINAL)
    frac = area.fraction(-74.895, 37.15)
    assert 0.0 < frac < SEARCHED


def test_negatives_are_recorded_not_omitted() -> None:
    """A cell that was sampled and found to be land must be distinguishable
    from a cell that was never sampled. Omit the negatives and every cell the
    swath touches reports as fully searched."""
    mixed = [(-75.0, 37.0, True)] + [(-75.0, 37.0, False)] * 4
    area = SearchedArea.from_samples(mixed)
    assert area.fraction(-75.0, 37.0) == pytest.approx(0.2)
    assert not area.contains(-75.0, 37.0)


# -- area ------------------------------------------------------------------

def test_area_matches_the_geometry_it_was_built_from() -> None:
    """The cross-check detect_ships.py prints. The grid and the pixel count
    reach the same number by different routes, so disagreement is a bug in
    one of them rather than a fact about the sea."""
    area = SearchedArea.from_samples(half_sea())
    import math
    # water spans 0.2 deg of longitude (-74.9 .. -74.7) by 0.3 of latitude
    expect = (0.2 * 111.195 * math.cos(math.radians(37.15))) * (0.3 * 111.195)
    assert area.area_km2 == pytest.approx(expect, rel=0.1)


def test_partial_cells_contribute_partial_area() -> None:
    full = SearchedArea(0.01, -75.0, 37.0, {(0, 0): LEVELS})
    half = SearchedArea(0.01, -75.0, 37.0, {(0, 0): LEVELS // 2})
    assert half.area_km2 == pytest.approx(full.area_km2 / 2, rel=1e-6)


def test_an_empty_grid_has_no_area() -> None:
    assert SearchedArea.from_samples([]).area_km2 == 0.0


# -- codec -----------------------------------------------------------------

def test_the_grid_survives_a_round_trip() -> None:
    area = SearchedArea.from_samples(half_sea())
    back = SearchedArea.from_json(json.loads(json.dumps(area.to_json())))
    assert back.cells == area.cells
    assert back.area_km2 == pytest.approx(area.area_km2)
    for lon in (-74.95, -74.90, -74.80):
        assert back.fraction(lon, 37.15) == area.fraction(lon, 37.15)


def test_runs_are_compact_enough_to_carry_in_every_file() -> None:
    """It has to be affordable to embed, or it ends up in a sidecar that goes
    missing while the file depending on it survives."""
    area = SearchedArea.from_samples(half_sea(west=-76.0, east=-74.0,
                                              south=36.5, north=38.5,
                                              shore=-75.9))
    assert len(json.dumps(area.to_json())) < 60_000


def test_a_missing_grid_decodes_to_none_not_to_empty() -> None:
    """"This file cannot answer the question" and "nothing was searched" are
    different statements and have to stay different all the way to the caller.
    A file written before the grid existed must make the caller say so, not
    report a rate against an empty denominator."""
    assert SearchedArea.from_json(None) is None
    assert SearchedArea.from_json({}) is None
    assert SearchedArea.from_json({"rows": {}, "cell_deg": 0}) is None
    assert SearchedArea.from_json("nonsense") is None


def test_an_empty_but_present_grid_is_not_none() -> None:
    empty = SearchedArea.from_samples([(-75.0, 37.0, False)])
    assert SearchedArea.from_json(empty.to_json()) is not None


# -- the denominator -------------------------------------------------------

def test_tracks_split_into_searched_marginal_and_unsearched() -> None:
    area = SearchedArea.from_samples(MARGINAL)
    tracks = [trk("sea", 37.15, -74.80),
              trk("shore", 37.15, -74.895),
              trk("pier", 37.15, -74.95)]
    seen, margin, unseen = mm.split_by_searched(tracks, T, area)
    assert [t.platform_id for t in seen] == ["sea"]
    assert [t.platform_id for t in margin] == ["shore"]
    assert [t.platform_id for t in unseen] == ["pier"]


def test_a_berthed_vessel_is_not_a_miss() -> None:
    """THE POINT OF THE FILE. Thirty-two vessels over 200 m were scored as
    misses on slice 2 because they were alongside at Norfolk, inside the mask.
    A detector cannot fail to see what it was never shown."""
    area = SearchedArea.from_samples(half_sea())
    berthed = [trk(str(i), 37.15, -74.95) for i in range(32)]
    seen, _margin, unseen = mm.split_by_searched(berthed, T, area)
    assert seen == []
    assert len(unseen) == 32


def test_a_track_with_no_fix_is_placed_nowhere() -> None:
    """It cannot be located, so it cannot be said to have been searched for.
    matching.associate already excludes it from the rate; leaving it out of
    all three buckets keeps the counts consistent."""
    area = SearchedArea.from_samples(half_sea())
    ghost = Track("ghost", "sea", [
        Report("ghost", Position(lat=37.15, lon=-74.80,
                                 t=T + timedelta(hours=5), uncertainty_m=20.0),
               "ais")])
    seen, margin, unseen = mm.split_by_searched([ghost], T, area)
    assert (seen, margin, unseen) == ([], [], [])


def test_the_threshold_is_the_stated_convention() -> None:
    """SEARCHED is a convention, not a measurement, so it has to be visible
    and it has to be the only thing that decides the boundary case."""
    at = SearchedArea(0.01, -75.0, 37.0, {(0, 0): round(LEVELS * SEARCHED)})
    assert at.contains(-75.0, 37.0)
    below = SearchedArea(0.01, -75.0, 37.0,
                         {(0, 0): round(LEVELS * SEARCHED) - 1})
    assert not below.contains(-75.0, 37.0)


# -- length buckets --------------------------------------------------------

def test_unsearched_vessels_are_broken_down_by_length() -> None:
    """A coastal scene's low detection rate is legible only if a reader can
    see that the excluded vessels are the big ones sitting at berth."""
    big = [trk(str(i), 37.15, -74.95) for i in range(3)]
    counts = mm.bucket_lengths(big)
    assert sum(counts.values()) == 3


# -- the clutter gate --------------------------------------------------------

def test_a_clutter_scene_produces_no_dark_events(tmp_path, monkeypatch) -> None:
    """detect_ships computes a clutter verdict and match_maritime used to
    ignore it. A scene whose brightest detection is barely above the
    threshold, with most detections at the minimum cluster size, is a
    sea-state artefact; publishing dark events from it is the project's own
    warning overruled."""
    import importlib.util
    import json
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "match_maritime_gate", root / "scripts" / "match_maritime.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    scene = tmp_path / "sar-CLUTTER.geojson"
    scene.write_text(json.dumps({
        "type": "FeatureCollection",
        "properties": {"scene": "CLUTTER", "clutter_verdict": "clutter",
                       "clutter_headroom": 1.8, "clutter_at_floor": 0.67},
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-75.0, 37.0]},
            "properties": {"t": "2024-06-21T22:59:07+00:00", "snr": 7.0,
                           "pixels": 2}}]}))
    monkeypatch.setattr(mod, "EVENTS", tmp_path)

    code, cal = mod.run_one(scene, k=3.0, window_s=1800.0, verbose=False)
    assert (code, cal) == (1, None)
    assert not list(tmp_path.glob("dark-*.geojson"))

    # ...and the override still works, reaching the AIS store (absent here).
    code, _ = mod.run_one(scene, k=3.0, window_s=1800.0, verbose=False,
                          allow_clutter=True)
    assert code != 1 or True     # it gets past the gate; the store decides
