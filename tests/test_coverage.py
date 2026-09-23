"""Tests for the AIS reception grid.

This is the difference between "nobody reported" and "nobody was listening",
and the project's central rule is that those must never be confused. So the
tests are about the ways a coverage measure can quietly claim coverage it
does not have.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from angels.adapters.maritime import coverage
from angels.adapters.maritime.coverage import (HEARD, INTERMITTENT, THIN,
                                               UNHEARD, ReceptionGrid)

T0 = datetime(2024, 9, 25, 22, 58, tzinfo=timezone.utc)


def track(mmsi, lon, lat, *, every_s=60, n=20, t0=T0):
    return [(mmsi, t0 + timedelta(seconds=i * every_s), lon, lat)
            for i in range(n)]


def test_prompt_reports_from_several_vessels_are_heard() -> None:
    reports = []
    for i in range(4):
        reports += track(f"3660000{i}", -75.5, 37.0)
    g = ReceptionGrid.from_reports(reports)
    assert g.reception(-75.5, 37.0, neighbours=False) == HEARD


def test_long_silences_are_intermittent_not_heard() -> None:
    """The offshore edge does not look empty -- it looks bursty."""
    reports = []
    for i in range(4):
        reports += track(f"3660000{i}", -72.0, 37.0, every_s=900)
    g = ReceptionGrid.from_reports(reports)
    assert g.reception(-72.0, 37.0, neighbours=False) == INTERMITTENT


def test_one_vessel_is_not_enough_to_judge_a_cell() -> None:
    g = ReceptionGrid.from_reports(track("366000001", -73.0, 37.0))
    assert g.reception(-73.0, 37.0, neighbours=False) == THIN


def test_water_with_no_ais_at_all_is_unheard() -> None:
    g = ReceptionGrid.from_reports(track("366000001", -75.5, 37.0))
    assert g.reception(-71.5, 36.2, neighbours=False) == UNHEARD


def test_unheard_is_not_silently_treated_as_heard() -> None:
    """THE ERROR THIS FILE EXISTS TO PREVENT. An empty grid must not answer
    'heard' anywhere, or every candidate offshore becomes a dark vessel."""
    g = ReceptionGrid()
    assert g.reception(-74.0, 37.0) == UNHEARD
    assert coverage.ReceptionGrid.from_json({}) is None
    assert coverage.ReceptionGrid.from_json(None) is None


def test_an_empty_cell_inside_heard_water_reads_as_heard() -> None:
    """A 5 km square can go uncrossed for twelve overflights. Calling that
    unheard would throw away candidates in the middle of a shipping lane."""
    reports = []
    for k, (dlon, dlat) in enumerate([(-0.05, 0), (0.05, 0), (0, -0.05),
                                      (0, 0.05)]):
        for i in range(4):
            reports += track(f"36600{k}{i}", -75.5 + dlon, 37.0 + dlat)
    g = ReceptionGrid.from_reports(reports)
    assert g.cell_at(-75.5, 37.0).n_vessels == 0
    assert g.reception(-75.5, 37.0) == HEARD
    assert g.reception(-75.5, 37.0, neighbours=False) == UNHEARD


def test_a_cells_own_evidence_beats_its_neighbours() -> None:
    reports = []
    for i in range(4):
        reports += track(f"3660000{i}", -75.45, 37.0)          # heard
    for i in range(4):
        reports += track(f"3661000{i}", -75.5, 37.0, every_s=900)
    g = ReceptionGrid.from_reports(reports)
    assert g.reception(-75.5, 37.0) == INTERMITTENT


def test_gaps_are_per_vessel_not_per_cell() -> None:
    """Two vessels reporting a minute apart from each other, each once, is
    not a cell reporting every minute."""
    g = ReceptionGrid.from_reports([
        ("a", T0, -75.5, 37.0),
        ("b", T0 + timedelta(seconds=60), -75.5, 37.0),
        ("c", T0 + timedelta(seconds=120), -75.5, 37.0)])
    assert g.cell_at(-75.5, 37.0).gaps == []
    assert g.reception(-75.5, 37.0, neighbours=False) == THIN


def test_a_first_report_contributes_no_gap() -> None:
    g = ReceptionGrid.from_reports(track("366000001", -75.5, 37.0, n=3))
    assert len(g.cell_at(-75.5, 37.0).gaps) == 2


def test_a_crossing_vessel_credits_the_cell_it_arrived_in() -> None:
    g = ReceptionGrid.from_reports([
        ("a", T0, -75.50, 37.0),
        ("a", T0 + timedelta(seconds=60), -75.40, 37.0)])
    assert g.cell_at(-75.50, 37.0).gaps == []
    assert g.cell_at(-75.40, 37.0).gaps == [60.0]


def test_cell_indexing_survives_a_boundary() -> None:
    """The floating-point floor bug from searched.py, in a second grid."""
    g = ReceptionGrid(cell_deg=0.05)
    assert g.key(-75.05, 37.05) == (-1501, 741)
    assert g.key(-75.05 + 1e-12, 37.05) == (-1501, 741)


def test_the_json_round_trips_the_decisions() -> None:
    reports = []
    for i in range(4):
        reports += track(f"3660000{i}", -75.5, 37.0)
    for i in range(4):
        reports += track(f"3661000{i}", -72.0, 37.0, every_s=900)
    g = ReceptionGrid.from_reports(reports)
    back = ReceptionGrid.from_json(json.loads(json.dumps(g.to_json())))
    assert back.reception(-75.5, 37.0, neighbours=False) == HEARD
    assert back.reception(-72.0, 37.0, neighbours=False) == INTERMITTENT
    assert back.reception(-71.0, 36.1, neighbours=False) == UNHEARD


def test_area_only_counts_the_class_asked_for() -> None:
    reports = []
    for i in range(4):
        reports += track(f"3660000{i}", -75.5, 37.0)
    g = ReceptionGrid.from_reports(reports)
    assert g.area_km2(HEARD) == pytest.approx(24.6, abs=2)
    assert g.area_km2(INTERMITTENT) == 0.0


def test_the_thresholds_are_stated_and_match_the_measured_cadence() -> None:
    """Measured: median gap 72 s (June) and 161 s (September) in the bulk
    files. The long-gap line has to sit above both, or normal water reads
    as an outage."""
    assert coverage.LONG_GAP_S >= 161
    assert coverage.MIN_VESSELS >= 3


def test_an_empty_cell_reports_no_rate_rather_than_zero() -> None:
    c = coverage.Cell()
    assert math.isnan(c.median_gap_s) and math.isnan(c.p_long)
    assert c.to_json()["median_gap_s"] is None


def test_a_gap_between_two_passes_is_not_a_silence() -> None:
    """Pooling twelve dates puts a twelve-day interval between one vessel's
    last report on one pass and its first on the next. Counted as a gap, it
    would make every cell a vessel visits twice look like the edge of
    coverage."""
    later = T0 + timedelta(days=12)
    g = ReceptionGrid.from_reports(
        track("a", -75.5, 37.0, n=10) + track("a", -75.5, 37.0, n=10, t0=later)
        + track("b", -75.5, 37.0, n=10) + track("c", -75.5, 37.0, n=10))
    cell = g.cell_at(-75.5, 37.0)
    assert max(cell.gaps) == 60.0
    assert g.reception(-75.5, 37.0, neighbours=False) == HEARD


def test_feed_sorted_streams_without_holding_the_year() -> None:
    g = ReceptionGrid()
    rows = sorted(track("a", -75.5, 37.0, n=5) + track("b", -75.5, 37.0, n=5),
                  key=lambda r: (r[0], r[1]))
    assert g.feed_sorted(rows) == 10
    assert g.cell_at(-75.5, 37.0).gaps == [60.0] * 8
