"""`tune_gate` must read the measured keep-fractions, never remember them.

A hand-copied constant lived in `tune_gate.py` until 2026-09-26. After the
pass-3 label re-read it still said >10nm 0.62 while `correct_clutter.py`
measured 0.59, so the disqualifier that decides whether a gate may be applied
upstream of the boundary test was being checked against a number nothing
produced any more. Nothing failed. Nothing said anything.

These tests fail if the fallback ever comes back: missing artefact, or an
artefact written from a different `labels.jsonl`, must both stop the run.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from scripts.tune_gate import load_justified


def _labels(tmp_path, text="{}\n"):
    p = tmp_path / "labels.jsonl"
    p.write_text(text, encoding="utf-8")
    return p


def _artefact(tmp_path, labels, per_band, *, sha=None):
    p = tmp_path / "keep-fractions.json"
    p.write_text(json.dumps({
        "per_band": per_band,
        "labels": labels.name,
        "labels_sha256": sha or hashlib.sha256(labels.read_bytes()).hexdigest(),
    }), encoding="utf-8")
    return p


def test_reads_the_measurement(tmp_path):
    labels = _labels(tmp_path)
    art = _artefact(tmp_path, labels, {"<=2nm": 0.83, "2-10nm": 0.72, ">10nm": 0.59})
    assert load_justified(art, labels) == {"<=2nm": 0.83, "2-10nm": 0.72, ">10nm": 0.59}


def test_missing_artefact_stops_the_run(tmp_path):
    labels = _labels(tmp_path)
    with pytest.raises(SystemExit) as e:
        load_justified(tmp_path / "nope.json", labels)
    assert "correct_clutter" in str(e.value)


def test_artefact_from_other_labels_stops_the_run(tmp_path):
    """The exact defect: the artefact is present, and describes a past run."""
    labels = _labels(tmp_path, '{"key": "a"}\n')
    art = _artefact(tmp_path, labels, {">10nm": 0.62}, sha="0" * 64)
    with pytest.raises(SystemExit) as e:
        load_justified(art, labels)
    msg = str(e.value)
    assert "different" in msg and "0" * 64 in msg


def test_empty_per_band_stops_the_run(tmp_path):
    labels = _labels(tmp_path)
    art = _artefact(tmp_path, labels, {})
    with pytest.raises(SystemExit):
        load_justified(art, labels)


def test_no_remembered_constant_survives_in_the_module():
    """Mutation guard: a dict of band keep-fractions written into the source."""
    import inspect

    import scripts.tune_gate as tg

    src = inspect.getsource(tg)
    head = src.split("def load_justified", 1)[0]
    assert "JUSTIFIED =" not in head, (
        "a hardcoded keep-fraction table is back in tune_gate.py; the "
        "disqualifier must read what correct_clutter.py measured")
