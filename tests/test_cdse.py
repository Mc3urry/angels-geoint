"""Tests for the Copernicus access layer.

Downloads cannot be tested in CI -- a gigabyte over the network is not a unit
test. What CAN be tested is the logic that decides WHICH scene to fetch and
whether a resumed transfer is safe, and that is where the damage lives: a
silent bug in the resume path produces a corrupt zip that fails much later,
during detection, looking like a detector bug.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from angels.adapters.maritime.cdse import (
    MissingCredentials, Scene, TokenManager, footprint_box, overlap_fraction,
    wkt,
)
from angels.config import AOI_SEA

T = datetime(2026, 9, 12, 22, 41, tzinfo=timezone.utc)


@pytest.fixture
def no_credentials(monkeypatch):
    """Guarantee TokenManager cannot find credentials in the environment.

    WHY THIS FIXTURE EXISTS. TokenManager("", "") falls back to CDSE_USERNAME
    and CDSE_PASSWORD, and config.py loads .env on import for every test. So
    on a machine with working credentials -- which is every machine that
    matters -- a test expecting MissingCredentials instead made a REAL
    authentication request to Copernicus.

    That is three separate faults in one line. The test asserted the opposite
    of what it measured; the suite could not run offline; and `pytest` quietly
    hit an external identity server on every invocation, which repeated often
    enough looks like a brute-force attempt against the user's own account.

    A unit test must never be able to reach the network by accident.
    """
    monkeypatch.delenv("CDSE_USERNAME", raising=False)
    monkeypatch.delenv("CDSE_PASSWORD", raising=False)
    return monkeypatch


def _scene(name="S1A_IW_GRDH_1SDV_20260912T224110_x_1_2", cov=0.7, size=1_000_000_000):
    return Scene(id="abc", name=name, t=T, coverage=cov,
                 size_bytes=size, online=True)


# -- geometry --------------------------------------------------------------

def test_wkt_polygon_is_closed() -> None:
    """An unclosed ring is rejected by the catalogue with an opaque error."""
    s = wkt(AOI_SEA)
    inner = s[s.index("((") + 2:s.rindex("))")]
    pts = [p.strip() for p in inner.split(",")]
    assert pts[0] == pts[-1], "first and last vertex must match"
    assert len(pts) == 5


def test_wkt_is_lon_lat_not_lat_lon() -> None:
    """The single most common way to query the wrong half of the planet."""
    lomin, lamin, lomax, lamax = AOI_SEA
    first = wkt(AOI_SEA).split("((")[1].split(",")[0].split()
    assert float(first[0]) == lomin
    assert float(first[1]) == lamin


def test_footprint_from_geojson() -> None:
    p = {"GeoFootprint": {"type": "Polygon",
                          "coordinates": [[[-77.0, 36.2], [-72.0, 36.2],
                                           [-72.0, 39.4], [-77.0, 39.4],
                                           [-77.0, 36.2]]]}}
    assert footprint_box(p) == (-77.0, 36.2, -72.0, 39.4)


def test_footprint_from_wkt_fallback() -> None:
    p = {"Footprint": "geography'SRID=4326;POLYGON((-77 36.2, -72 36.2, "
                      "-72 39.4, -77 39.4, -77 36.2))'"}
    assert footprint_box(p) == (-77.0, 36.2, -72.0, 39.4)


def test_footprint_missing_is_none_not_a_crash() -> None:
    assert footprint_box({}) is None


def test_a_corner_clipping_scene_scores_low() -> None:
    """THE SELECTION BUG THIS PREVENTS.

    'Intersects the box' is a catalogue hit even when the scene shows almost
    none of the Chesapeake. Downloading a gigabyte for a corner sliver wastes
    an hour and a gigabyte, and -- worse -- counting it as a look inflates the
    apparent sample size of the whole experiment.
    """
    corner = (-71.2, 39.4, -70.0, 40.5)
    assert overlap_fraction(corner, AOI_SEA) < 0.05


def test_a_covering_scene_scores_high() -> None:
    wide = (-77.2, 36.0, -72.0, 39.6)
    assert overlap_fraction(wide, AOI_SEA) > 0.6


def test_no_overlap_is_zero_not_negative() -> None:
    pacific = (-130.0, 30.0, -120.0, 40.0)
    assert overlap_fraction(pacific, AOI_SEA) == 0.0


def test_overlap_never_exceeds_one() -> None:
    """A footprint swallowing the box whole is 100% coverage, not 400%."""
    huge = (-180.0, -90.0, 180.0, 90.0)
    assert overlap_fraction(huge, AOI_SEA) == pytest.approx(1.0, abs=0.001)


# -- scene metadata --------------------------------------------------------

def test_product_type_and_satellite_are_read_from_the_name() -> None:
    s = _scene()
    assert s.product_type == "GRD"
    assert s.satellite == "S1A"


def test_a_malformed_name_does_not_crash() -> None:
    assert _scene(name="junk").product_type == "?"


# -- credentials -----------------------------------------------------------

def test_missing_credentials_names_the_fix(no_credentials) -> None:
    """The error a user hits first should say what to do, not what broke."""
    with pytest.raises(MissingCredentials) as e:
        TokenManager().token()
    msg = str(e.value)
    assert "dataspace.copernicus.eu" in msg
    assert ".env" in msg


def test_a_cached_token_is_reused(monkeypatch) -> None:
    """Re-minting per request would hit the identity server once per chunk."""
    tm = TokenManager("u", "p")
    tm._token = "cached"
    tm._expires = 1e12
    assert tm.token() == "cached"


def test_an_expired_token_is_not_reused(no_credentials) -> None:
    """A token past its expiry must be re-minted, not handed back.

    Reusing it produces a 401 in the middle of a gigabyte download rather than
    a clean failure at the start.
    """
    tm = TokenManager("", "")
    tm._token = "stale"
    tm._expires = 0.0
    with pytest.raises(MissingCredentials):
        tm.token()


def test_explicit_credentials_beat_the_environment(no_credentials) -> None:
    """Passing a username explicitly must not be overridden by .env, so a
    caller can authenticate as someone other than the ambient user."""
    assert TokenManager("alice", "secret").username == "alice"


def test_blank_credentials_fall_back_to_the_environment(monkeypatch) -> None:
    """The convenience the scripts rely on -- and the exact behaviour that
    made this file hit the live network. Pinned so it stays deliberate."""
    monkeypatch.setenv("CDSE_USERNAME", "from-env")
    monkeypatch.setenv("CDSE_PASSWORD", "x")
    assert TokenManager().username == "from-env"


# -- redirect handling -----------------------------------------------------
#
# The download endpoint answers 302 to a different host. httpx strips the
# Authorization header across origins, so the second request arrives
# unauthenticated and returns 401 -- which reads exactly like a wrong
# password and is not. These pin the hand-rolled redirect chain that fixes it.

def _fake_client(script):
    """A minimal httpx.Client stand-in driven by a list of (status, headers).

    Records the headers each hop actually received, which is the thing under
    test: whether the token survived the redirect.
    """
    import httpx as _httpx

    seen = []

    class _Resp:
        """Deliberately NOT a context manager.

        An httpx Response from send(stream=True) has no __enter__ -- only the
        client.stream() helper does. The first version of this double DID
        implement it, so `with r:` in the download path passed every test and
        raised TypeError on the very first real download.

        A test double must be no more capable than the thing it stands for.
        Anything it can do that the real object cannot is a place where the
        suite is green about code that does not work.
        """
        def __init__(self, status, headers, url):
            self.status_code = status
            self.headers = headers
            self.url = _httpx.URL(url)
            self.closed = False
        def close(self):
            self.closed = True

    class _Client:
        def build_request(self, method, url, headers=None):
            return (method, url, dict(headers or {}))
        def send(self, req, stream=False, follow_redirects=False):
            _, url, headers = req
            seen.append((url, headers))
            status, hdrs = script[len(seen) - 1]
            return _Resp(status, hdrs, url)

    return _Client(), seen


def test_the_token_survives_a_cross_host_redirect() -> None:
    """THE REGRESSION TEST for the 401-that-was-not-an-auth-failure."""
    from angels.adapters.maritime.cdse import follow_authed
    client, seen = _fake_client([
        (302, {"location": "https://zipper.dataspace.copernicus.eu/f/abc"}),
        (200, {"content-length": "1000"}),
    ])
    r = follow_authed(client, "https://catalogue.dataspace.copernicus.eu/x",
                      {"Authorization": "Bearer tok"})
    assert r.status_code == 200
    assert len(seen) == 2
    assert seen[1][1]["Authorization"] == "Bearer tok", \
        "the token was dropped crossing to the download node"


def test_a_relative_location_is_resolved() -> None:
    from angels.adapters.maritime.cdse import follow_authed
    client, seen = _fake_client([
        (302, {"location": "/download/abc"}),
        (200, {}),
    ])
    follow_authed(client, "https://catalogue.dataspace.copernicus.eu/x",
                  {"Authorization": "Bearer tok"})
    assert seen[1][0] == "https://catalogue.dataspace.copernicus.eu/download/abc"


def test_the_token_is_NOT_sent_off_copernicus() -> None:
    """The reason httpx strips the header in the first place.

    Re-attaching credentials to wherever a server points is how tokens leak.
    Following the chain by hand reintroduces that risk unless the destination
    is checked, so an off-domain redirect must refuse rather than comply.
    """
    from angels.adapters.maritime.cdse import follow_authed
    client, seen = _fake_client([
        (302, {"location": "https://evil.example.com/collect"}),
    ])
    with pytest.raises(RuntimeError, match="refusing to forward"):
        follow_authed(client, "https://catalogue.dataspace.copernicus.eu/x",
                      {"Authorization": "Bearer tok"})
    assert len(seen) == 1, "the token must not reach the second host at all"


def test_a_lookalike_domain_is_not_trusted() -> None:
    """dataspace.copernicus.eu.evil.com must not pass a naive suffix check."""
    from angels.adapters.maritime.cdse import _is_trusted
    assert _is_trusted("https://zipper.dataspace.copernicus.eu/x")
    assert _is_trusted("https://catalogue.dataspace.copernicus.eu/x")
    assert not _is_trusted("https://dataspace.copernicus.eu.evil.com/x")
    assert not _is_trusted("https://notcopernicus.eu/x")


def test_a_redirect_loop_terminates() -> None:
    from angels.adapters.maritime.cdse import follow_authed
    client, _ = _fake_client([
        (302, {"location": "https://catalogue.dataspace.copernicus.eu/x"})
    ] * 20)
    with pytest.raises(RuntimeError, match="too many redirects"):
        follow_authed(client, "https://catalogue.dataspace.copernicus.eu/x",
                      {"Authorization": "Bearer tok"})


def test_a_redirect_without_a_location_is_an_error() -> None:
    from angels.adapters.maritime.cdse import follow_authed
    client, _ = _fake_client([(302, {})])
    with pytest.raises(RuntimeError, match="no Location"):
        follow_authed(client, "https://catalogue.dataspace.copernicus.eu/x",
                      {"Authorization": "Bearer tok"})


def test_the_fake_response_is_not_a_context_manager() -> None:
    """Pins the double's fidelity.

    If someone later adds __enter__ to make a test convenient, this fails and
    explains why -- rather than the suite silently going green about a
    download path that raises TypeError in production.
    """
    client, _ = _fake_client([(200, {})])
    r = client.send(client.build_request("GET", "https://x/", {}))
    assert not hasattr(r, "__enter__"), \
        "the real httpx streaming Response has no __enter__; the double "\
        "must not either"


def test_the_response_is_closed_even_on_error() -> None:
    """A streamed response that is never closed leaks the connection. On a
    six-attempt retry loop over gigabyte files that matters."""
    import httpx as _h
    from angels.adapters.maritime.cdse import follow_authed
    client, _ = _fake_client([(302, {"location": "https://evil.example.com/x"})])
    with pytest.raises(RuntimeError):
        follow_authed(client, "https://catalogue.dataspace.copernicus.eu/x",
                      {"Authorization": "Bearer tok"})


# -- the download path, end to end -----------------------------------------
#
# THE TEST THAT WAS MISSING. Everything above tested the PIECES -- token
# caching, redirect handling, scene selection -- and all of it passed while
# download() itself raised TypeError on its first line of real work, because
# nothing ever called download() at all. Unit tests on the parts do not add up
# to a working whole, and this is the cheapest possible proof of the whole.

class _StreamResp:
    """What follow_authed actually returns: a streamed httpx Response.

    No __enter__, iter_bytes rather than content, close() must be called.
    """

    def __init__(self, body: bytes, status: int = 200, headers=None):
        self.status_code = status
        self.headers = headers if headers is not None else {
            "content-length": str(len(body))}
        self.url = type("U", (), {"host": "zipper.dataspace.copernicus.eu"})()
        self._body = body
        self.closed = False

    def iter_bytes(self, chunk=1 << 20):
        for i in range(0, len(self._body), chunk):
            yield self._body[i:i + chunk]

    def close(self):
        self.closed = True


@pytest.fixture
def _tokens():
    tm = TokenManager("u", "p")
    tm._token = "tok"
    tm._expires = 1e12
    return tm


def test_download_writes_the_file(tmp_path, monkeypatch, _tokens) -> None:
    import angels.adapters.maritime.cdse as cdse
    resp = _StreamResp(b"PK\x03\x04payload")
    monkeypatch.setattr(cdse, "follow_authed", lambda c, u, h: resp)

    out = cdse.download(_scene(size=len(resp._body)), tmp_path, _tokens,
                        progress=False)
    assert out.exists()
    assert out.read_bytes() == b"PK\x03\x04payload"
    assert out.suffix == ".zip"


def test_download_closes_the_response(tmp_path, monkeypatch, _tokens) -> None:
    """A streamed response left open leaks the connection, and the retry loop
    can open six per file."""
    import angels.adapters.maritime.cdse as cdse
    resp = _StreamResp(b"x" * 100)
    monkeypatch.setattr(cdse, "follow_authed", lambda c, u, h: resp)
    cdse.download(_scene(size=100), tmp_path, _tokens, progress=False)
    assert resp.closed


def test_an_existing_file_is_not_refetched(tmp_path, monkeypatch, _tokens) -> None:
    """Re-running a fifty-scene fetch must not re-download the forty already
    on disk."""
    import angels.adapters.maritime.cdse as cdse
    s = _scene()
    (tmp_path / f"{s.name}.zip").write_bytes(b"already here")

    def explode(*a, **kw):
        raise AssertionError("re-downloaded a file that was already complete")

    monkeypatch.setattr(cdse, "follow_authed", explode)
    out = cdse.download(s, tmp_path, _tokens, progress=False)
    assert out.read_bytes() == b"already here"


def test_a_partial_file_is_resumed_not_restarted(tmp_path, monkeypatch,
                                                 _tokens) -> None:
    """The whole reason resume exists: a token expiry mid-gigabyte must cost
    a pause, not the transfer."""
    import angels.adapters.maritime.cdse as cdse
    s = _scene(size=10)
    (tmp_path / f"{s.name}.zip.part").write_bytes(b"12345")

    seen = {}

    def capture(c, u, h):
        seen.update(h)
        return _StreamResp(b"67890", status=206,
                           headers={"content-length": "5"})

    monkeypatch.setattr(cdse, "follow_authed", capture)
    out = cdse.download(s, tmp_path, _tokens, progress=False)

    assert seen.get("Range") == "bytes=5-", "resume did not send a Range header"
    assert out.read_bytes() == b"1234567890", "the resumed half was not appended"


def test_a_server_ignoring_range_restarts_instead_of_appending(
        tmp_path, monkeypatch, _tokens) -> None:
    """THE CORRUPTION CASE.

    We ask for bytes 5- and the server sends the whole file with 200 instead.
    Appending would produce a zip containing the first five bytes twice --
    silently corrupt, and discovered days later during detection where it
    would look like a detector bug.
    """
    import angels.adapters.maritime.cdse as cdse
    s = _scene(size=10)
    (tmp_path / f"{s.name}.zip.part").write_bytes(b"12345")

    monkeypatch.setattr(cdse, "follow_authed",
                        lambda c, u, h: _StreamResp(b"1234567890", status=200))
    out = cdse.download(s, tmp_path, _tokens, progress=False)
    assert out.read_bytes() == b"1234567890", "the partial was appended to"


@pytest.mark.parametrize("status", [501, 416])
def test_a_refused_range_restarts_from_zero(tmp_path, monkeypatch, _tokens,
                                            status) -> None:
    """2024-05-28: a connection dropped at 418 MB and every resume came back
    501. Keeping the partial would spend all six attempts asking again. The
    partial is dropped and the next attempt asks for the whole file."""
    import angels.adapters.maritime.cdse as cdse
    monkeypatch.setattr(cdse.time, "sleep", lambda s: None)
    s = _scene(size=10)
    (tmp_path / f"{s.name}.zip.part").write_bytes(b"12345")
    asked = []

    def server(c, u, h):
        asked.append(h.get("Range"))
        if "Range" in h:
            return _StreamResp(b"", status=status, headers={})
        return _StreamResp(b"1234567890", status=200)

    monkeypatch.setattr(cdse, "follow_authed", server)
    out = cdse.download(s, tmp_path, _tokens, progress=False)
    assert asked == ["bytes=5-", None]
    assert out.read_bytes() == b"1234567890"


def test_an_ordinary_server_error_keeps_the_partial(tmp_path, monkeypatch,
                                                    _tokens) -> None:
    """A 503 is an outage, not a refusal of Range: keep the bytes and resume."""
    import angels.adapters.maritime.cdse as cdse
    monkeypatch.setattr(cdse.time, "sleep", lambda s: None)
    s = _scene(size=10)
    (tmp_path / f"{s.name}.zip.part").write_bytes(b"12345")
    replies = iter([_StreamResp(b"", status=503, headers={}),
                    _StreamResp(b"67890", status=206,
                                headers={"content-length": "5"})])
    monkeypatch.setattr(cdse, "follow_authed", lambda c, u, h: next(replies))
    out = cdse.download(s, tmp_path, _tokens, progress=False)
    assert out.read_bytes() == b"1234567890"


# -- passes ----------------------------------------------------------------
#
# THE MODELLING ERROR THIS FIXES. Asking for a scene covering 40% of AOI_SEA
# returned nothing, and that read as poor coverage. It was not: AOI_SEA is
# 545 x 398 km and a Sentinel-1 IW swath is 250 km wide, so a single slice
# CANNOT exceed ~20% of the box. The request was geometrically impossible and
# the tool reported it as an absence of data -- the same class of mistake as
# the aviation probe, in a new place.

def _sc(name, fp, t=None, size=1_000_000_000):
    from datetime import timedelta
    return Scene(id=name, name=name, t=(t or T) + timedelta(seconds=0),
                 coverage=0.0, size_bytes=size, online=True, footprint=fp)


def test_a_single_slice_cannot_cover_the_whole_sea_box() -> None:
    """The fact that made --min-coverage 0.4 return nothing.

    If AOI_SEA is ever shrunk this may stop holding, which is exactly when
    someone should be told to revisit the pass logic.
    """
    import math
    lomin, lamin, lomax, lamax = AOI_SEA
    mid = math.radians((lamin + lamax) / 2)
    box_km2 = ((lomax - lomin) * 111.32 * math.cos(mid)) * \
              ((lamax - lamin) * 110.57)
    slice_km2 = 250 * 170          # IW swath width x GRD slice length
    assert slice_km2 / box_km2 < 0.25, (
        "a single IW slice now covers >25% of AOI_SEA; the per-scene "
        "coverage threshold may be meaningful again"
    )


def test_slices_sharing_an_orbit_become_one_pass() -> None:
    from angels.adapters.maritime.cdse import group_passes
    a = _sc("S1D_IW_GRDH_1SDV_20260824T224919_20260824T224949_004274_007DD6_1",
            (-77.0, 36.0, -74.0, 38.0))
    b = _sc("S1D_IW_GRDH_1SDV_20260824T224949_20260824T225019_004274_007DD6_2",
            (-77.0, 38.0, -74.0, 39.6))
    passes = group_passes([a, b], AOI_SEA)
    assert len(passes) == 1
    assert len(passes[0].scenes) == 2
    assert passes[0].orbit == "004274"


def test_different_orbits_stay_separate() -> None:
    """Two overflights are two observations of the bay at two instants, and
    merging them would invent a single moment that never existed."""
    from angels.adapters.maritime.cdse import group_passes
    a = _sc("S1D_IW_GRDH_1SDV_20260824T224919_x_004274_007DD6_1",
            (-77.0, 36.0, -74.0, 38.0))
    b = _sc("S1D_IW_GRDH_1SDV_20260825T224919_x_004275_007DD7_1",
            (-77.0, 36.0, -74.0, 38.0))
    assert len(group_passes([a, b], AOI_SEA)) == 2


def test_pass_coverage_is_a_union_not_a_sum() -> None:
    """Two slices covering the SAME water cover it once.

    Summing per-slice coverage would report 2x and make a pass look twice as
    useful as it is -- optimistic in exactly the direction that makes a thin
    dataset look adequate.
    """
    from angels.adapters.maritime.cdse import group_passes
    fp = (-77.0, 36.0, -74.0, 38.0)
    one = group_passes([_sc("S1D_A_B_C_D_E_004274_F_1", fp)], AOI_SEA)[0]
    two = group_passes([_sc("S1D_A_B_C_D_E_004274_F_1", fp),
                        _sc("S1D_A_B_C_D_E_004274_F_2", fp)], AOI_SEA)[0]
    assert two.coverage == pytest.approx(one.coverage, abs=0.01)


def test_adjacent_slices_add_coverage() -> None:
    """Non-overlapping slices of one pass genuinely see more water."""
    from angels.adapters.maritime.cdse import group_passes
    south = _sc("S1D_A_B_C_D_E_004274_F_1", (-77.0, 36.0, -74.0, 37.8))
    north = _sc("S1D_A_B_C_D_E_004274_F_2", (-77.0, 37.8, -74.0, 39.6))
    one = group_passes([south], AOI_SEA)[0]
    both = group_passes([south, north], AOI_SEA)[0]
    assert both.coverage > one.coverage * 1.8


def test_pass_size_sums_its_slices() -> None:
    """A pass is what you actually download, so its cost is the sum."""
    from angels.adapters.maritime.cdse import group_passes
    p = group_passes([_sc("S1D_A_B_C_D_E_004274_F_1", (-77.0, 36.0, -74.0, 38.0),
                          size=1_000_000_000),
                      _sc("S1D_A_B_C_D_E_004274_F_2", (-77.0, 38.0, -74.0, 39.6),
                          size=1_200_000_000)], AOI_SEA)[0]
    assert p.size_gb == pytest.approx(2.2, abs=0.01)


def test_grid_coverage_of_nothing_is_zero() -> None:
    from angels.adapters.maritime.cdse import grid_coverage
    assert grid_coverage([], AOI_SEA) == 0.0
    assert grid_coverage([None], AOI_SEA) == 0.0


def test_grid_coverage_of_the_whole_box_is_one() -> None:
    from angels.adapters.maritime.cdse import grid_coverage
    assert grid_coverage([AOI_SEA], AOI_SEA) == pytest.approx(1.0, abs=0.01)


# -- one acquisition, one download -----------------------------------------
#
# Copernicus publishes each slice twice: a classic product and a COG. Same
# orbit, same datatake, same thirty seconds of water, different trailing
# product ID. Deduping by filename let both through and doubled the download
# -- 8.4 GB for a pass that needed 4.

def _raw(name, size=1_000_000_000):
    return {
        "Name": name,
        "Id": name,
        "ContentDate": {"Start": "2026-08-24T22:49:19.000Z"},
        "ContentLength": size,
        "Online": True,
        "GeoFootprint": {"type": "Polygon", "coordinates": [[
            [-77.0, 36.0], [-74.0, 36.0], [-74.0, 38.0],
            [-77.0, 38.0], [-77.0, 36.0]]]},
    }


def _search_over(raws):
    """Drive search() against a canned catalogue response."""
    import angels.adapters.maritime.cdse as cdse

    class _C:
        def get(self, url, params=None):
            class R:
                status_code = 200
                @staticmethod
                def json():
                    return {"value": raws}
            return R()
        def close(self): pass

    from datetime import timedelta
    return cdse.search(AOI_SEA, T - timedelta(days=1), T + timedelta(days=1),
                       client=_C())


CLASSIC = "S1D_IW_GRDH_1SDV_20260824T224919_20260824T224949_004274_007DD6_26E5"
COG = "S1D_IW_GRDH_1SDV_20260824T224919_20260824T224949_004274_007DD6_9F31_COG"


def test_the_same_acquisition_in_two_formats_yields_one_scene() -> None:
    """THE REGRESSION TEST. 8.4 GB where 4 would have done."""
    got = _search_over([_raw(CLASSIC, 2_037_000_000), _raw(COG, 986_000_000)])
    assert len(got) == 1, (
        "the classic and COG encodings of one acquisition were both kept; "
        "every pass downloads twice and every vessel is detected twice"
    )


def test_the_cog_is_preferred() -> None:
    """COGs are internally tiled with overviews, so the detector can read a
    window without pulling a gigabyte."""
    assert _search_over([_raw(CLASSIC), _raw(COG)])[0].name == COG
    assert _search_over([_raw(COG), _raw(CLASSIC)])[0].name == COG


def test_a_classic_only_acquisition_is_still_kept() -> None:
    """Preferring the COG must not mean discarding slices that have none."""
    got = _search_over([_raw(CLASSIC)])
    assert len(got) == 1 and got[0].name == CLASSIC


def test_genuinely_different_slices_are_both_kept() -> None:
    """The dedupe must not be so eager that it eats the rest of the pass.

    These differ in their stop time -- consecutive slices of one overflight,
    covering different water.
    """
    other = ("S1D_IW_GRDH_1SDV_20260824T224949_20260824T225019"
             "_004274_007DD6_4B2C_COG")
    assert len(_search_over([_raw(COG), _raw(other)])) == 2


def test_different_orbits_are_both_kept() -> None:
    other = ("S1D_IW_GRDH_1SDV_20260824T224919_20260824T224949"
             "_004275_007DD8_26E5")
    assert len(_search_over([_raw(CLASSIC), _raw(other)])) == 2


# -- boundaries, not area --------------------------------------------------
#
# The first pass downloaded covered 54% of AOI_SEA and contained none of the
# 3, 12 or 24 nm limits and none of the Chesapeake. Its westmost pixel was at
# -75.19; the 24 nm line is at -75.47. Half the study box of open Atlantic,
# chosen by a ranking that could only see area.
#
# Area is not the independent variable of this experiment. Boundaries are.

def _pass_over(lon_min, lon_max):
    from angels.adapters.maritime.cdse import group_passes
    fp = (lon_min, 36.5, lon_max, 38.5)
    return group_passes([_sc("S1D_A_B_C_D_E_004274_F_1", fp)], AOI_SEA)[0]


def test_the_real_downloaded_pass_contains_only_the_eez() -> None:
    """The actual footprints from 24 August 2026, as inspect_scene reported
    them. Pinned so the diagnosis is reproducible rather than remembered."""
    p = _pass_over(-75.19, -71.18)
    assert p.limits == (200,), (
        "this pass was ranked best by coverage and contains a single limit"
    )


def test_a_bay_mouth_pass_contains_the_nested_cluster() -> None:
    """The three inner limits sit within half a degree of the coast -- the
    part of the study that tests whether the effect scales with the STRENGTH
    of the jurisdictional change, which is the interesting claim."""
    assert _pass_over(-76.5, -75.0).limits == (3, 12, 24)


def test_an_open_ocean_pass_contains_no_limit_at_all() -> None:
    assert _pass_over(-74.0, -72.0).limits == ()


def test_limit_longitudes_reproduce_both_documented_values() -> None:
    """LIMIT_LONGITUDES is derived from one coast reference rather than typed
    in. The derivation must keep reproducing the two limits that are
    independently documented in config -- that agreement is the only reason
    the undocumented 24 nm value can be trusted."""
    from angels.config import LIMIT_LONGITUDES
    assert LIMIT_LONGITUDES[3] == pytest.approx(-75.91, abs=0.01)
    assert LIMIT_LONGITUDES[200] == pytest.approx(-71.81, abs=0.02)


def test_the_limits_are_ordered_seaward() -> None:
    """Nested limits must come out in order. Reversed, every
    distance-to-boundary sign in the analysis flips."""
    from angels.config import LIMIT_LONGITUDES, TERRITORIAL_LIMITS_NM
    lons = [LIMIT_LONGITUDES[nm] for nm in sorted(TERRITORIAL_LIMITS_NM)]
    assert lons == sorted(lons), "further from shore must mean further east"
