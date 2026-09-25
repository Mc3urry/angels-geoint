"""The independent-channel probe must not report a confident zero.

`scripts/check_tisb.py` read the aircraft list from `d.get("ac")`. adsb.fi
returns it under `aircraft`, so the probe printed "0 aircraft within 70 nm of
DC" every time it was run -- a sky with no independent traffic in it and a
probe looking in the wrong place produce identical output, and only one of
them is a finding.

These tests are offline. The module is loaded with `httpx` stubbed out, so
nothing here touches the network.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(payload: dict):
    """Import check_tisb with httpx replaced by a canned response."""
    stub = types.ModuleType("httpx")

    class _R:
        def raise_for_status(self):
            return self

        def json(self):
            return payload

    stub.get = lambda *a, **k: _R()
    saved = sys.modules.get("httpx")
    sys.modules["httpx"] = stub
    try:
        spec = importlib.util.spec_from_file_location(
            "_check_tisb_under_test", ROOT / "scripts" / "check_tisb.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if saved is not None:
            sys.modules["httpx"] = saved
        else:
            del sys.modules["httpx"]


def _run(mod, argv=()):
    saved = sys.argv
    sys.argv = ["check_tisb.py", *argv]
    try:
        return mod.main()
    finally:
        sys.argv = saved


def test_reads_the_adsbfi_key(capsys) -> None:
    mod = _load({"aircraft": [{"type": "adsb_icao"}, {"type": "mlat"}]})
    assert _run(mod) == 0
    out = capsys.readouterr().out
    assert "2 aircraft" in out
    assert "INDEPENDENT" in out


def test_reads_the_adsbexchange_key(capsys) -> None:
    """Both spellings exist in the wild; accept either."""
    mod = _load({"ac": [{"type": "adsb_icao"}]})
    assert _run(mod) == 0
    assert "1 aircraft" in capsys.readouterr().out


def test_a_missing_list_is_not_an_empty_sky(capsys) -> None:
    """THE BUG. No recognised key must be loud, and must not exit 0."""
    mod = _load({"now": 123, "total": 7})
    assert _run(mod) == 2
    out = capsys.readouterr().out
    assert "NO AIRCRAFT LIST" in out
    assert "Refusing to report zero" in out
    assert "0 aircraft" not in out


def test_an_empty_list_says_so(capsys) -> None:
    mod = _load({"aircraft": []})
    assert _run(mod) == 0
    assert "genuinely empty" in capsys.readouterr().out


@pytest.mark.parametrize("kind,independent", [
    ("adsb_icao", False), ("adsb_icao_nt", False), ("adsr_icao", True),
    ("tisb_icao", True), ("tisb_other", True), ("mlat", True),
])
def test_position_source_vocabulary(kind: str, independent: bool) -> None:
    mod = _load({"aircraft": []})
    assert mod.is_independent(kind) is independent


def test_a_partially_sourced_field_is_not_an_independent_target(capsys) -> None:
    """`tisb: ["baro_rate"]` on an ADS-B aircraft is one field, not a target.

    Counting it as independent would overstate the channel, which is the
    error this whole probe exists to avoid making in the other direction.
    """
    mod = _load({"aircraft": [
        {"type": "adsb_icao", "tisb": ["baro_rate"]},
        {"type": "adsb_icao"},
    ]})
    assert _run(mod) == 0
    out = capsys.readouterr().out
    assert "0 of 2 independently positioned" in out
    assert "1 more carry at least one" in out
