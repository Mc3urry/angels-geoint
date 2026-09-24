"""Tests for the ADS-B reception grid.

This module decides whether a silence in the aviation archive is a finding or
an artefact, so the tests are about the ways it could quietly say "heard"
when it should not: a band borrowing its class from the band above, an
unknown altitude falling into the lowest bin, a holding stack making one cell
look like twenty polls' worth of coverage, and a class that survives being
written to disk and read back as something laxer.

Nothing here reaches the network.
"""

from __future__ import annotations

import json

import pytest

from angels.adapters.aviation.coverage import (
    BANDS,
    GROUND,
    MIN_AIRCRAFT,
    AirReceptionGrid,
    Band,
    band_of,
)
from angels.core.coverage import HEARD, INTERMITTENT, THIN, UNHEARD, neighbour_class


def state(icao, lon=-75.0, lat=40.0, alt=10000.0, ground=False, age=1.0):
    return (icao, lon, lat, alt, ground, age)


def grid_with(polls, cell_deg=1.0):
    g = AirReceptionGrid(cell_deg)
    for p in polls:
        g.add_poll(p)
    return g


# -- bands -------------------------------------------------------------------

def test_the_band_edges_are_stated_and_inclusive_below() -> None:
    assert band_of(0.0) == "0-1 km"
    assert band_of(999.0) == "0-1 km"
    assert band_of(1000.0) == "1-3 km"
    assert band_of(11999.0) == "9-12 km"
    assert band_of(12000.0) == "12+ km"
    assert band_of(40000.0) == "12+ km"


def test_on_the_ground_is_its_own_band_not_the_lowest_one() -> None:
    """An aircraft heard from the tarmac is the strongest evidence that
    coverage reaches the bottom of a cell. Folding it in with traffic a few
    hundred metres up would dilute exactly the signal that matters."""
    assert band_of(0.0, on_ground=True) == GROUND
    assert band_of(None, on_ground=True) == GROUND
    assert GROUND in BANDS and BANDS[0] == GROUND


def test_an_unknown_altitude_is_dropped_not_guessed() -> None:
    """About 10% of real rows have no barometric altitude. A coverage floor
    assembled out of guesses is the wrong artefact to build."""
    assert band_of(None) is None
    assert band_of(float("nan")) is None

    g = grid_with([[state("a", alt=None), state("b", alt=5000.0)]])
    assert g.n_no_altitude == 1
    assert sum(c.n_states for c in g.cells.values()) == 1


# -- what makes a band heard -------------------------------------------------

def test_one_airframe_is_thin_however_often_it_is_seen() -> None:
    """A class resting on a single aircraft's habits is not a class."""
    g = grid_with([[state("a")] for _ in range(50)])
    assert g.band_at(-75.0, 40.0, "9-12 km").n_aircraft == 1
    assert g.reception(-75.0, 40.0, 10000.0, neighbours=False) == THIN


def test_enough_aircraft_seen_routinely_is_heard() -> None:
    polls = [[state(f"a{i}") for i in range(MIN_AIRCRAFT)] for _ in range(20)]
    g = grid_with(polls)
    assert g.reception(-75.0, 40.0, 10000.0) == HEARD


def test_enough_aircraft_seen_rarely_is_intermittent() -> None:
    """Real traffic, but too little of it to promise that a silence here
    would have been recorded."""
    polls = [[state(f"a{i}") for i in range(MIN_AIRCRAFT)]] + [[] for _ in range(99)]
    g = grid_with(polls)
    assert g.band_at(-75.0, 40.0, "9-12 km").presence(g.n_polls) == pytest.approx(0.01)
    assert g.reception(-75.0, 40.0, 10000.0, neighbours=False) == INTERMITTENT


def test_a_holding_stack_does_not_look_like_coverage_over_time() -> None:
    """THE COUNTING RULE. Presence is per poll, not per aircraft: twenty
    aircraft stacked over one airport in a single snapshot is one moment of
    evidence, not twenty."""
    stack = [[state(f"a{i}") for i in range(20)]] + [[] for _ in range(99)]
    spread = [[state(f"b{i}")] for i in range(20)] + [[] for _ in range(80)]
    g1, g2 = grid_with(stack), grid_with(spread)
    b1 = g1.band_at(-75.0, 40.0, "9-12 km")
    b2 = g2.band_at(-75.0, 40.0, "9-12 km")
    assert b1.n_states == b2.n_states == 20
    assert b1.n_polls_present == 1
    assert b2.n_polls_present == 20


# -- the error this module exists to prevent ---------------------------------

def test_a_band_never_borrows_coverage_from_the_band_above_it() -> None:
    """THE WHOLE POINT, in miniature. Over the Great Basin the network hears
    two hundred airliners at eleven kilometres and has never heard anything
    at three. A grid that let the low band inherit the high one's class would
    launder a place it cannot see into a place it can."""
    polls = [[state(f"a{i}", lon=-116.5, alt=11000.0) for i in range(10)]
             for _ in range(20)]
    g = grid_with(polls)
    assert g.reception(-117.0, 40.0, 11000.0) == HEARD
    assert g.reception(-117.0, 40.0, 3000.0) == UNHEARD
    assert g.floor(-117.0, 40.0) == "9-12 km"


def test_neighbours_fill_an_empty_cell_only_within_its_own_band() -> None:
    polls = []
    for _ in range(20):
        polls.append([state(f"a{i}", lon=-76.5, alt=11000.0) for i in range(5)]
                     + [state(f"b{i}", lon=-74.5, alt=11000.0) for i in range(5)])
    g = grid_with(polls)
    # -75.5 is empty and sits between two heard cells, in that band only.
    assert g.band_at(-75.5, 40.0, "9-12 km").n_aircraft == 0
    assert g.reception(-75.5, 40.0, 11000.0) == HEARD
    assert g.reception(-75.5, 40.0, 2000.0) == UNHEARD


def test_an_unknown_altitude_gets_no_answer_rather_than_a_lenient_one() -> None:
    polls = [[state(f"a{i}") for i in range(5)] for _ in range(20)]
    g = grid_with(polls)
    assert g.reception(-75.0, 40.0, None) == UNHEARD


def test_nothing_ever_heard_is_unheard_and_has_no_floor() -> None:
    g = grid_with([[] for _ in range(10)])
    assert g.reception(-100.0, 45.0, 5000.0) == UNHEARD
    assert g.floor(-100.0, 45.0) is None


# -- the vertical profile ----------------------------------------------------

def test_the_profile_reads_bottom_up_and_names_every_band() -> None:
    polls = [[state(f"a{i}", lon=-116.5, alt=11000.0) for i in range(5)]
             for _ in range(20)]
    g = grid_with(polls)
    prof = g.profile(-117.0, 40.0)
    assert list(prof) == list(BANDS)
    assert prof["9-12 km"] == HEARD
    assert prof["0-1 km"] == UNHEARD


def test_the_new_jersey_and_great_basin_columns_come_out_opposite() -> None:
    """The measured contrast the design rests on, in miniature: heard to the
    tarmac on one coast, nothing below six kilometres inland."""
    polls = []
    for _ in range(20):
        nj = [state(f"n{i}{b}", lon=-74.5, lat=40.5, alt=a, ground=(a is None))
              for i in range(5)
              for b, a in enumerate((None, 500.0, 2000.0, 5000.0, 11000.0))]
        gb = [state(f"g{i}", lon=-116.5, lat=40.5, alt=11000.0) for i in range(5)]
        polls.append(nj + gb)
    g = grid_with(polls)
    assert g.floor(-74.5, 40.5) == GROUND
    assert g.floor(-116.5, 40.5) == "9-12 km"
    assert g.reception(-116.5, 40.5, 2000.0, neighbours=False) == UNHEARD
    assert g.reception(-74.5, 40.5, 2000.0, neighbours=False) == HEARD


# -- freshness is not coverage -----------------------------------------------

def test_contact_age_is_reported_but_never_classifies() -> None:
    """Measured over the real archive, contact age is ~1 s in every longitude
    band from the Atlantic to the Pacific. It is liveness, not coverage, and
    a grid that classified on it would call the Great Basin well covered."""
    stale = [[state(f"a{i}", lon=-116.5, alt=11000.0, age=300.0)
              for i in range(5)] for _ in range(20)]
    g = grid_with(stale)
    assert g.reception(-117.0, 40.0, 11000.0) == HEARD      # despite 300 s
    assert g.freshness()["median_s"] == 300.0
    assert g.freshness()["p_stale"] == 1.0


def test_freshness_of_an_empty_grid_is_absent_not_zero() -> None:
    assert AirReceptionGrid().freshness() == {"n": 0}


# -- storage -----------------------------------------------------------------

def test_the_json_round_trip_preserves_every_class() -> None:
    """A grid that came back laxer than it went out would silently promote
    candidates. The classes are the thing that must survive, not the bytes."""
    polls = []
    for p in range(40):
        rows = [state(f"a{i}", lon=-74.5, alt=11000.0) for i in range(5)]
        if p < 1:                                  # rare traffic -> intermittent
            rows += [state(f"b{i}", lon=-80.5, alt=5000.0) for i in range(4)]
        if p == 0:                                 # one airframe -> thin
            rows += [state("solo", lon=-90.5, alt=5000.0)]
        polls.append(rows)
    g = grid_with(polls)

    doc = json.loads(json.dumps(g.to_json()))       # serialisable
    back = AirReceptionGrid.from_json(doc)

    assert back.n_polls == g.n_polls
    assert back.cell_deg == g.cell_deg
    for lon, alt in ((-74.5, 11000.0), (-80.5, 5000.0), (-90.5, 5000.0),
                     (-60.5, 5000.0)):
        assert back.reception(lon, 40.0, alt, neighbours=False) == \
               g.reception(lon, 40.0, alt, neighbours=False)
    assert {g.reception(lon, 40.0, alt, neighbours=False)
            for lon, alt in ((-74.5, 11000.0), (-80.5, 5000.0),
                             (-90.5, 5000.0), (-60.5, 5000.0))} == \
           {HEARD, INTERMITTENT, THIN, UNHEARD}


def test_from_json_of_nothing_is_none_not_an_empty_grid() -> None:
    """An empty grid answers 'unheard' everywhere, which is a claim. A
    missing file is not."""
    assert AirReceptionGrid.from_json(None) is None
    assert AirReceptionGrid.from_json({}) is None


# -- the shared vocabulary ---------------------------------------------------

def test_both_domains_use_one_set_of_words() -> None:
    from angels.adapters.maritime import coverage as sea
    assert (sea.HEARD, sea.INTERMITTENT, sea.THIN, sea.UNHEARD) == \
           (HEARD, INTERMITTENT, THIN, UNHEARD)


def test_the_empty_cell_rule_is_conservative_in_the_middle() -> None:
    assert neighbour_class([HEARD, HEARD, THIN]) == HEARD
    assert neighbour_class([HEARD, THIN, THIN, UNHEARD]) == INTERMITTENT
    assert neighbour_class([THIN, UNHEARD]) == THIN
    assert neighbour_class([]) == UNHEARD


def test_area_is_counted_per_band(monkeypatch) -> None:
    polls = [[state(f"a{i}", alt=11000.0) for i in range(5)] for _ in range(20)]
    g = grid_with(polls)
    assert g.area_km2("9-12 km", HEARD) > 0
    assert g.area_km2("1-3 km", HEARD) == 0


def test_a_band_with_no_poll_count_refuses_to_say_routinely() -> None:
    """Guards from_json documents that never carried a poll count."""
    b = Band(n_states=9, aircraft={"a", "b", "c"})
    assert b.reception(0) == INTERMITTENT
