"""A verdict without a note cannot be adjudicated, so it is not accepted.

WHY THIS EXISTS

Round 1 of the blind second read produced **14 disagreements out of 40, and
zero of the 40 carried a note**. Not one could be settled: two readers
disagreeing with no stated reason is a number, not an argument. The round
that measured kappa 0.467 cannot say what the other 0.533 consisted of, and
that is unrecoverable -- the chips can be re-read, but not by the reader who
saw them that day.

Enforced on the server, not in the browser. A rule in the client is a
suggestion: a replay script, a second reader on a stale page, or a curl
command writes whatever it likes.
"""

from __future__ import annotations

import pytest

from angels.api.routes.labels import (NOTE_MIN, NOTE_MIN_AMBIGUOUS,
                                      note_required)


def test_an_empty_note_is_refused_for_every_verdict() -> None:
    for v in ("vessel", "fixed", "clutter", "ambiguous"):
        why = note_required(v, "")
        assert why, f"{v} accepted with no note"
        assert "needs a note" in why


def test_whitespace_is_not_a_note() -> None:
    assert note_required("vessel", "   \n\t  ")


def test_none_is_not_a_note() -> None:
    assert note_required("vessel", None)


def test_a_real_note_is_accepted() -> None:
    assert note_required("vessel", "compact bright point, dark water") is None


def test_ambiguous_carries_the_longer_floor() -> None:
    """It is the verdict most in need of a reason and the easiest to click
    through."""
    assert NOTE_MIN_AMBIGUOUS > NOTE_MIN
    short = "x" * NOTE_MIN
    assert note_required("vessel", short) is None
    assert note_required("ambiguous", short) is not None


def test_the_refusal_says_what_to_do_differently() -> None:
    """'note required' sends a reader back to the chip with nothing to
    change. The message has to name the thing being asked for."""
    why = note_required("vessel", "ok")
    assert "characters" in why
    assert "Describe the thing" in why
    assert str(NOTE_MIN) in why


def test_the_empty_case_explains_why_the_rule_exists() -> None:
    why = note_required("clutter", None)
    assert "14 disagreements" in why
    assert "adjudicated" in why


def test_the_floor_counts_stripped_length() -> None:
    """Padding a short note with spaces must not clear the bar."""
    assert note_required("vessel", " " * 40 + "no") is not None
