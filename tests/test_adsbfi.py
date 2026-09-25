"""The independent-channel collector: offline, like every test here.

No network. The feed is a fixture, and the one thing worth testing hardest is
the same thing that broke `check_tisb.py`: telling an empty sky from a
response this code does not understand.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def mod():
    """Load the collector with httpx replaced, so nothing can reach out.

    Stubbed at import rather than patched afterwards: a test that imports the
    real client and merely promises not to call it is one typo away from
    hitting a public API from CI.
    """
    saved = sys.modules.get("httpx")
    sys.modules["httpx"] = types.ModuleType("httpx")
    try:
        spec = importlib.util.spec_from_file_location(
            "_ingest_adsbfi", ROOT / "scripts" / "ingest_adsbfi.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    finally:
        if saved is not None:
            sys.modules["httpx"] = saved
        else:
            sys.modules.pop("httpx", None)


AIRCRAFT = [
    {"hex": "a1b2c3", "type": "adsb_icao", "flight": "AAL123 ", "lat": 38.9,
     "lon": -77.0, "alt_baro": 35000, "gs": 450.0, "track": 270.1,
     "seen_pos": 0.3, "rssi": -12.5, "messages": 4211, "nic": 8,
     "t": "B738", "r": "N123AA", "squawk": "1200"},
    {"hex": "d4e5f6", "type": "mlat", "lat": 39.0, "lon": -76.9,
     "alt_baro": "ground", "seen_pos": 2.1, "mlat": ["gs", "track"]},
    {"hex": "998877", "type": "adsb_icao", "lat": 38.8, "lon": -77.2,
     "tisb": ["baro_rate"], "some_new_field_2027": 42},
]


def test_position_source_not_airframe_type(mod) -> None:
    """`type` is the position source; `t` is the airframe. Never confuse them."""
    assert mod.is_independent("mlat") is True
    assert mod.is_independent("tisb_icao") is True
    assert mod.is_independent("adsr_icao") is True
    assert mod.is_independent("adsb_icao") is False
    assert mod.is_independent("adsb_icao_nt") is False
    assert mod.is_independent("B738") is False        # an airframe code
    assert mod.is_independent(None) is False


def test_every_field_survives_the_round_trip(mod) -> None:
    t = mod.rows_to_table(datetime(2026, 9, 25, tzinfo=timezone.utc),
                          1790000000.0, AIRCRAFT)
    d = t.to_pydict()
    assert d["hex"] == ["a1b2c3", "d4e5f6", "998877"]
    assert d["type"] == ["adsb_icao", "mlat", "adsb_icao"]
    assert d["flight"][0] == "AAL123"                 # stripped
    assert d["t"][0] == "B738" and d["r"][0] == "N123AA"
    assert d["alt_baro"] == ["35000", "ground", None]  # int OR a word
    assert d["mlat_fields"][1] == json.dumps(["gs", "track"])
    assert d["tisb_fields"][2] == json.dumps(["baro_rate"])


def test_an_undocumented_field_is_kept_not_dropped(mod) -> None:
    """An hour you did not collect is gone; so is a field you discarded."""
    t = mod.rows_to_table(datetime(2026, 9, 25, tzinfo=timezone.utc), None,
                          AIRCRAFT)
    extra = t.to_pydict()["extra"]
    assert extra[0] is None and extra[1] is None
    assert json.loads(extra[2]) == {"some_new_field_2027": 42}


def _ingester(mod, payload, tmp_path):
    ing = mod.Ingester(38.9, -77.0, 70, tmp_path, 30.0, 30)
    stub = types.SimpleNamespace(
        get=lambda *a, **k: types.SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: payload))
    mod.httpx = stub
    return ing


def test_counts_both_channels(mod, tmp_path) -> None:
    ing = _ingester(mod, {"aircraft": AIRCRAFT}, tmp_path)
    assert ing.poll_once() == 3
    assert ing.seen["independent"] == 1
    assert ing.seen["cooperative"] == 2


def test_accepts_the_adsbexchange_key(mod, tmp_path) -> None:
    ing = _ingester(mod, {"ac": AIRCRAFT}, tmp_path)
    assert ing.poll_once() == 3


def test_a_missing_list_raises_rather_than_counting_zero(mod, tmp_path) -> None:
    """THE MISTAKE THIS COLLECTOR MUST NOT REPEAT.

    `check_tisb.py` read `d.get("ac")` against a feed that returns
    `aircraft`, and so reported an empty sky for its entire life. A collector
    doing the same would write hour after hour of zero-aircraft parquet and
    look exactly like an AOI with no traffic in it.
    """
    ing = _ingester(mod, {"now": 1.0, "total": 7}, tmp_path)
    with pytest.raises(KeyError, match="no aircraft list"):
        ing.poll_once()


def test_an_empty_list_is_accepted_as_empty(mod, tmp_path) -> None:
    ing = _ingester(mod, {"aircraft": []}, tmp_path)
    assert ing.poll_once() == 0
    assert ing.seen["cooperative"] == 0


def test_partition_is_hourly(mod, tmp_path) -> None:
    t = mod.rows_to_table(datetime(2026, 9, 25, 14, tzinfo=timezone.utc),
                          None, AIRCRAFT)
    p = mod.write_partition(t, datetime(2026, 9, 25, 14, 30,
                                        tzinfo=timezone.utc), tmp_path)
    assert p.parent.name == "hour=2026092514"
    assert p.parent.parent.name == mod.DATASET


def test_the_interval_floor_is_enforced(mod, monkeypatch, capsys) -> None:
    """adsb.fi gives this away and asks 1/s. Do not crowd it."""
    monkeypatch.setattr(sys, "argv",
                        ["ingest_adsbfi.py", "--interval", "0.5"])
    assert mod.main() == 2
    assert "floor" in capsys.readouterr().out
