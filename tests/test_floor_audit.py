"""The floor audit reports a bound, and must never report an estimate.

The published gate discards 85.9% of what the detector found, and no chip
below it has ever been read. The temptation is to extrapolate the labelled
bin rates downward and quote a vessel count. That would be inventing a
measurement in the one region where none exists.
"""

from __future__ import annotations

import inspect

import pytest

from scripts import floor_audit

pytestmark = pytest.mark.realdata


def test_the_gate_is_read_from_the_artefact_not_hardcoded():
    """Same lesson as the keep-fractions: a copied constant drifts."""
    pub = floor_audit.published_gate()
    if pub is None:
        pytest.skip("no boundary-bands.json")
    assert pub == (15.0, 6)


def test_raw_detections_include_what_the_floor_removed():
    rows = list(floor_audit.raw_detections())
    if not rows:
        pytest.skip("no sar-*.geojson on this machine")
    assert len(rows) > 2500, "these look like the gated set, not the raw one"
    assert any(s < 15 or px < 6 for s, px, _ in rows), (
        "no sub-floor detections found; the audit would have nothing to "
        "measure and would silently report a floor that discards nothing")


def test_it_does_not_estimate_a_vessel_fraction():
    """A mutation guard on the one thing this script must not start doing."""
    src = inspect.getsource(floor_audit)
    body = src.split('"""', 2)[-1]
    for banned in ("p_vessel", "P(vessel)", "expected_vessels", "estimated_vessels"):
        assert banned not in body, (
            f"floor_audit has started computing {banned!r}. There are no "
            "labels below the floor; any such number is extrapolated from "
            "bins the labels never covered.")


def test_the_output_says_what_it_cannot_say():
    src = inspect.getsource(floor_audit)
    assert "DOES NOT SAY" in src
    assert "bound" in src
