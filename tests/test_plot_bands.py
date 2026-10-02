"""The figure is generated from the artefact, and refuses to lie.

WHY THIS EXISTS

docs/boundary-step-12nm.svg was drawn by hand on 24 September from numbers
that moved on 30 September and again on 2 October. A figure with no script
behind it cannot tell you it is stale, and neither can a reader: it is a
picture of numbers, indistinguishable from a picture of the right numbers.
"""

from __future__ import annotations

import pytest

from scripts.plot_bands import ratios, svg

BANDS = ["0-1 nm", "1-2 nm", "> 25 nm"]


def entry(cand=(10, 25, 570), exp=(42.18, 42.84, 507.83),
          ctl=(24, 19, 151), ctl_exp=(18.6, 18.9, 228.8), **kw):
    e = {"candidates_by_band": dict(zip(BANDS, cand)),
         "expected_by_band": dict(zip(BANDS, exp)),
         "ais_seen_by_band": dict(zip(BANDS, ctl)),
         "ais_seen_expected": dict(zip(BANDS, ctl_exp)),
         "p_shift": 0.8254, "p_control_shift": 0.0100, "testable": True}
    e.update(kw)
    return e


META = {"trials": 2000, "shift_trials": 400}


def test_the_ratio_is_observed_over_expected() -> None:
    bands, cand, _ = ratios(entry())
    assert bands == BANDS
    assert cand[0][0] == pytest.approx(10 / 42.18)
    assert cand[0][1] == 10


def test_a_zero_denominator_is_not_plotted_as_zero() -> None:
    """A band nothing searched has no ratio. Drawing it at zero would put a
    marker on the floor that reads as 'nothing found there'."""
    _, cand, _ = ratios(entry(exp=(0.0, 42.84, 507.83)))
    assert cand[0] is None
    assert cand[1] is not None


def test_both_series_are_drawn_and_both_are_named() -> None:
    """Identity never rests on colour alone."""
    out = svg("12 nm territorial sea", entry(), META)
    assert "candidates" in out
    assert "AIS-seen control" in out
    assert out.count("<circle") >= 2 * len(BANDS)


def test_the_control_p_value_is_on_the_figure() -> None:
    """The control is the point of the chart, not decoration: it is what
    separates a measured absence from an absence of power."""
    out = svg("3 nm state seaward limit", entry(), META)
    assert "0.8254" in out and "0.0100" in out


def test_the_axis_says_what_it_measures() -> None:
    out = svg("12 nm territorial sea", entry(), META)
    assert "observed / expected" in out
    assert "parity" in out


def test_dark_mode_is_declared_for_every_painted_role() -> None:
    """A ring stamped with the light surface glows wrong on a dark one. The
    first version of this script did exactly that."""
    out = svg("12 nm territorial sea", entry(), META)
    dark = out.split("prefers-color-scheme:dark")[1]
    for role in (".ring{", ".s{", ".ink{", ".cd{", ".ct{"):
        assert role in dark, f"{role} has no dark step"


def test_direct_labels_are_pushed_apart_when_they_would_collide() -> None:
    """Both series ending at the same value must not stack two labels on one
    line. Checked by geometry, because the palette validator checks colour
    and a human checks layout."""
    out = svg("12 nm territorial sea",
              entry(cand=(10, 25, 100), exp=(42.18, 42.84, 100.0),
                    ctl=(24, 19, 100), ctl_exp=(18.6, 18.9, 100.0)), META)
    from scripts.plot_bands import R, W
    legend_x = f'cx="{W - R + 20}"'
    ys = [float(t.split('cy="')[1].split('"')[0])
          for t in out.split("<circle ") if legend_x in t]
    assert len(ys) == 2
    assert abs(ys[0] - ys[1]) >= 18
