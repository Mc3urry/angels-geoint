"""Tests for the AIS availability probe.

No network. The bisection and the control logic are separated from the HTTP,
because those are the parts that can be wrong in a way that matters -- and one
of them already was: the first version of this script reported "nothing found"
when it could not reach the server at all, which is an error dressed as an
absence, in the one tool whose job is to tell those apart.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "check_ais_lag",
    Path(__file__).resolve().parents[1] / "scripts" / "check_ais_lag.py")
mod = importlib.util.module_from_spec(SPEC)
# Registered BEFORE execution. @dataclass resolves its annotations through
# sys.modules[cls.__module__], so a module executed without being registered
# raises AttributeError on the decorator -- from inside dataclasses, with a
# traceback that says nothing about the real cause.
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)

TODAY = date(2026, 9, 16)


def frontier(cut: date, calls: list | None = None):
    """An existence test where everything on or before `cut` is present."""
    def exists(d: date) -> bool:
        if calls is not None:
            calls.append(d)
        return d <= cut
    return exists


# -- the bisection ---------------------------------------------------------

CONTROL = date(2022, 6, 15)          # 1554 days before TODAY


@pytest.mark.parametrize("lag", [0, 1, 7, 30, 150, 165, 400, 900, 1553])
def test_it_finds_the_frontier_exactly(lag) -> None:
    cut = TODAY - timedelta(days=lag)
    assert mod.newest_available(frontier(cut), TODAY, CONTROL) == cut


def test_it_searches_past_a_year_of_silence() -> None:
    """THE REGRESSION.

    The version this replaces searched backwards from today up to a fixed 400
    days and then reported 'no source has anything within 400 days -- a real
    publishing gap'. On the real NOAA archive the frontier sat just beyond
    that edge, so the horizon of the SEARCH was reported as the edge of the
    DATA: confident, specific, and wrong in the direction that stops you
    looking.
    """
    cut = TODAY - timedelta(days=800)
    assert mod.newest_available(frontier(cut), TODAY, CONTROL) == cut


def test_it_is_cheap() -> None:
    """Each probe is a request against a 320 MB file. Bisecting four years is
    about eleven of them; a linear walk would be fifteen hundred."""
    calls: list = []
    mod.newest_available(frontier(TODAY - timedelta(days=800), calls),
                         TODAY, CONTROL)
    assert len(calls) < 15, f"{len(calls)} requests"


def test_the_control_is_the_floor_not_a_guess() -> None:
    """The control was verified present before bisection starts, so the worst
    honest answer is the control date itself -- never None, and never a date
    that was never checked."""
    assert mod.newest_available(lambda d: d <= CONTROL, TODAY,
                                CONTROL) == CONTROL


def test_a_missing_day_inside_the_archive_stays_conservative() -> None:
    """Availability is assumed monotonic. An isolated hole may stop bisection
    short of the true frontier -- reporting LESS recent data than exists,
    which sends the SAR window further back and costs only download time. The
    opposite error would send it forward into dates with no ground truth and
    would not surface until matching produced nothing."""
    hole = TODAY - timedelta(days=157)
    cut = TODAY - timedelta(days=150)
    got = mod.newest_available(lambda d: d <= cut and d != hole,
                               TODAY, CONTROL)
    assert got is not None and got <= cut


# -- the control, which is the point --------------------------------------

def fake_probe(exists_url):
    """Stand in for probe(); `exists_url` says whether a URL is served.

    A predicate rather than a set, because availability has to stay MONOTONIC
    for bisection to mean anything, and a hand-written set of URLs quietly
    stops being monotonic past whichever range it happens to cover -- which
    tests the fixture rather than the code.
    """
    def p(url, timeout=25.0):
        ok = exists_url(url)
        return ok, "200 (HEAD)" if ok else "404 (HEAD)"
    return p


def served_from(source, cut):
    """Every date on or before `cut`, under this source's naming.

    The URL is parsed back with the same template that built it, so a source
    only ever answers for its own URLs -- which is what makes the
    two-source test meaningful rather than a coincidence of ordering.
    """
    def exists_url(url: str) -> bool:
        try:
            when = datetime.strptime(url, source.template).date()
        except ValueError:
            return False
        return when <= cut
    return exists_url


def test_a_source_whose_control_fails_is_not_reported_as_empty(monkeypatch):
    """THE POINT OF THE FILE.

    A server that refuses every request must not be indistinguishable from a
    server with no recent data. The first produces "not checked" and a
    diagnosis; only the second is a statement about AIS.
    """
    src = mod.Source("dead", "https://nowhere/%Y_%m_%d.zip", date(2022, 6, 15))
    monkeypatch.setattr(mod, "probe", fake_probe(lambda url: False))
    results, any_control = mod.survey([src], TODAY)
    assert any_control is False
    assert results[0][2] is None


def test_a_source_whose_control_passes_is_bisected(monkeypatch):
    src = mod.Source("live", "https://host/%Y_%m_%d.zip", date(2022, 6, 15))
    cut = TODAY - timedelta(days=157)
    monkeypatch.setattr(mod, "probe", fake_probe(served_from(src, cut)))

    results, any_control = mod.survey([src], TODAY)
    assert any_control is True
    assert results[0][2] == cut


def test_the_furthest_forward_source_wins(monkeypatch):
    """Mid-migration, two hosts serve overlapping ranges. The useful answer is
    the later frontier, not the first source in the list."""
    old = mod.Source("old", "https://a/%Y_%m_%d.zip", date(2022, 6, 15))
    new = mod.Source("new", "https://b/%Y_%m_%d.parquet", date(2024, 6, 15))
    old_ok = served_from(old, TODAY - timedelta(days=300))
    new_ok = served_from(new, TODAY - timedelta(days=150))
    monkeypatch.setattr(mod, "probe",
                        fake_probe(lambda u: old_ok(u) or new_ok(u)))

    results, _ = mod.survey([old, new], TODAY)
    live = [(s, n) for s, _, n in results if n]
    best, newest = max(live, key=lambda p: p[1])
    assert best.name == "new"
    assert newest == TODAY - timedelta(days=150)


def test_a_source_does_not_answer_for_another_sources_urls(monkeypatch):
    """Two hosts mid-migration. If the fake -- or the real probe -- answered
    across naming schemes, a dead source would inherit a live one's frontier
    and the table would recommend a pattern that serves nothing."""
    old = mod.Source("old", "https://a/%Y_%m_%d.zip", date(2022, 6, 15))
    new = mod.Source("new", "https://b/%Y_%m_%d.parquet", date(2024, 6, 15))
    only_new = served_from(new, TODAY - timedelta(days=150))
    monkeypatch.setattr(mod, "probe", fake_probe(only_new))

    results, any_control = mod.survey([old, new], TODAY)
    by_name = {s.name: n for s, _, n in results}
    assert by_name["old"] is None, "a dead source must not borrow a frontier"
    assert by_name["new"] == TODAY - timedelta(days=150)
    assert any_control is True


def test_every_source_carries_a_control_that_predates_the_lag() -> None:
    """A control inside the publishing lag would fail for the ordinary reason
    and take the whole source down with it."""
    for s in mod.SOURCES:
        assert (date(2026, 9, 16) - s.control).days > mod.DOCUMENTED_LAG_DAYS[1]


# -- the request itself ----------------------------------------------------

def test_it_never_downloads_a_whole_file() -> None:
    """320 MB per probe would be a rude way to ask a yes/no question."""
    import inspect
    src = inspect.getsource(mod.probe)
    assert "head" in src
    assert "Range" in src and "bytes=0-0" in src


def test_head_refusals_fall_through_to_a_ranged_get() -> None:
    """Plenty of static hosts answer HEAD with 403 or 405 while serving the
    file. Treating that as 'missing' is exactly the error-as-absence this
    script exists to avoid."""
    import inspect
    src = inspect.getsource(mod.probe)
    assert "403" in src and "405" in src


def test_the_url_matches_noaas_bulk_layout() -> None:
    legacy = mod.SOURCES[0]
    assert legacy.url(date(2026, 4, 3)).endswith("/2026/AIS_2026_04_03.zip")


# -- publication cadence ---------------------------------------------------

@pytest.mark.parametrize("d", [date(2024, 12, 31), date(2023, 12, 26),
                               date(2019, 12, 30)])
def test_a_year_end_frontier_is_recognised_as_annual(d) -> None:
    """A frontier on 31 December is not a rolling lag with the tail trimmed.
    It means the archive is released a year at a time, so the next tranche is
    a whole year on no announced date -- which is the difference between
    'wait a few weeks' and 'build the study inside 2024'."""
    assert mod.looks_annual(d)


@pytest.mark.parametrize("d", [date(2024, 12, 15), date(2025, 1, 2),
                               date(2024, 6, 15), date(2024, 11, 30)])
def test_an_arbitrary_frontier_is_not_called_annual(d) -> None:
    assert not mod.looks_annual(d)
