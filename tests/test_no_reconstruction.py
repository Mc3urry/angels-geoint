"""Values the pipeline recorded are read, not derived a second time.

THE DEFECT THIS GENERALISES FROM

`inspect_candidates.py` threw away the pixel the detector had recorded and
reconstructed it from lon/lat by Newton iteration. On 17 of 1,150 candidates
the reconstruction was 300 m to 10 km wrong, silently, and four hand-read
chips were of open sea kilometres from their candidate. One of them produced
a published false finding.

Re-deriving a stored value is not a cross-check unless the two are compared
and the disagreement is reported. These tests are that comparison, kept.
"""

from __future__ import annotations

import json

import pytest

from angels.config import EVENTS

pytestmark = pytest.mark.realdata


def _sample():
    p = EVENTS / "label-sample.json"
    if not p.exists():
        pytest.skip("no label-sample.json on this machine")
    return json.loads(p.read_text(encoding="utf-8"))


def test_the_draw_carries_the_detectors_pixel():
    """Without this the chipper has no choice but to invert a coordinate."""
    for rec in _sample()["sample"]:
        assert rec.get("pixel"), f"{rec['id']} has no recorded pixel"
        assert len(rec["pixel"]) == 2


def test_distance_to_limit_recomputes_to_what_was_stored():
    """Three scripts recompute this from the limit lines; one stored it.

    They agree today. If a limit file is replaced, or the distance routine
    changes, this is where that shows up -- instead of in a band assignment
    nobody re-derived.
    """
    from scripts.boundary_analysis import limit_sets
    from scripts.sample_candidates import distance_nm

    lines = limit_sets().get("any limit")
    if lines is None:
        pytest.skip("limit lines not available")

    worst, n = 0.0, 0
    for rec in _sample()["sample"]:
        stored = rec.get("nm_to_limit")
        if stored is None:
            continue
        now = distance_nm(rec["lon"], rec["lat"], lines)
        if now is None:
            continue
        n += 1
        worst = max(worst, abs(now - stored))
    assert n > 100, "too few records compared for this to mean anything"
    # The stored value is rounded to 2 dp at draw time; nothing beyond that.
    assert worst <= 0.02, f"distance-to-limit drifted by {worst:.3f} nm"


def test_pixel_of_is_not_used_where_a_pixel_was_recorded():
    """A mutation guard: the chipper must prefer the stored pixel."""
    import inspect

    import scripts.inspect_candidates as ic

    src = inspect.getsource(ic)
    assert 'p.get("pixel")' in src, (
        "inspect_candidates no longer reads the recorded pixel; it is "
        "reconstructing a coordinate it was handed")
