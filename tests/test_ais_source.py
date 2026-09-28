"""AIS is looked for in both places it can live, and the choice is declared.

`build_dossiers.py` checked one path -- the national day in
data/reference/ais -- and reported "NO AIS FILE" when it was absent. For
2024-09-25 it was absent, because that day arrived as a legacy CSV zip rather
than MarineCadastre's GeoParquet and no national file was ever written. The
AOI-clipped file had been on disk since 22 September with 434,668 rows in it.
Every run announced that 59 candidates could not be assessed for isolation,
and 59 dossiers carried an unknown they did not need to carry.

The check said "missing". It meant "missing at the one path I looked".

These tests fail if the second path is dropped, if the clipped file is used
without saying so, or if a genuinely absent date stops being reported.
"""

from __future__ import annotations

import pytest

from scripts.build_dossiers import ais_source


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """A data tree with both artefact locations, neither populated."""
    import scripts.build_dossiers as bd

    nat = tmp_path / "reference" / "ais"
    raw = tmp_path / "raw"
    nat.mkdir(parents=True)
    (raw / "maritime").mkdir(parents=True)
    monkeypatch.setattr(bd, "AIS_DIR", nat)
    monkeypatch.setattr(bd, "RAW", raw)
    return nat, raw


def _clipped(raw, date):
    d = raw / "maritime" / f"date={date}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "ais.parquet").write_bytes(b"")
    return d / "ais.parquet"


def test_national_is_preferred_and_carries_no_caveat(layout):
    nat, raw = layout
    (nat / "ais-2024-06-21.parquet").write_bytes(b"")
    _clipped(raw, "2024-06-21")
    path, kind, caveat = ais_source("2024-06-21")
    assert path == nat / "ais-2024-06-21.parquet"
    assert kind == "national"
    assert caveat is None


def test_falls_back_to_the_clipped_file(layout):
    """The actual defect: this date used to report NO AIS FILE."""
    _, raw = layout
    want = _clipped(raw, "2024-09-25")
    path, kind, caveat = ais_source("2024-09-25")
    assert path == want
    assert kind == "clipped"


def test_the_fallback_declares_what_it_costs(layout):
    """Using a smaller haystack silently is the same defect one step on."""
    _, raw = layout
    _clipped(raw, "2024-09-25")
    _, _, caveat = ais_source("2024-09-25")
    assert caveat, "the clipped file must not be used without saying so"
    assert "AOI" in caveat
    assert "isolated" in caveat, "the caveat must name the direction of the bias"


def test_genuinely_absent_is_still_absent(layout):
    path, kind, caveat = ais_source("2024-01-01")
    assert path is None
    assert kind == "none"
    assert caveat is None


def test_both_schemas_are_known_to_the_reader():
    """The two artefacts name the same facts differently; both are handled."""
    from scripts.build_dossiers import _SCHEMAS

    assert set(_SCHEMAS) == {"national", "clipped"}
    national, _ = _SCHEMAS["national"]
    clipped, _ = _SCHEMAS["clipped"]
    assert "geometry" in national, "the national day hides position in a WKB point"
    assert {"LON", "LAT"} <= set(clipped), "the clipped file carries plain columns"
