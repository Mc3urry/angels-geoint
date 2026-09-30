"""A p-value of 1.0 must not be able to mean two opposite things.

The 200 nm EEZ line sits outside AOI_SEA, so all 1,097 candidates land in the
catch-all "> 25 nm" band, chi2 is exactly 0.0 and p is 1.0000. Printed in the
same table as the 12 and 24 nm lines it reads as "tested, no effect". It means
"never got within 25 nm of this line" -- the opposite claim, with the same
number attached.
"""

from __future__ import annotations

from scripts.boundary_analysis import untestable

NAMES = ["0-1 nm", "1-2 nm", "2-5 nm", "5-10 nm", "10-25 nm", "> 25 nm"]


def test_the_eez_shape_is_called_out():
    """Everything in the catch-all band: exactly the EEZ case."""
    why = untestable({b: 0 for b in NAMES[:-1]} | {"> 25 nm": 1097}, NAMES)
    assert why is not None
    assert "catch-all" in why


def test_a_real_test_is_not_flagged():
    obs = {"0-1 nm": 10, "1-2 nm": 25, "2-5 nm": 78, "5-10 nm": 87,
           "10-25 nm": 303, "> 25 nm": 594}
    assert untestable(obs, NAMES) is None


def test_one_near_candidate_is_enough_to_be_testable():
    """The flag is about having anything to compare, not about power."""
    obs = {b: 0 for b in NAMES[:-1]} | {"0-1 nm": 1, "> 25 nm": 1096}
    assert untestable(obs, NAMES) is None


def test_an_entirely_empty_set_is_untestable():
    assert untestable({b: 0 for b in NAMES}, NAMES) is not None


def test_the_reason_is_a_sentence_not_a_boolean():
    """A flag nobody can read is a flag nobody acts on."""
    why = untestable({b: 0 for b in NAMES[:-1]} | {"> 25 nm": 9}, NAMES)
    assert isinstance(why, str) and len(why) > 40
