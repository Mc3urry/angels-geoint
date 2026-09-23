"""Tests for the boundary-band analysis.

This script produces the project's headline claim, so the tests are about
the three ways it could produce a confident wrong one: counting candidates
without the searched water under them, pooling passes whose clutter differs,
and taking a p-value from a distribution the data does not follow.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "boundary_analysis", ROOT / "scripts" / "boundary_analysis.py")
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)

NM = mod.NM_M


# -- bands -------------------------------------------------------------------

def test_bands_are_in_nautical_miles_and_fixed_in_advance() -> None:
    assert mod.BANDS_NM[:3] == (0.0, 1.0, 2.0)
    assert mod.band_of(0.5 * NM) == "0-1 nm"
    assert mod.band_of(1.0 * NM) == "1-2 nm"
    assert mod.band_of(24.9 * NM) == "10-25 nm"


def test_beyond_the_search_radius_lands_in_the_far_band_not_the_near_one() -> None:
    """inf is 'further than we looked'. Reading it as 0 would drop every
    far-offshore candidate onto a boundary."""
    assert mod.band_of(float("inf")) == "> 25 nm"
    assert mod.band_of(100 * NM) == "> 25 nm"


# -- the null ----------------------------------------------------------------

def test_candidates_spread_like_the_water_are_not_a_finding() -> None:
    """Twice the water, twice the candidates: nothing to report."""
    per_pass = [(30, {"0-1 nm": 1000.0, "1-2 nm": 2000.0}),
                (30, {"0-1 nm": 1000.0, "1-2 nm": 2000.0})]
    obs = {"0-1 nm": 20, "1-2 nm": 40}
    stat, p = mod.permutation_p(obs, per_pass, trials=500)
    assert stat < 1.0
    assert p > 0.2


def test_a_real_concentration_is_found() -> None:
    per_pass = [(40, {"0-1 nm": 1000.0, "1-2 nm": 9000.0})] * 3
    obs = {"0-1 nm": 90, "1-2 nm": 30}          # 9x the expected 12
    stat, p = mod.permutation_p(obs, per_pass, trials=500)
    assert stat > 50
    assert p < 0.01


def test_expectation_is_computed_within_each_pass() -> None:
    """THE SEA-STATE CONTROL. One rough pass that searched only band A and
    one calm pass that searched only band B must not make band A look
    enriched just because the rough pass found more of everything."""
    per_pass = [(100, {"0-1 nm": 1000.0}),      # rough day, near-shore only
                (10, {"1-2 nm": 1000.0})]       # calm day, offshore only
    obs = {"0-1 nm": 100, "1-2 nm": 10}
    stat, p = mod.permutation_p(obs, per_pass, trials=500)
    assert stat == pytest.approx(0.0, abs=1e-9)
    assert p > 0.5


def test_a_pass_that_searched_nothing_cannot_contribute_expectation() -> None:
    per_pass = [(5, {}), (10, {"0-1 nm": 100.0})]
    stat, p = mod.permutation_p({"0-1 nm": 10}, per_pass, trials=200)
    assert stat == pytest.approx(0.0, abs=1e-9)


def test_the_p_value_is_never_zero() -> None:
    """(worse + 1) / (trials + 1): a permutation p of exactly 0 claims more
    than the number of trials can support."""
    per_pass = [(50, {"0-1 nm": 1.0, "> 25 nm": 999.0})]
    stat, p = mod.permutation_p({"0-1 nm": 50}, per_pass, trials=100)
    assert p == pytest.approx(1 / 101)


def test_the_null_is_reproducible() -> None:
    per_pass = [(20, {"0-1 nm": 500.0, "1-2 nm": 500.0})]
    a = mod.permutation_p({"0-1 nm": 14, "1-2 nm": 6}, per_pass, trials=300)
    b = mod.permutation_p({"0-1 nm": 14, "1-2 nm": 6}, per_pass, trials=300)
    assert a == b


# -- the denominator ---------------------------------------------------------

def test_only_properly_searched_cells_count_as_water() -> None:
    """Part-searched shoreline cells are excluded from the rate's
    denominator, so they must be excluded here too or the two disagree."""
    from angels.adapters.maritime.searched import LEVELS, SearchedArea
    area = SearchedArea(0.01, -75.0, 37.0,
                        {(0, 0): LEVELS, (1, 0): 1, (2, 0): LEVELS // 2})
    cells = list(mod.scene_cells(area))
    assert len(cells) == 2                       # the 10% cell is dropped
    assert cells[0][2] == pytest.approx(0.99, abs=0.05)   # 0.01 deg at 37N


def test_cell_area_shrinks_with_the_searched_fraction() -> None:
    from angels.adapters.maritime.searched import LEVELS, SearchedArea
    full = SearchedArea(0.01, -75.0, 37.0, {(0, 0): LEVELS})
    half = SearchedArea(0.01, -75.0, 37.0, {(0, 0): LEVELS // 2})
    a_full = list(mod.scene_cells(full))[0][2]
    a_half = list(mod.scene_cells(half))[0][2]
    assert a_half == pytest.approx(a_full / 2, rel=0.01)


# -- limit types and the control --------------------------------------------

def test_limit_types_are_read_from_the_attributes(tmp_path, monkeypatch) -> None:
    """NOAA's lines are told apart only by their DBF flags. Merged, a point
    1 nm from the EEZ and 150 nm from the territorial sea is filed as
    '1 nm from a limit', which answers a different question."""
    import struct
    shp, dbf = tmp_path / "limits.shp", tmp_path / "limits.dbf"

    runs = [[(-75.0, 36.0), (-75.0, 40.0)], [(-73.0, 36.0), (-73.0, 40.0)]]
    body = b""
    for i, run in enumerate(runs, 1):
        xs, ys = [p[0] for p in run], [p[1] for p in run]
        shape = struct.pack("<i", 3) + struct.pack("<4d", min(xs), min(ys),
                                                   max(xs), max(ys))
        shape += struct.pack("<ii", 1, len(run)) + struct.pack("<i", 0)
        for x, y in run:
            shape += struct.pack("<2d", x, y)
        body += struct.pack(">ii", i, len(shape) // 2) + shape
    head = struct.pack(">i", 9994) + b"\x00" * 20
    head += struct.pack(">i", (100 + len(body)) // 2)
    head += struct.pack("<ii", 1000, 3) + b"\x00" * 64
    shp.write_bytes(head + body)

    fields = [("FEAT_TYPE", 14), ("TS", 10), ("CZ", 10), ("EEZ", 10)]
    rec_len = 1 + sum(w for _, w in fields)
    hdr_len = 32 + 32 * len(fields) + 1
    h = struct.pack("<B3B", 3, 24, 1, 1) + struct.pack("<IHH", 2, hdr_len,
                                                       rec_len) + b"\x00" * 20
    for name, width in fields:
        h += name.encode().ljust(11, b"\x00") + b"C" + b"\x00" * 4
        h += bytes([width]) + b"\x00" * 15
    h += b"\x0d"
    rows = [("Maritime Limit", "1.0", "0.0", "0.0"),
            ("Maritime Limit", "0.0", "0.0", "1.0")]
    for r in rows:
        h += b" " + b"".join(v.encode().ljust(w) for v, (_, w) in zip(r, fields))
    dbf.write_bytes(h)

    monkeypatch.setattr(mod, "LIMITS", tmp_path)
    sets = mod.limit_sets()
    assert mod.TS_NAME in sets and mod.EEZ_NAME in sets
    assert mod.ANY_NAME in sets
    assert len(sets[mod.TS_NAME]) == 1 and len(sets[mod.ANY_NAME]) == 2
    # the two lines are 2 degrees apart: a point beside one is far from the other
    assert sets[mod.TS_NAME].distance_m(-74.9, 38.0) < 10_000
    assert sets[mod.EEZ_NAME].distance_m(-74.9, 38.0) > 100_000


def test_a_land_boundary_is_not_a_maritime_limit() -> None:
    assert mod._truthy("1.00000000") and not mod._truthy("0.00000000")
    assert not mod._truthy("") and not mod._truthy("abc")


def test_the_bander_caches_without_changing_the_answer() -> None:
    from angels.adapters.maritime.limits import LineSet
    calls = []

    class Counting(LineSet):
        def distance_m(self, lon, lat, **kw):
            calls.append((lon, lat))
            return super().distance_m(lon, lat, **kw)

    b = mod.Bander(Counting([[(-75.0, 36.0), (-75.0, 40.0)]]))
    first = b.band(-74.9, 37.0)
    again = b.band(-74.9, 37.0)
    assert first == again
    assert len(calls) == 1


def test_matched_and_unmatched_split_by_the_dark_file(tmp_path, monkeypatch) -> None:
    """The control group is the exact complement of the dark- file."""
    monkeypatch.setattr(mod, "EVENTS", tmp_path)
    scene = tmp_path / "sar-X.geojson"
    def feat(lon, lat, snr=20, px=10):
        return {"type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {"snr": snr, "pixels": px,
                               "t": "2024-06-21T22:59:07+00:00"}}
    scene.write_text(json.dumps({"type": "FeatureCollection", "features": [
        feat(-75.0, 37.0), feat(-75.1, 37.0), feat(-75.2, 37.0, snr=5)]}))
    (tmp_path / "dark-X.geojson").write_text(json.dumps({
        "type": "FeatureCollection", "features": [feat(-75.1, 37.0)]}))

    date, matched, unmatched = mod.gated_detections(scene, 15.0, 6)
    assert date == "2024-06-21"
    assert matched == [(-75.0, 37.0)]           # the weak one is gated out
    assert unmatched == [(-75.1, 37.0)]


def test_without_a_dark_file_nothing_is_called_matched(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mod, "EVENTS", tmp_path)
    scene = tmp_path / "sar-Y.geojson"
    scene.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "geometry": {"type": "Point", "coordinates": [-75.0, 37.0]},
         "properties": {"snr": 20, "pixels": 10,
                        "t": "2024-06-21T22:59:07+00:00"}}]}))
    date, matched, unmatched = mod.gated_detections(scene, 15.0, 6)
    assert (date, matched, unmatched) == ("2024-06-21", [], [])


# -- signed bands ------------------------------------------------------------

NM = mod.NM_M


def test_the_side_of_a_limit_comes_from_its_companion_line() -> None:
    """Unsigned distance folds 1 nm inside onto 1 nm outside and reports the
    average, which is exactly the step a boundary effect would be."""
    # 12 nm line, companion (24 nm) 12 nm further out
    assert mod.signed_band(0.5 * NM, 12.5 * NM, outer_companion=True) == "-1 to 0 nm"
    assert mod.signed_band(0.5 * NM, 11.5 * NM, outer_companion=True) == "0 to 1 nm"


def test_water_beyond_both_lines_is_seaward_not_landward() -> None:
    """Past the 24 nm line the nearer line is the companion; a naive rule
    flips the sign there and puts the outer ocean inside the territorial
    sea."""
    assert mod.signed_band(20 * NM, 8 * NM,
                           outer_companion=True) == "10 to 25 nm"


def test_the_mirror_case_for_a_limit_with_an_inner_companion() -> None:
    # 24 nm line, companion (12 nm) 12 nm inside it
    assert mod.signed_band(0.5 * NM, 12.5 * NM, outer_companion=False) == "0 to 1 nm"
    assert mod.signed_band(0.5 * NM, 11.5 * NM, outer_companion=False) == "-1 to 0 nm"


def test_signed_bands_are_narrow_at_the_line_and_fixed_in_advance() -> None:
    assert mod.SIGNED_EDGES_NM[4:7] == (-1.0, 0.0, 1.0)
    assert mod.SIGNED_NAMES[0] == "landward > 25 nm"
    assert mod.SIGNED_NAMES[-1] == "seaward > 25 nm"
    assert mod.signed_band(40 * NM, 60 * NM, outer_companion=True) == "landward > 25 nm"


# -- the shift null ----------------------------------------------------------

def test_a_clustered_pattern_is_not_significant_under_the_shift_null() -> None:
    """THE REASON THE SHIFT NULL EXISTS. One tight clump that happens to sit
    in a band looks impossible to a null that scatters points independently,
    and unremarkable to one that slides the clump around."""
    pts = {"2024-06-21": [(-75.0 + 0.001 * i, 37.0) for i in range(60)]}
    area = {"2024-06-21": {"0-1 nm": 500.0, "> 25 nm": 500.0}}

    def band(lon, lat):
        return "0-1 nm" if lat > 36.995 else "> 25 nm"

    expected = {"0-1 nm": 30.0, "> 25 nm": 30.0}
    stat = 30.0                                   # all 60 in one band
    p, used = mod.shift_p(pts, area, band, lambda d, lon, lat: True,
                          expected, stat, trials=200, max_shift_deg=0.05)
    assert used > 100
    assert p > 0.2, "a single clump must not be reported as a certainty"


def test_a_real_relationship_survives_the_shift() -> None:
    """Points sitting on a thin line that is only a tenth of the water stay
    significant: every shift moves them off it, and off it is where the
    water says they should mostly be."""
    pts = {"a": [(-75.0 + 0.01 * i, 37.0) for i in range(60)]}
    area = {"a": {"near": 100.0, "far": 900.0}}     # the line is 10% of it

    def band(lon, lat):
        return "near" if abs(lat - 37.0) < 0.001 else "far"

    p, used = mod.shift_p(pts, area, band, lambda d, lon, lat: True,
                          {"near": 6.0, "far": 54.0}, 540.0, trials=200,
                          max_shift_deg=0.5)
    assert used > 150
    assert p < 0.05


def test_shifted_points_outside_the_searched_water_are_dropped() -> None:
    """A shift that walks the pattern onto land must not be compared against
    water it never searched."""
    pts = {"a": [(-75.0, 37.0)] * 10}
    area = {"a": {"x": 100.0}}
    seen = []

    def searched(date, lon, lat):
        seen.append((lon, lat))
        return False                      # nothing lands anywhere valid

    p, used = mod.shift_p(pts, area, lambda lon, lat: "x", searched,
                          {"x": 10.0}, 1.0, trials=20)
    assert used == 0 and p == 1.0
    assert seen, "the searched test was never consulted"


def test_the_step_test_keeps_only_the_strip_around_the_line() -> None:
    """--near-nm exists because the biggest spatial signal in the box is the
    shore, not any boundary. Over a narrow strip the shore gradient is mild
    and what is left is the step."""
    names = list(mod.SIGNED_NAMES)
    keep = {b for b in names
            if b in mod.SIGNED_NAMES[1:-1]
            and abs(float(b.split()[0])) <= 10 + 1e-9
            and abs(float(b.split()[-2])) <= 10 + 1e-9}
    assert "landward > 25 nm" not in keep and "seaward > 25 nm" not in keep
    assert "-25 to -10 nm" not in keep and "10 to 25 nm" not in keep
    assert "-1 to 0 nm" in keep and "0 to 1 nm" in keep and "5 to 10 nm" in keep


def test_the_shift_null_ignores_points_outside_the_kept_strip() -> None:
    """With a strip, a shifted point that lands outside it is not evidence
    for any band and must not be counted into one."""
    pts = {"a": [(-75.0, 37.0)] * 10}
    area = {"a": {"0 to 1 nm": 100.0}}

    def band_or_none(lon, lat):
        return "0 to 1 nm" if lat > 36.5 else None

    p, used = mod.shift_p(pts, area, band_or_none, lambda d, lon, lat: True,
                          {"0 to 1 nm": 10.0}, 0.0, trials=50,
                          max_shift_deg=1.0)
    assert used > 0


def test_deep_inside_a_bay_is_landward_of_the_outer_limit_too() -> None:
    """THE BUG THIS CAUGHT. The 12 nm line runs across the mouth of the
    Chesapeake, so the head of the bay is ~28 nm INSIDE it. Judged against
    the 24 nm line by the inner-companion rule alone, that water came out
    'seaward > 25 nm' -- 788 bay candidates in the outermost band, making the
    24 nm test look significant when it was the inshore gradient relabelled."""
    # deep in the bay: 40 nm from the 24 nm line, 28 nm from the 12 nm line
    assert mod.signed_band(40 * NM, 28 * NM,
                           outer_companion=False) == "landward > 25 nm"
    # and the same point is landward of the 12 nm line as well
    assert mod.signed_band(28 * NM, 40 * NM,
                           outer_companion=True) == "landward > 25 nm"


def test_open_water_beyond_the_outer_limit_is_still_seaward() -> None:
    """The fix must not swallow the case it was written for."""
    assert mod.signed_band(30 * NM, 42 * NM,
                           outer_companion=False) == "seaward > 25 nm"
    assert mod.signed_band(1.5 * NM, 13.5 * NM,
                           outer_companion=False) == "1 to 2 nm"
