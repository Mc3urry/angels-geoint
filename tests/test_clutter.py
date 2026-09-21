"""Tests for the check that says "these are not ships".

A CFAR detector always returns something. Sea clutter in SAR is K-distributed
-- heavy-tailed, not Gaussian -- so a threshold in sigmas admits a long tail
of bright speckle, and that tail arrives formatted exactly like a vessel list:
coordinates, SNR, cluster size, all plausible.

Measured on a real slice of open Atlantic: 567 detections, brightest 1.8x the
threshold, 67% at the minimum cluster size, not one of them a ship.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "detect_ships",
    Path(__file__).resolve().parents[1] / "scripts" / "detect_ships.py")
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)

from angels.core.models import Observation, Position   # noqa: E402

T = datetime(2024, 9, 25, 22, 58, tzinfo=timezone.utc)


def det(snr, pixels):
    return Observation(
        position=Position(lat=37.0, lon=-75.0, t=T, uncertainty_m=30.0),
        sensor="sentinel1-vv-cfar",
        attributes={"snr": snr, "pixels": pixels})


def clutter_tail(k=6.0, n=567):
    """The measured shape: exponential decay from the threshold, stopping just
    above it, most clusters at the floor."""
    out = []
    for snr, count in ((6.5, 360), (7.4, 162), (8.3, 35), (9.2, 7), (10.5, 3)):
        for i in range(count):
            out.append(det(snr, 2 if i % 3 else 4))
    return out[:n]


def vessels():
    """A real population: a few very bright returns with large clusters, over
    a thin background of small ones."""
    out = [det(s, p) for s, p in
           ((573.7, 291), (274.3, 76), (254.6, 108), (106.2, 29), (76.3, 18))]
    out += [det(7.0, 2) for _ in range(40)]
    return out


# -- the two verdicts ------------------------------------------------------

def test_a_clutter_tail_is_named_as_one() -> None:
    """THE POINT OF THE FILE. This list is 567 plausible-looking rows and
    contains no vessels."""
    r = mod.clutter_report(clutter_tail(), 6.0)
    assert r["verdict"] == "clutter"
    assert r["headroom"] < 2.0
    assert r["at_floor"] > 0.4


def test_a_real_population_is_not() -> None:
    r = mod.clutter_report(vessels(), 6.0)
    assert r["verdict"] == "vessels present"
    assert r["headroom"] > 20


def test_headroom_is_measured_against_the_threshold_actually_used() -> None:
    """k is a knob. Raising it shrinks every SNR relative to nothing -- the
    ratio has to use the k that produced the list, or lowering k would look
    like finding brighter ships."""
    obs = clutter_tail()
    assert mod.clutter_report(obs, 6.0)["headroom"] > \
        mod.clutter_report(obs, 12.0)["headroom"]


def test_one_bright_ship_in_a_sea_of_clutter_is_not_called_clutter() -> None:
    """A slice with a single large vessel and a lot of tail must not be
    dismissed -- the finding might BE that one vessel."""
    obs = clutter_tail() + [det(180.0, 140)]
    assert mod.clutter_report(obs, 6.0)["verdict"] == "vessels present"


def test_the_floor_is_read_from_the_data_not_assumed() -> None:
    """min_pixels is a parameter. If a run used 5, then 5 is the floor and a
    cluster of 5 is the suspicious one, not a cluster of 2."""
    obs = [det(7.0, 5) for _ in range(90)] + [det(8.0, 9) for _ in range(10)]
    r = mod.clutter_report(obs, 6.0)
    assert r["at_floor"] == pytest.approx(0.9)


def test_an_empty_list_is_not_a_verdict() -> None:
    """No detections is not evidence of clutter, and not evidence of an empty
    sea either. It is an absence, and absences are reported as such."""
    r = mod.clutter_report([], 6.0)
    assert r["verdict"] == "empty"
    assert r["n"] == 0


def test_marginal_sits_between_the_two() -> None:
    obs = clutter_tail() + [det(25.0, 12)]
    assert mod.clutter_report(obs, 6.0)["verdict"] == "marginal"
