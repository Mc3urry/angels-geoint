
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from angels.adapters.aviation import opensky as osky
from angels.adapters.aviation.opensky import (
    ICAO24, LATITUDE, LONGITUDE, ON_GROUND, TokenManager,
    rows_to_reports, row_to_report, split_into_tracks,
)
from angels.core.models import Track

T0 = 1788048000  # unix seconds


def row(icao, callsign, lon, lat, *, t=T0, on_ground=False,
        vel=200.0, trk=90.0, baro=10000.0, geo=10200.0):
    """Build a state vector with the real 18-field layout."""
    return [icao, callsign, "United States", t, t, lon, lat, baro,
            on_ground, vel, trk, 0.0, None, geo, "1200", False, 0, 0]


# Five aircraft on the ground at DCA, as actually observed, plus two airborne.
DCA_ROWS = [
    row("a22358", "RPA5592 ", -77.038, 38.842, on_ground=True, vel=0.0, geo=None, baro=None),
    row("aa8164", "AAL1972 ", -77.042, 38.854, on_ground=True, vel=0.0, geo=None, baro=None),
    row("a36661", "AAL1218 ", -77.041, 38.846, on_ground=True, vel=0.0, geo=None, baro=None),
    row("a6e376", "JIA5131 ", -77.041, 38.844, on_ground=True, vel=0.0, geo=None, baro=None),
    row("a3a9a3", "DAL737  ", -77.039, 38.843, on_ground=True, vel=0.0, geo=None, baro=None),
    row("abc123", "SWA1234 ", -76.610, 39.290),
    row("def456", "JBU0099 ", -76.500, 39.400),
]


# -- field mapping ---------------------------------------------------------

def test_lon_lat_are_not_transposed() -> None:
    """The single easiest way to silently ruin everything downstream.

    OpenSky puts LONGITUDE at index 5 and LATITUDE at 6. Swap them and every
    aircraft appears somewhere plausible but wrong, with no error raised.
    """
    r = row_to_report(row("abc123", "TEST", -76.610, 39.290))
    assert r is not None
    assert r.position.lat == pytest.approx(39.290)
    assert r.position.lon == pytest.approx(-76.610)


def test_report_carries_kinematics_and_altitude() -> None:
    r = row_to_report(row("abc123", "TEST", -76.6, 39.3, vel=180.0, trk=270.0))
    assert r.position.speed_mps == 180.0
    assert r.position.heading_deg == 270.0
    assert r.position.alt_m == 10200.0        # geo preferred over baro
    assert r.source == "adsb"
    assert r.platform_id == "abc123"


def test_falls_back_to_baro_altitude() -> None:
    r = row_to_report(row("abc123", "T", -76.6, 39.3, geo=None, baro=9000.0))
    assert r.position.alt_m == 9000.0


def test_unpositioned_rows_are_dropped() -> None:
    """In contact but no fix. Real, and not a position."""
    assert row_to_report(row("abc123", "T", None, None)) is None
    assert row_to_report(row("abc123", "T", -76.6, None)) is None


def test_uncertainty_is_always_present() -> None:
    r = row_to_report(row("abc123", "T", -76.6, 39.3))
    assert r.position.uncertainty_m > 0


def test_timestamp_is_utc_aware() -> None:
    r = row_to_report(row("abc123", "T", -76.6, 39.3))
    assert r.position.t.tzinfo is not None
    assert r.position.t == datetime.fromtimestamp(T0, tz=timezone.utc)


def test_prefers_time_position_over_last_contact() -> None:
    """An aircraft transmitting with a stale GPS fix.

    Using last_contact would place it where it no longer is.
    """
    r = row("abc123", "T", -76.6, 39.3)
    r[3] = T0 - 60          # time_position: the fix is a minute old
    r[4] = T0               # last_contact: still talking
    rep = row_to_report(r)
    assert rep.position.t == datetime.fromtimestamp(T0 - 60, tz=timezone.utc)


def test_uses_last_contact_when_position_time_missing() -> None:
    r = row("abc123", "T", -76.6, 39.3)
    r[3] = None
    rep = row_to_report(r)
    assert rep.position.t == datetime.fromtimestamp(T0, tz=timezone.utc)


# -- the ground filter -----------------------------------------------------

def test_ground_aircraft_excluded_by_default() -> None:
    """The DCA gate problem.

    Five parked airliners reporting identical coordinates for hours would each
    become a high-confidence loiter event. Excluding them is the default for
    exactly that reason.
    """
    reports = rows_to_reports(DCA_ROWS)
    assert len(reports) == 2
    assert {r.platform_id for r in reports} == {"abc123", "def456"}


def test_ground_aircraft_available_when_asked_for() -> None:
    reports = rows_to_reports(DCA_ROWS, include_on_ground=True)
    assert len(reports) == 7


# -- track splitting -------------------------------------------------------

def _reports(icao: str, offsets: list[int]) -> list:
    return [row_to_report(row(icao, "T", -76.6 + i * 0.01, 39.3, t=T0 + off))
            for i, off in enumerate(offsets)]


def test_reports_group_by_platform() -> None:
    reports = _reports("aaa111", [0, 10, 20]) + _reports("bbb222", [0, 10])
    tracks = split_into_tracks(reports)
    assert len(tracks) == 2
    assert {t.platform_id for t in tracks} == {"aaa111", "bbb222"}


def test_long_silence_splits_one_id_into_two_tracks() -> None:
    """Monday and Thursday are two flights, not one with a straight line
    drawn through three days of nothing."""
    reports = _reports("aaa111", [0, 10, 20, 100_000, 100_010])
    tracks = split_into_tracks(reports, max_gap_s=900)
    assert len(tracks) == 2
    assert len(tracks[0]) == 3
    assert len(tracks[1]) == 2


def test_short_gaps_do_not_split() -> None:
    reports = _reports("aaa111", [0, 10, 400, 410])
    assert len(split_into_tracks(reports, max_gap_s=900)) == 1


def test_tracks_come_back_time_ordered() -> None:
    reports = _reports("aaa111", [30, 0, 20, 10])
    tr = split_into_tracks(reports)[0]
    times = [r.position.t for r in tr.reports]
    assert times == sorted(times)


def test_split_preserves_every_report() -> None:
    reports = _reports("aaa111", [0, 10, 100_000, 100_010, 200_000])
    tracks = split_into_tracks(reports, max_gap_s=900)
    assert sum(len(t) for t in tracks) == len(reports)


def test_tracks_are_usable_by_core() -> None:
    """The point of the adapter: what comes out is a core Track, and the
    running fix works on it with no aviation knowledge involved."""
    tracks = split_into_tracks(_reports("aaa111", [0, 10, 20]))
    tr = tracks[0]
    assert isinstance(tr, Track)
    assert tr.domain == "air"
    mid = tr.position_at(tr.t_start + timedelta(seconds=5))
    assert mid is not None
    assert tr.t_start <= mid.t <= tr.t_end


# -- auth ------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload): self._p = payload
    def raise_for_status(self): return self
    def json(self): return self._p


class FakeClient:
    """Counts token requests so we can prove caching and refresh."""

    def __init__(self, expires_in=1800):
        self.calls = 0
        self.expires_in = expires_in

    def post(self, url, data=None, headers=None):
        self.calls += 1
        return FakeResponse({"access_token": f"tok{self.calls}",
                             "expires_in": self.expires_in})


def test_missing_credentials_fail_loudly(monkeypatch) -> None:
    monkeypatch.delenv("OPENSKY_CLIENT_ID", raising=False)
    monkeypatch.delenv("OPENSKY_CLIENT_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="credentials missing"):
        TokenManager()


def test_token_is_cached_not_refetched() -> None:
    fake = FakeClient()
    tm = TokenManager("id", "secret", client=fake)
    assert tm.token() == "tok1"
    assert tm.token() == "tok1"
    assert tm.token() == "tok1"
    assert fake.calls == 1


def test_token_refreshes_before_it_expires() -> None:
    """Tokens last 30 minutes. Refresh on a margin, not on a 401 -- a poller
    that discovers expiry by failing is one you debug at 2am."""
    fake = FakeClient(expires_in=1800)
    tm = TokenManager("id", "secret", margin_s=120, client=fake)
    assert tm.token() == "tok1"

    # jump to 100s before expiry, inside the 120s margin
    tm._expires_at = __import__("time").time() + 100
    assert tm.token() == "tok2"
    assert fake.calls == 2


def test_headers_carry_the_bearer_token() -> None:
    tm = TokenManager("id", "secret", client=FakeClient())
    assert tm.headers() == {"Authorization": "Bearer tok1"}


# -- fetch -----------------------------------------------------------------

class FakeGetClient(FakeClient):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows
        self.last_params = None

    def get(self, url, params=None, headers=None):
        self.last_params = params
        return FakeResponse({"time": T0, "states": self.rows})


def test_fetch_states_sends_bbox_in_opensky_order() -> None:
    """AOI is GeoJSON order (min_lon, min_lat, max_lon, max_lat).
    OpenSky wants them named. Getting this unpack wrong queries the wrong box.
    """
    fake = FakeGetClient(DCA_ROWS)
    tm = TokenManager("id", "secret", client=fake)
    aoi = (-77.2, 38.7, -76.0, 39.8)
    t, rows = osky.fetch_states(aoi, tm, client=fake)

    assert fake.last_params == {"lamin": 38.7, "lomin": -77.2,
                                "lamax": 39.8, "lomax": -76.0}
    assert t == datetime.fromtimestamp(T0, tz=timezone.utc)
    assert len(rows) == 7


def test_fetch_states_handles_empty_sky() -> None:
    """states is null, not [], when nothing matches. Do not let that raise."""
    class Empty(FakeClient):
        def get(self, url, params=None, headers=None):
            return FakeResponse({"time": T0, "states": None})
    fake = Empty()
    tm = TokenManager("id", "secret", client=fake)
    _, rows = osky.fetch_states((-1, -1, 1, 1), tm, client=fake)
    assert rows == []
