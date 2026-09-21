"""Tests for the gate chooser. The rule is fixed before the data is seen;
these check it does what it says and cannot peek at the test pass."""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("tune_detector",
                                              ROOT / "scripts" / "tune_detector.py")
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)

from angels.core.detectors.calibration import Rate  # noqa: E402


def table(entries):
    """{(snr, px): (found, scored, unmatched)} -> Outcomes over 1,000 km2."""
    return {g: mod.Outcome(Rate(f, n), u, 1000.0) for g, (f, n, u) in entries.items()}


BASE = (mod.SNR_GATES[0], mod.PIXEL_GATES[0])


def test_it_picks_the_strictest_gate_that_keeps_the_rate() -> None:
    t = table({BASE: (10, 20, 500),          # 50%
               (8, 2): (10, 20, 200),         # 50%, cleaner
               (12, 2): (9, 20, 80),          # 45% = 90% of base, cleaner still
               (20, 2): (5, 20, 10)})         # 25%, too costly
    assert mod.choose(t, 0.9) == (12, 2)


def test_a_stricter_rate_requirement_picks_a_looser_gate() -> None:
    t = table({BASE: (10, 20, 500), (8, 2): (10, 20, 200),
               (12, 2): (9, 20, 80)})
    assert mod.choose(t, 1.0) == (8, 2)


def test_no_rate_no_choice() -> None:
    assert mod.choose(table({BASE: (0, 0, 100)}), 0.9) is None


def test_the_chooser_never_sees_the_test_pass() -> None:
    """choose() takes one table. main() must hand it the TRAIN table."""
    src = inspect.getsource(mod.main)
    assert "choose(train," in src and "choose(test" not in src


def test_the_gate_keeps_detections_at_the_line() -> None:
    class O:
        def __init__(self, snr, px):
            self.attributes = {"snr": snr, "pixels": px}
    kept = mod.gated([O(6, 2), O(8, 2), O(8, 3), O(20, 1)], 8, 2)
    assert [(o.attributes["snr"], o.attributes["pixels"]) for o in kept] == [(8, 2), (8, 3)]
