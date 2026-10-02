"""The aviation discrepancy analysis, and the three mistakes already made in it.

Nothing here touches the network or the collected data.
"""

from __future__ import annotations

import json

import pytest

from scripts.air_discrepancy import (COOPERATIVE, INDEPENDENT, TISB, VEIL_NM,
                                     altitude_ft, cell_of, continuity,
                                     difference_in_differences, load_airports,
                                     signed_nm)

DCA = (38.85144027, -77.03772138)


# -- alt_baro ---------------------------------------------------------------

def test_altitude_reads_the_string_column() -> None:
    """alt_baro is a STRING. Reading it as a number dropped every real
    altitude once and reported 100% of independent targets at 0 ft -- a
    parsing bug that looked like a finding."""
    assert altitude_ft("21125") == 21125
    assert altitude_ft("600") == 600


def test_ground_is_not_an_altitude_of_zero() -> None:
    """'ground' is a sentinel. Mapping it to 0 puts taxiing aircraft in the
    lowest altitude band and inflates it."""
    assert altitude_ft("ground") is None
    assert altitude_ft(None) is None
    assert altitude_ft("") is None


# -- the running variable ---------------------------------------------------

def test_inside_the_veil_is_negative_and_outside_positive() -> None:
    a = [DCA]
    assert signed_nm(DCA[1], DCA[0], a) == pytest.approx(-VEIL_NM, abs=0.01)
    assert signed_nm(DCA[1], DCA[0] + 1.0, a) > 0


def test_the_boundary_is_the_NEAREST_airport_not_the_first() -> None:
    """The veil is a union of circles. Using one airport put rings 40 to 55 nm
    from DCA outside a boundary they were inside, and produced an apparent
    3 to 4x step that was an artefact."""
    far, near = (40.5, -77.0), (38.9, -77.0)
    one = signed_nm(-77.0, 38.95, [far])
    two = signed_nm(-77.0, 38.95, [far, near])
    assert two < one


# -- the mask ---------------------------------------------------------------

def test_continuity_joins_consecutive_fixes() -> None:
    cells = continuity({"abc": [(0.0, -77.0, 38.9), (60.0, -76.9, 38.9)]})
    assert cell_of(-77.0, 38.9) in cells
    assert cell_of(-76.95, 38.9) in cells
    assert cell_of(-76.9, 38.9) in cells


def test_continuity_refuses_a_long_gap() -> None:
    """Two fixes an hour apart say nothing about the ground between them."""
    assert continuity({"abc": [(0.0, -77.0, 38.9), (3600.0, -76.0, 38.9)]}) == set()


def test_continuity_needs_two_fixes() -> None:
    assert continuity({"abc": [(0.0, -77.0, 38.9)]}) == set()


def test_continuity_survives_a_timestamp_that_is_not_a_number() -> None:
    """Sighting 26: float() raised on every row and a defaultdict reported
    122 aircraft while holding nothing. The caller now guards, so this
    function only ever sees numbers -- but an empty input must give an empty
    answer rather than an exception."""
    assert continuity({}) == set()


# -- the statistic ----------------------------------------------------------

def units(**kw):
    return {k: list(v) for k, v in kw.items()}


def test_the_did_is_treatment_step_minus_control_step() -> None:
    u = {((0, 0), "<10k"): [900, 100],     # inside  0.100
         ((9, 9), "<10k"): [800, 200],     # outside 0.200  step +0.100
         ((0, 0), ">=10k"): [950, 50],     # inside  0.050
         ((9, 9), ">=10k"): [900, 100]}    # outside 0.100  step +0.050
    sides = {(0, 0): "inside", (9, 9): "outside"}
    r = difference_in_differences(u, sides, floor=1)
    assert r["treatment_step"] == pytest.approx(0.100)
    assert r["control_step"] == pytest.approx(0.050)
    assert r["did"] == pytest.approx(0.050)


def test_an_equal_step_in_both_groups_is_zero_not_an_effect() -> None:
    """A step that is the same in the control is confound. The design exists
    to subtract it, and the test exists because a large treatment step reads
    as a result until the control is put beside it."""
    u = {((0, 0), "<10k"): [800, 200], ((9, 9), "<10k"): [600, 400],
         ((0, 0), ">=10k"): [800, 200], ((9, 9), ">=10k"): [600, 400]}
    sides = {(0, 0): "inside", (9, 9): "outside"}
    r = difference_in_differences(u, sides, floor=1)
    assert r["treatment_step"] > 0.1
    assert r["did"] == pytest.approx(0.0)


def test_the_floor_excludes_thin_units() -> None:
    u = {((0, 0), "<10k"): [10, 90], ((9, 9), "<10k"): [500, 10],
         ((0, 0), ">=10k"): [500, 10], ((9, 9), ">=10k"): [500, 10]}
    sides = {(0, 0): "inside", (9, 9): "outside"}
    r = difference_in_differences(u, sides, floor=200)
    assert r["n_units"]["<10k"]["inside"] == 0


# -- what may be called independent -----------------------------------------

def test_tisb_is_never_in_the_independent_channel() -> None:
    """AMENDMENT 1. TIS-B is uplinked for the benefit of ADS-B-equipped
    aircraft in a service volume, so its presence rises with the cooperative
    density it would be divided by. It is reported, never pooled."""
    assert not set(INDEPENDENT) & set(TISB)
    assert not set(INDEPENDENT) & set(COOPERATIVE)
    assert "mlat" in INDEPENDENT and "mode_s" in INDEPENDENT
    for t in ("tisb_icao", "tisb_trackfile", "tisb_other"):
        assert t not in INDEPENDENT


def test_adsr_is_cooperative_not_independent() -> None:
    """ADS-R is a rebroadcast of a cooperative transmission, not an
    observation of a silent aircraft."""
    assert "adsr_icao" in COOPERATIVE
    assert "adsr_icao" not in INDEPENDENT


# -- the airport file -------------------------------------------------------

def test_a_radius_that_disagrees_with_the_script_is_refused(tmp_path) -> None:
    """The file declares the radius the obligation uses. If it ever says
    something other than 30 nm, one of the two is wrong and the run stops
    rather than silently using the script's number."""
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"radius_nm": 25, "airports": []}), encoding="utf-8")
    with pytest.raises(SystemExit):
        load_airports(p)


def test_a_matching_radius_loads(tmp_path) -> None:
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"radius_nm": 30, "eff_date": "2026/10/01",
                             "airports": [{"lat": 38.9, "lon": -77.0}]}),
                 encoding="utf-8")
    pts, eff = load_airports(p)
    assert pts == [(38.9, -77.0)]
    assert eff == "2026/10/01"


def test_the_day_dimension_is_not_collapsed() -> None:
    """THE REGRESSION. The call site re-keyed the per-day units on
    (cell, band) in a dict comprehension, which overwrote every day but the
    last. 565 units became 78, the control group fell to four, and the
    difference in differences flipped from -0.00007 to +0.00443 -- from
    refuted to not refuted, toward the hypothesis.

    Two days of one cell and band must be two units, not one.
    """
    u = {((0, 0), "<10k", "d1"): [500, 50],
         ((0, 0), "<10k", "d2"): [500, 50],
         ((9, 9), "<10k", "d1"): [500, 50],
         ((0, 0), ">=10k", "d1"): [500, 50],
         ((9, 9), ">=10k", "d1"): [500, 50]}
    sides = {(0, 0): "inside", (9, 9): "outside"}
    r = difference_in_differences(u, sides, floor=1)
    assert r["n_units"]["<10k"]["inside"] == 2, "the two days were merged"
    assert r["n_units"]["<10k"]["outside"] == 1


def test_a_two_element_key_still_works() -> None:
    """The sensitivity passes (cell, band). Both shapes must be accepted
    without the function guessing which it was handed."""
    u = {((0, 0), "<10k"): [500, 50], ((9, 9), "<10k"): [500, 50],
         ((0, 0), ">=10k"): [500, 50], ((9, 9), ">=10k"): [500, 50]}
    sides = {(0, 0): "inside", (9, 9): "outside"}
    r = difference_in_differences(u, sides, floor=1)
    assert r["n_units"]["<10k"]["inside"] == 1
    assert r["did"] == pytest.approx(0.0)


# -- the nulls --------------------------------------------------------------
#
# Added after the fact, which is the wrong order: the nulls were written,
# run, and their output used to reach a verdict before any test existed for
# them. A null that silently always rejects, or never rejects, produces a
# confident verdict either way.

from scripts.air_discrepancy import nulls


def two_sided_units():
    """Half the cells inside, half outside, with a real difference built in:
    the treatment side-gap is large, the control's is zero."""
    u, sides = {}, {}
    for i in range(40):
        c_in, c_out = (i, 0), (i, 9)
        sides[c_in], sides[c_out] = "inside", "outside"
        u[(c_in, "<10k", "d")] = [900, 10]      # 0.011
        u[(c_out, "<10k", "d")] = [900, 100]    # 0.100
        u[(c_in, ">=10k", "d")] = [900, 50]
        u[(c_out, ">=10k", "d")] = [900, 50]
    return u, sides


def test_a_real_difference_is_not_reproduced_by_the_nulls() -> None:
    """If the nulls cannot reject anything, the verdict is meaningless."""
    u, sides = two_sided_units()
    obs = difference_in_differences(u, sides, floor=1)["did"]
    assert obs > 0.05
    r = nulls(u, sides, 1, obs, trials=200, shift_trials=100)
    assert r["scattered_p"] < 0.05, "scattered reproduced a large effect"


def test_an_effect_of_nothing_is_reproduced_by_almost_everything() -> None:
    """The complement, and the case this analysis actually hit: an observed
    difference near zero is exceeded by nearly every random assignment."""
    u, sides = two_sided_units()
    for k in u:
        u[k] = [900, 50]                        # identical everywhere
    obs = difference_in_differences(u, sides, floor=1)["did"]
    assert abs(obs) < 1e-9
    r = nulls(u, sides, 1, obs, trials=200, shift_trials=100)
    assert r["scattered_p"] > 0.5
    assert r["shift_p"] > 0.5


def test_p_is_k_of_n_plus_one_and_never_zero() -> None:
    """A p of exactly 0 is not a thing 200 trials can report."""
    u, sides = two_sided_units()
    obs = difference_in_differences(u, sides, floor=1)["did"]
    r = nulls(u, sides, 1, obs, trials=200, shift_trials=100)
    assert r["scattered_p"] == pytest.approx((r["scattered_k"] + 1) / 201)
    assert r["shift_p"] == pytest.approx((r["shift_k"] + 1) / 101)
    assert r["scattered_p"] > 0 and r["shift_p"] > 0


def test_the_seed_makes_it_reproducible() -> None:
    u, sides = two_sided_units()
    obs = difference_in_differences(u, sides, floor=1)["did"]
    a = nulls(u, sides, 1, obs, trials=100, shift_trials=50, seed=7)
    b = nulls(u, sides, 1, obs, trials=100, shift_trials=50, seed=7)
    c = nulls(u, sides, 1, obs, trials=100, shift_trials=50, seed=8)
    assert a == b
    assert (a["scattered_k"], a["shift_k"]) != (c["scattered_k"], c["shift_k"]) \
        or a["scattered_k"] in (0, 100)


def test_the_shift_null_preserves_the_units_and_moves_only_the_boundary() -> None:
    """The point of the shift is that clustering is carried intact onto a
    boundary in the wrong place. If it perturbed the counts instead, it would
    be a different null."""
    u, sides = two_sided_units()
    before = dict(u)
    obs = difference_in_differences(u, sides, floor=1)["did"]
    nulls(u, sides, 1, obs, trials=10, shift_trials=10)
    assert u == before, "the null mutated the units it was given"
