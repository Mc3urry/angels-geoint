"""The sentence that says which population survived the shift null.

THE DEFECT THIS EXISTS FOR, 2026-10-02.

The report had three branches for four combinations. `if p_shift > 0.05`
came first and swallowed two cases -- neither population surviving, and the
CONTROL surviving while the candidates did not -- and printed "nothing here
survives moving the pattern around" for both.

That sentence is true about the candidates and silent about the control. It
was wrong by omission on the 3 nm state seaward line, where the candidates
came back at shift p 0.83 and the AIS-reporting control at 0.0100: the
control concentrates on the line and the unexplained returns do not. That
omitted fact is the most informative one available, because a control that
survives on the same bands and the same searched-water denominator is what
separates a measured absence from an absence of statistical power.

The branch logic was not testable while it lived inside a print, which is
why it went unexamined through four published runs.
"""

from __future__ import annotations

from scripts.boundary_analysis import shift_verdict

SURVIVES, NULL = 0.002, 0.90


def test_both_survive_points_at_the_difference() -> None:
    v = shift_verdict(SURVIVES, SURVIVES)
    assert "Both survive" in v
    assert "DIFFERENCE" in v


def test_candidates_only_is_the_shape_of_a_reporting_effect() -> None:
    v = shift_verdict(SURVIVES, NULL)
    assert "Candidates survive" in v
    assert "real reporting effect" in v


def test_control_only_is_reported_and_not_called_nothing() -> None:
    """THE REGRESSION. This is the combination that printed the null
    sentence for four runs."""
    v = shift_verdict(NULL, SURVIVES)
    assert "CONTROL survives" in v
    assert "Nothing here survives" not in v
    assert "opposite" in v
    assert "measured absence" in v


def test_neither_surviving_is_the_only_case_called_nothing() -> None:
    v = shift_verdict(NULL, NULL)
    assert "Nothing here survives" in v


def test_the_four_combinations_give_four_different_sentences() -> None:
    """Three sentences for four cases is how the omission happened."""
    out = {shift_verdict(a, b)
           for a in (SURVIVES, NULL) for b in (SURVIVES, NULL)}
    assert len(out) == 4


def test_the_3nm_numbers_that_exposed_it() -> None:
    """Candidates 0.8254, control 0.00998 -- the primary run, 2026-10-02."""
    v = shift_verdict(0.8254364089775561, 0.00997506234413965)
    assert "CONTROL survives" in v


def test_the_boundary_is_inclusive_at_alpha() -> None:
    """A p exactly at alpha counts as surviving, the same convention the
    report used before extraction."""
    assert "Both survive" in shift_verdict(0.05, 0.05)
    assert "Nothing here survives" in shift_verdict(0.0501, 0.0501)
