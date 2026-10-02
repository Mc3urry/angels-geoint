"""What may and may not become a limit line.

THE FAILURE THIS FILE EXISTS FOR, 2026-10-02.

BOEM answered HTTP 500. The fallback fetched 153 MB of 483 NOAA
state-submerged-lands POLYGONS. Every guard passed it:

  * `describe_sla` checked that the JSON parsed, that there was no error
    object, that features existed and that nothing was truncated. It printed
    `BDRY_NAME_TEXT: None` and said OK.
  * the file was deliberately named `submerged_lands_state_polygons.geojson`
    so it would not claim to be the line -- but `limit_sets` labels any file
    whose stem contains "submerged", "3nm" or "sla" as the 3 nm limit, and
    `read_lines` turns polygon rings into runs by design.
  * `"any limit"` was the union of every group in the folder, so the
    polygons joined it.

483 polygons tiling the US coast became the 3 nm state seaward limit. 548
detections moved into the within-2 nm band, 730 left the beyond-10 nm band,
and the headline ratio went from 0.396 to 5.938 -- the sign flipped, toward
the hypothesis. Nothing in the pipeline said anything was wrong except the
one comparability check in `correct_clutter`, which reported it as "not
comparable to the published ones" rather than as a corrupted input.

Nothing here touches the network.
"""

from __future__ import annotations

import json

import pytest

from scripts import boundary_analysis as ba
from scripts.fetch_limits import describe_sla

NM_M = 1852.0


def gj(tmp_path, name, geoms, props=None):
    p = tmp_path / name
    p.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "geometry": g,
                      "properties": dict(props or {})} for g in geoms],
    }), encoding="utf-8")
    return p


def ring(lon=-75.0, lat=37.0, d=0.5):
    return {"type": "Polygon", "coordinates": [[
        [lon, lat], [lon + d, lat], [lon + d, lat + d],
        [lon, lat + d], [lon, lat]]]}


def meridian(lon=-75.0):
    return {"type": "LineString",
            "coordinates": [[lon, 36.0], [lon, 38.0], [lon, 40.0]]}


# -- describe_sla -----------------------------------------------------------

def test_describe_sla_refuses_polygons(tmp_path) -> None:
    """A legal limit is a line. This is the exact response that got through."""
    ok, msg = describe_sla(gj(tmp_path, "submerged.geojson", [ring(), ring()],
                              {"statute": "43 USC 1301"}))
    assert not ok
    assert "Polygon" in msg
    assert "not a line" in msg


def test_describe_sla_refuses_a_layer_with_no_identifying_field(tmp_path) -> None:
    """BDRY_NAME_TEXT is what separates the seaward line from the lateral
    state boundaries. Absent means this is not the layer that was asked
    for, whatever the filename says."""
    ok, msg = describe_sla(gj(tmp_path, "submerged.geojson",
                              [meridian(), meridian()]))
    assert not ok
    assert "no BDRY_NAME_TEXT" in msg


def test_describe_sla_still_accepts_the_real_shape(tmp_path) -> None:
    ok, msg = describe_sla(gj(tmp_path, "submerged.geojson", [meridian()],
                              {"BDRY_NAME_TEXT": "Submerged Lands Act Boundary"}))
    assert ok
    assert "Submerged Lands Act Boundary" in msg
    assert "LineString" in msg


# -- limit_sets -------------------------------------------------------------

def test_a_polygon_file_named_like_the_sla_line_is_refused(tmp_path,
                                                           monkeypatch,
                                                           capsys) -> None:
    """THE REGRESSION. Filename says submerged, contents are rings."""
    gj(tmp_path, "submerged_lands_state_polygons.geojson", [ring(), ring()])
    monkeypatch.setattr(ba, "LIMITS", tmp_path)
    sets = ba.limit_sets()
    assert ba.SLA_NAME not in sets
    assert "REFUSED" in capsys.readouterr().out


def test_a_refused_file_leaves_any_limit_empty_not_wrong(tmp_path,
                                                         monkeypatch) -> None:
    """The point is not that it is refused loudly. It is that nothing
    inherits it: a corrupted 'any limit' moves every band assignment while
    each named limit still looks right."""
    gj(tmp_path, "submerged_lands_state_polygons.geojson", [ring()])
    monkeypatch.setattr(ba, "LIMITS", tmp_path)
    assert ba.limit_sets() == {}


def test_an_unknown_line_file_is_loaded_but_kept_out_of_any_limit(
        tmp_path, monkeypatch) -> None:
    """A file that is lines but is not one of the four legal limits is still
    readable under its own name -- and must not silently redefine the
    pooled test the headline is quoted from."""
    gj(tmp_path, "some_other_line.geojson", [meridian()])
    monkeypatch.setattr(ba, "LIMITS", tmp_path)
    sets = ba.limit_sets()
    assert "some_other_line" in sets
    assert ba.ANY_NAME in sets
    assert len(sets[ba.ANY_NAME].segments) == 0


def test_a_line_file_named_like_the_sla_does_become_the_sla(tmp_path,
                                                            monkeypatch) -> None:
    """The guard must not refuse the real thing."""
    gj(tmp_path, "submerged_lands_act_3nm.geojson", [meridian()])
    monkeypatch.setattr(ba, "LIMITS", tmp_path)
    sets = ba.limit_sets()
    assert ba.SLA_NAME in sets
    # a three-vertex meridian is two segments
    assert len(sets[ba.ANY_NAME].segments) == 2


def test_any_limit_is_the_union_of_the_legal_lines(tmp_path,
                                                   monkeypatch) -> None:
    gj(tmp_path, "submerged_lands_act_3nm.geojson", [meridian(-75.0)])
    gj(tmp_path, "scratch_notes.geojson", [meridian(-74.0), meridian(-73.0)])
    monkeypatch.setattr(ba, "LIMITS", tmp_path)
    sets = ba.limit_sets()
    assert len(sets[ba.SLA_NAME].segments) == 2
    assert len(sets["scratch_notes"].segments) == 4
    assert len(sets[ba.ANY_NAME].segments) == 2
