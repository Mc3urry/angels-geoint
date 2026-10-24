"""G2b's border selection, against made-up states whose answers are known.

No network. Three square states share two lines:

        lon  -77        -76        -75
    lat 40   +----------+----------+
             |   "10"   |   "24"   |
    lat 39   +----------+----------+
             |        "51"         |
    lat 38   +---------------------+

so a box across lon -76 between lat 39 and 40 is in "10" and "24", a box well
inside "10" is in one state, and a box over the ocean is in none.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.fetch_road_registries import RegistryRefused
from scripts.select_border_feeds import (BUFFER_M, M_PER_DEG_LAT, MAX_BOX_KM,
                                         NAMED_BY_HAND, R_BY_HAND, R_DUPLICATE,
                                         R_NO_STATE, R_ONE_STATE,
                                         R_SKIPPED_STATE, SAME_AGENCY, bounds,
                                         build, buffered, check_arithmetic,
                                         check_hand_table, has_box,
                                         resolve_same_agency,
                                         point_in_rings, rect_overlaps_rings,
                                         segments_meet, select, states_touched,
                                         summary_sentence)


def square(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]


GEOM = {
    "10": square(-77, 39, -76, 40),
    "24": square(-76, 39, -75, 40),
    "51": square(-77, 38, -75, 39),
}
NAMES = {"10": "West", "24": "East", "51": "South"}


def states_dict():
    return {f: {"name": NAMES[f], "rings": [g], "bounds": bounds([g])}
            for f, g in GEOM.items()}


def row(fid, box=None, **kw):
    r = {"id": fid, "provider": f"Agency {fid}", "subdivision": "X",
         "url": f"https://example.org/{fid}.pb"}
    if box is not None:
        x0, y0, x1, y1 = box
        r["bbox"] = {"min_lon": x0, "min_lat": y0, "max_lon": x1, "max_lat": y1,
                     "valid": True}
    else:
        r["bbox"] = None
    r.update(kw)
    return r


KM = 1000.0
# one km of longitude at 39.5 N, in degrees
KM_LON = KM / (M_PER_DEG_LAT * 0.7716)


# -- the three outcomes -----------------------------------------------------

def test_a_box_across_a_shared_line_is_in_both_states() -> None:
    rect = (-76.2, 39.3, -75.8, 39.7)
    assert states_touched(rect, states_dict()) == ["10", "24"]


def test_a_box_inside_one_state_is_in_one() -> None:
    assert states_touched((-76.8, 39.3, -76.5, 39.6), states_dict()) == ["10"]


def test_a_box_over_the_ocean_is_in_none() -> None:
    assert states_touched((-70.0, 39.3, -69.5, 39.6), states_dict()) == []


def test_three_outcomes_land_in_the_right_lists() -> None:
    rows = [row("across", (-76.2, 39.3, -75.8, 39.7)),
            row("inside", (-76.8, 39.3, -76.5, 39.6)),
            row("ocean", (-70.0, 39.3, -69.5, 39.6))]
    sel, exc = select(rows, states_dict(), buffer_m=0)
    assert [s["id"] for s in sel] == ["across"]
    assert sel[0]["states"] == ["10", "24"]
    assert sel[0]["state_names"] == ["West", "East"]
    reasons = {e["id"]: e["reason"] for e in exc}
    assert reasons == {"inside": R_ONE_STATE, "ocean": R_NO_STATE}


# -- a state skipped on purpose is not a gap --------------------------------

ALASKA_EXTENT = {"02": {"extent": [-179.2, 51.2, 179.8, 71.4],
                        "reason": "Alaska: its only land boundary is with Canada"}}


def test_a_feed_in_a_state_skipped_by_rule_is_explained_not_flagged() -> None:
    """FOUND BY THE SMOKE RUN OF 2026-10-23. Anchorage People Mover overlaps
    no fetched polygon because Alaska is skipped on purpose. Filing it under
    "a bad box or a gap -- look at them" would be wrong, and would be wrong on
    every run, until nobody read that line -- which is the line meant to catch
    a real hole. Decided by the skipped state's extent, not by the registry's
    subdivision label."""
    anchorage = row("mdb-1656", (-149.98, 61.11, -149.57, 61.33))
    sel, exc = select([anchorage], states_dict(), skipped=ALASKA_EXTENT)
    assert sel == []
    assert exc[0]["reason"] == R_SKIPPED_STATE
    assert exc[0]["skipped_state"] == "02"
    assert "Canada" in exc[0]["detail"]


def test_a_box_in_no_state_and_no_skipped_extent_is_still_flagged() -> None:
    """The fix must not swallow the case it was separated from. A box at 0,0
    is in the Gulf of Guinea, not in any state skipped or fetched."""
    bad = row("broken", (0.0, 0.0, 0.1, 0.1))
    _, exc = select([bad], states_dict(), skipped=ALASKA_EXTENT)
    assert exc[0]["reason"] == R_NO_STATE


def test_an_unknown_extent_leaves_the_feed_flagged_rather_than_explained() -> None:
    """If the service did not return an extent, nothing can explain the feed,
    so it over-reports. Hiding it would be the unsafe direction."""
    anchorage = row("mdb-1656", (-149.98, 61.11, -149.57, 61.33))
    _, exc = select([anchorage], states_dict(),
                    skipped={"02": {"extent": None, "reason": "Alaska"}})
    assert exc[0]["reason"] == R_NO_STATE


def test_a_skipped_state_is_not_counted_as_a_stray_in_the_summary() -> None:
    s = summary_sentence({"_rules": {"buffer_m": 2000.0}, "rows_read": 9,
                          "n_selected": 3, "excluded_by_reason": {R_SKIPPED_STATE: 1}})
    assert "NO state" not in s


def test_fips_stay_zero_padded_strings() -> None:
    """"01" is Alabama; 1 is nothing. A join keyed on an int matches no row."""
    touched = states_touched((-76.2, 39.3, -75.8, 39.7), states_dict())
    assert all(isinstance(f, str) and len(f) == 2 for f in touched)


# -- the buffer, which is the reason this is not a simple overlap ----------

def test_a_box_stopping_short_of_the_line_is_pulled_in_by_the_buffer() -> None:
    """The case the buffer exists for. A feed whose stops end 1 km short of a
    state line still runs vehicles into the border zone; with no buffer it
    reads as single-state and would never be polled."""
    edge = -76.0 - 1 * KM_LON                         # 1 km west of the line
    rect = (-76.6, 39.3, edge, 39.7)
    assert states_touched(rect, states_dict()) == ["10"]
    assert states_touched(buffered(rect, 2 * KM), states_dict()) == ["10", "24"]


def test_a_box_well_short_of_the_line_is_not() -> None:
    """The buffer is an upper bound, not a licence. 3 km short stays out."""
    edge = -76.0 - 3 * KM_LON
    rect = (-76.6, 39.3, edge, 39.7)
    assert states_touched(buffered(rect, 2 * KM), states_dict()) == ["10"]


def test_the_buffer_is_at_least_its_width_at_the_poleward_edge() -> None:
    """Longitude degrees shrink towards the pole, so the widening is computed
    at the latitude nearer it. Computed at the middle, the poleward edge would
    get less than the stated buffer -- an upper bound that is not one."""
    import math
    rect = (-100.0, 45.0, -99.0, 46.0)
    g = buffered(rect, 2 * KM)
    dlon = g[2] - rect[2]
    metres_at_46 = dlon * M_PER_DEG_LAT * math.cos(math.radians(46.0))
    assert metres_at_46 >= 2 * KM - 1e-6


# -- the overlap test's corners ---------------------------------------------

def test_a_state_wholly_inside_the_box_overlaps_it() -> None:
    tiny = square(-76.5, 39.5, -76.4, 39.6)
    assert rect_overlaps_rings((-77.0, 39.0, -76.0, 40.0), [tiny])


def test_a_box_wholly_inside_a_state_overlaps_it_with_no_edge_crossing() -> None:
    """No edge reaches into the box, so the answer comes from one corner."""
    big = square(-80, 35, -70, 45)
    assert rect_overlaps_rings((-76.0, 39.0, -75.9, 39.1), [big])


def test_a_box_inside_a_hole_does_not_overlap() -> None:
    """Even-odd over every ring, so a hole is a hole."""
    outer = square(-80, 35, -70, 45)
    hole = square(-77, 38, -75, 40)
    assert not rect_overlaps_rings((-76.2, 38.8, -75.8, 39.2), [outer, hole])
    assert point_in_rings(-79.0, 36.0, [outer, hole])
    assert not point_in_rings(-76.0, 39.0, [outer, hole])


@pytest.mark.parametrize("p1,p2,q1,q2,meet", [
    ((0, 0), (2, 2), (0, 2), (2, 0), True),        # crossing
    ((0, 0), (1, 1), (1, 1), (2, 0), True),        # touching at an endpoint
    ((0, 0), (2, 0), (1, 0), (3, 0), True),        # collinear, overlapping
    ((0, 0), (1, 0), (2, 0), (3, 0), False),       # collinear, apart
    ((0, 0), (2, 0), (0, 1), (2, 1), False),       # parallel
])
def test_segment_intersection_cases(p1, p2, q1, q2, meet) -> None:
    assert segments_meet(p1, p2, q1, q2) is meet


# -- the feeds with no box --------------------------------------------------

def test_a_no_box_feed_follows_its_hand_decision() -> None:
    rows = [row("mdb-1695"), row("tld-7044-vp")]
    sel, exc = select(rows, states_dict())
    assert [s["id"] for s in sel] == ["mdb-1695"], "TANK is included"
    assert sel[0]["how"] == "named by hand"
    assert exc[0]["reason"] == R_BY_HAND


def test_an_unnamed_no_box_feed_is_a_refusal_not_a_silent_drop() -> None:
    """The only outcome that could never be noticed afterwards is a feed that
    fell out with no trace, so it is the one that stops the run."""
    with pytest.raises(RegistryRefused, match="no decision in NAMED_BY_HAND"):
        check_hand_table([row("mdb-unknown")])


def test_a_named_feed_that_gained_a_box_is_handed_back_to_geometry() -> None:
    warn = check_hand_table([row("mdb-1695", (-84.6, 39.0, -84.4, 39.2))])
    assert any("now HAS a box" in w for w in warn)


def test_the_hand_table_matches_the_real_registry_exactly() -> None:
    """Read from the committed artefact, not asserted from memory. The seven
    no-box feeds on 2026-10-23 are the seven keys, no more and no fewer."""
    reg = Path(__file__).resolve().parents[1] / "data" / "reference" / \
        "roads" / "transit-registry.json"
    if not reg.exists():                                 # pragma: no cover
        pytest.skip("registry not fetched in this checkout")
    d = json.loads(reg.read_text(encoding="utf-8"))
    rows = d["selected"] + d.get("credentialed", [])
    unusable = {r["id"] for r in rows if not has_box(r)}
    assert unusable == set(NAMED_BY_HAND)
    assert len(unusable) == 9, "seven with no box and two with impossible ones"
    assert check_hand_table(rows) == []


# -- a box too big to be evidence -------------------------------------------

def test_the_size_ceiling_sits_inside_the_measured_gap() -> None:
    """Chosen from the data, not by feel: the largest legitimate box is
    Denver's RTD at 333 km, and the smallest impossible one is 1,029 km. If a
    future registry puts a box in between, it is flagged for a person rather
    than trusted -- the loud direction."""
    import math
    reg = Path(__file__).resolve().parents[1] / "data" / "reference" / \
        "roads" / "transit-registry.json"
    if not reg.exists():                                 # pragma: no cover
        pytest.skip("registry not fetched in this checkout")
    d = json.loads(reg.read_text(encoding="utf-8"))
    sizes = []
    for r in d["selected"] + d.get("credentialed", []):
        b = r.get("bbox")
        if isinstance(b, dict) and b.get("valid"):
            mid = math.radians((b["min_lat"] + b["max_lat"]) / 2)
            sizes.append(max((b["max_lon"] - b["min_lon"]) * 111.32 * math.cos(mid),
                             (b["max_lat"] - b["min_lat"]) * 111.32))
    below = max(s for s in sizes if s <= MAX_BOX_KM)
    above = min(s for s in sizes if s > MAX_BOX_KM)
    assert below < MAX_BOX_KM < above
    assert above / below > 2.5, "the ceiling must sit in a real gap, not a slope"


def test_an_impossible_box_with_no_decision_is_a_refusal() -> None:
    """A 1,000 km box is not evidence of four states; it is a stray stop. It
    is treated as no box, so a person decides or the run stops."""
    huge = row("mdb-giant", (-100.0, 30.0, -88.0, 40.0))
    assert not has_box(huge)
    with pytest.raises(RegistryRefused, match="no decision in NAMED_BY_HAND"):
        check_hand_table([huge])


def test_the_two_impossible_boxes_are_excluded_with_their_size_shown() -> None:
    rit = row("tld-6884-vp", (-84.0, 33.0, -77.0, 42.3))
    _, exc = select([rit], states_dict())
    assert exc[0]["reason"] == R_BY_HAND
    assert "stray stop" in exc[0]["detail"]
    assert max(exc[0]["box_km"]) > MAX_BOX_KM, "the size is shown, not hidden"


# -- the same agency, selected twice ----------------------------------------

def sel(fid, provider):
    return {"id": fid, "provider": provider, "how": "geometry",
            "states": ["10", "24"]}


def test_a_duplicate_pair_keeps_one_and_says_why() -> None:
    """VRE from two hosts. Keeping both would count every train twice with no
    sign; keeping one can only lose coverage, which shows."""
    kept, exc = resolve_same_agency(
        [sel("mdb-1734", "Virginia Railway Express"),
         sel("tld-1127-vp", "Virginia Railway Express")], [])
    assert [k["id"] for k in kept] == ["tld-1127-vp"]
    assert exc[0]["id"] == "mdb-1734" and exc[0]["reason"] == R_DUPLICATE
    assert "PROVISIONAL" in exc[0]["detail"]


def test_bus_and_rail_of_one_agency_are_both_kept() -> None:
    kept, exc = resolve_same_agency(
        [sel("mdb-1850", "Washington Metropolitan Area Transit Authority"),
         sel("mdb-1853", "Washington Metropolitan Area Transit Authority")], [])
    assert {k["id"] for k in kept} == {"mdb-1850", "mdb-1853"} and exc == []


def test_an_undecided_same_agency_pair_is_a_refusal() -> None:
    with pytest.raises(RegistryRefused, match="no decision in SAME_AGENCY"):
        resolve_same_agency([sel("a-1", "Some Transit"), sel("a-2", "Some Transit")], [])


def test_a_pair_with_only_one_selected_needs_no_decision() -> None:
    """Port Authority of Allegheny County has bus and rail feeds, and
    Pittsburgh is in one state, so neither is selected and nothing is asked."""
    kept, _ = resolve_same_agency([sel("solo", "Some Transit")], [])
    assert [k["id"] for k in kept] == ["solo"]


def test_every_same_agency_decision_is_well_formed() -> None:
    for key, (decision, why) in SAME_AGENCY.items():
        assert key == tuple(sorted(key)), "keys are sorted id tuples"
        assert decision == "both" or decision.split(":", 1)[1] in key
        assert why


# -- the artefact, built end to end from files -----------------------------

def write_fixture(tmp: Path, *, partial=False, tol=0.0, rows=None) -> tuple[Path, Path]:
    bdir = tmp / "states-generalised"
    bdir.mkdir()
    manifest = {"_service": "test", "generalise_m_upper_bound": tol,
                "generalised": True, "partial": partial, "states": {}}
    for fips, g in GEOM.items():
        (bdir / f"{fips}.geojson").write_text(json.dumps({"type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {"STATE": fips},
                          "geometry": {"type": "Polygon", "coordinates": [g]}}]}),
            encoding="utf-8")
        manifest["states"][fips] = {"name": NAMES[fips], "file": f"{fips}.geojson"}
    (bdir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    reg = tmp / "transit-registry.json"
    rows = rows if rows is not None else [
        row("across", (-76.2, 39.3, -75.8, 39.7)),
        row("inside", (-76.8, 39.3, -76.5, 39.6)),
        row("mdb-1695"), row("tld-7044-vp"), row("mdb-1566"), row("tld-1260-vp"),
        row("tld-4364-vp"), row("tld-3391-vp"), row("tld-6890-vp"),
        row("keyed", (-76.2, 39.3, -75.8, 39.7), env_var="WMATA_API_KEY")]
    reg.write_text(json.dumps({"selected": rows[:-1], "credentialed": rows[-1:]}),
                   encoding="utf-8")
    return reg, bdir


def test_every_row_lands_in_exactly_one_list(tmp_path) -> None:
    reg, bdir = write_fixture(tmp_path)
    art = build(reg, bdir, BUFFER_M)
    assert art["n_selected"] + art["n_excluded"] == art["rows_read"] == 10
    ids = [s["id"] for s in art["selected"]] + [e["id"] for e in art["excluded"]]
    assert len(ids) == len(set(ids)) == 10


def test_the_arithmetic_check_catches_a_lost_row() -> None:
    with pytest.raises(RegistryRefused, match="lost between the lists"):
        check_arithmetic({"rows_read": 5, "n_selected": 2, "n_excluded": 2,
                          "excluded_by_reason": {"x": 2}})


def test_a_credentialed_feed_carries_its_variable_name_and_nothing_else(tmp_path) -> None:
    reg, bdir = write_fixture(tmp_path)
    art = build(reg, bdir, BUFFER_M)
    keyed = next(s for s in art["selected"] if s["id"] == "keyed")
    assert keyed["env_var"] == "WMATA_API_KEY"


def test_the_inputs_are_named_by_digest(tmp_path) -> None:
    """Rule 4. A selection that does not say which registry and which
    boundaries it was made from cannot be checked in February."""
    reg, bdir = write_fixture(tmp_path)
    art = build(reg, bdir, BUFFER_M)
    assert len(art["_inputs"]["transit_registry_sha256"]) == 64
    assert len(art["_inputs"]["boundary_manifest_sha256"]) == 64


def test_the_generalisation_is_added_to_the_buffer_not_just_recorded(tmp_path) -> None:
    """A box 2,050 m short of the true line, against polygons simplified by up
    to 111 m. At 2,000 m it would be missed; at 2,111 m it is caught. Recording
    the tolerance without applying it would leave the artefact claiming a
    reach it did not have."""
    edge = -76.0 - 2.05 * KM_LON
    rows = [row("near", (-76.6, 39.3, edge, 39.7))] + [
        row(k) for k in NAMED_BY_HAND] + [row("keyed", (-76.8, 39.3, -76.5, 39.6),
                                              env_var="X")]
    reg, bdir = write_fixture(tmp_path, tol=111.3, rows=rows)
    art = build(reg, bdir, BUFFER_M)
    assert art["_rules"]["buffer_applied_m"] == pytest.approx(BUFFER_M + 111.3)
    assert "near" in [s["id"] for s in art["selected"]]


def test_a_partial_boundary_set_is_refused(tmp_path) -> None:
    """Every state missing from the set would make its neighbours' feeds read
    as single-state -- a wrong answer with exactly the right shape."""
    reg, bdir = write_fixture(tmp_path, partial=True)
    with pytest.raises(RegistryRefused, match="PARTIAL"):
        build(reg, bdir, BUFFER_M)


def test_four_situations_give_four_sentences() -> None:
    """Rule 13."""
    base = {"_rules": {"buffer_m": 2000.0}}
    said = {
        summary_sentence({**base, "rows_read": 0, "n_selected": 0, "excluded_by_reason": {}}),
        summary_sentence({**base, "rows_read": 9, "n_selected": 0, "excluded_by_reason": {}}),
        summary_sentence({**base, "rows_read": 9, "n_selected": 3, "excluded_by_reason": {}}),
        summary_sentence({**base, "rows_read": 9, "n_selected": 3,
                          "excluded_by_reason": {R_NO_STATE: 1}}),
    }
    assert len(said) == 4


def test_a_feed_in_no_state_is_called_out_not_filed_as_inland() -> None:
    s = summary_sentence({"_rules": {"buffer_m": 2000.0}, "rows_read": 9,
                          "n_selected": 3, "excluded_by_reason": {R_NO_STATE: 2}})
    assert "overlap NO state" in s and "look at them" in s
