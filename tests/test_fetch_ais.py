"""Tests for the AIS download step.

No network. What matters here is not the transfer but the DATE: a wrong one
does not error. It fetches a real file full of real vessels that were nowhere
near the radar, matching finds almost no partners, and the run reports a sea
full of dark vessels.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "fetch_ais",
    Path(__file__).resolve().parents[1] / "scripts" / "fetch_ais.py")
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)

SCENE = ("S1A_IW_GRDH_1SDV_20240925T225812_20240925T225841"
         "_055828_06D27E_789A_COG.SAFE.zip")


# -- dates come from the filenames -----------------------------------------

def test_the_date_is_read_from_the_scene_name(tmp_path) -> None:
    (tmp_path / SCENE).write_bytes(b"")
    assert mod.dates_from_scenes(tmp_path) == [date(2024, 9, 25)]


def test_it_reads_the_acquisition_field_not_the_orbit(tmp_path) -> None:
    """Field 4 is the acquisition start -- the same field detect_ships.py
    parses for the instant AIS is interpolated to. Field 5 is the end and
    field 6 the absolute orbit; picking the wrong one either shifts the day or
    produces a nonsense date that still parses as one."""
    (tmp_path / SCENE).write_bytes(b"")
    (only,) = mod.dates_from_scenes(tmp_path)
    assert (only.year, only.month, only.day) == (2024, 9, 25)


def test_several_slices_of_one_pass_are_one_date(tmp_path) -> None:
    """A pass is four slices seconds apart. Downloading the same 320 MB day
    four times would be slow and would not be wrong, which is how it survives
    review."""
    for sec in ("225812", "225841", "225906", "225931"):
        (tmp_path / SCENE.replace("225812", sec)).write_bytes(b"")
    assert len(mod.dates_from_scenes(tmp_path)) == 1


def test_two_passes_on_different_days_are_two_dates(tmp_path) -> None:
    (tmp_path / SCENE).write_bytes(b"")
    (tmp_path / SCENE.replace("20240925", "20241007")).write_bytes(b"")
    assert mod.dates_from_scenes(tmp_path) == [date(2024, 9, 25),
                                               date(2024, 10, 7)]


def test_an_unparseable_name_is_skipped_not_guessed(tmp_path) -> None:
    (tmp_path / "notes.zip").write_bytes(b"")
    (tmp_path / SCENE).write_bytes(b"")
    assert mod.dates_from_scenes(tmp_path) == [date(2024, 9, 25)]


def test_no_scenes_is_an_empty_list(tmp_path) -> None:
    assert mod.dates_from_scenes(tmp_path) == []


# -- the URL ---------------------------------------------------------------

def test_the_url_matches_the_bulk_layout() -> None:
    assert mod.url_for(date(2024, 9, 25)).endswith(
        "/2024/AIS_2024_09_25.zip")


def test_the_year_directory_tracks_the_date() -> None:
    """New Year's Eve and New Year's Day sit in different directories. A
    hardcoded year works for eleven months of testing."""
    assert "/2024/" in mod.url_for(date(2024, 12, 31))
    assert "/2025/" in mod.url_for(date(2025, 1, 1))


# -- what counts as downloaded ---------------------------------------------

def test_a_truncated_file_does_not_count_as_downloaded(tmp_path, monkeypatch):
    """A 2 KB error page saved with a .zip extension would otherwise be
    skipped as 'already downloaded' forever, and fail much later inside DuckDB
    with a message about Parquet."""
    monkeypatch.setattr(mod, "DEST", tmp_path)
    d = date(2024, 9, 25)
    mod.dest_for(d).write_bytes(b"x" * 2048)
    assert not mod.already_have(d)


def test_a_full_sized_file_counts(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mod, "DEST", tmp_path)
    d = date(2024, 9, 25)
    p = mod.dest_for(d)
    with p.open("wb") as fh:
        fh.truncate(mod.MIN_PLAUSIBLE_BYTES + 1)
    assert mod.already_have(d)


def test_a_missing_file_does_not_count(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mod, "DEST", tmp_path)
    assert not mod.already_have(date(2024, 9, 25))


def test_the_size_floor_is_below_a_real_day_and_above_an_error_page() -> None:
    """A national day is 250-400 MB. The floor has to sit between that and
    anything a failed request could leave behind."""
    assert 1 * 1024 * 1024 < mod.MIN_PLAUSIBLE_BYTES < 250 * 1024 * 1024


# -- the frontier ----------------------------------------------------------

def test_a_date_past_the_frontier_is_known_to_be_unpublished() -> None:
    """The six scenes already on disk are from August and September 2026.
    Requesting their AIS would 404 four times and look like a network
    problem."""
    from angels.config import AIS_FRONTIER
    frontier = date.fromisoformat(AIS_FRONTIER)
    assert date(2026, 9, 10) > frontier
    assert date(2024, 9, 25) <= frontier


# -- resume ----------------------------------------------------------------

def test_a_partial_transfer_is_never_renamed_into_place() -> None:
    """The .part file only becomes the .zip after the size check. Renaming
    first and validating later is how a half-file gets skipped as complete on
    the next run."""
    import inspect
    src = inspect.getsource(mod.download)
    assert src.index("MIN_PLAUSIBLE_BYTES") < src.index("part.replace(out)")


def test_a_server_ignoring_range_restarts_rather_than_appending() -> None:
    """Asked to resume, some servers send 200 and the whole file. Appending
    that to what is already on disk splices two copies together into an
    archive that unzips to nonsense -- and it is full-sized, so every size
    check passes."""
    import inspect
    src = inspect.getsource(mod.download)
    assert "status_code == 200" in src and "unlink" in src
