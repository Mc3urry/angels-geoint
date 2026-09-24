"""Shared test fixtures, and two hard rules: tests reach neither the
network nor this machine's own data.

WHY THIS FILE EXISTS

tests/test_cdse.py once contained a test asserting that a TokenManager with no
credentials raises MissingCredentials. It passed in CI and failed on the
author's laptop, because TokenManager falls back to CDSE_USERNAME and
CDSE_PASSWORD, and angels.config loads .env on import for every test. On a
machine with working credentials the test did not measure what it claimed:
it made a REAL authentication request to Copernicus, got a real token, and
reported DID NOT RAISE.

Three faults in one line, and only one of them was visible:

  1. The test asserted the opposite of what it measured.
  2. The suite could not run offline, or in CI, or on a train.
  3. `pytest` silently hit an external identity server on every invocation.
     Run often enough -- and a test suite is run constantly -- that is
     indistinguishable from a brute-force attempt against the user's own
     account, and could get it locked.

The specific bug is fixed. This file stops the CLASS of bug: any test that
tries to open a socket now fails loudly, naming the host it wanted, instead of
quietly succeeding against a live service.

It is the same principle the detectors are built on. An absence of failures
means nothing unless failure was possible -- and a test that can reach the
real service is not testing the code, it is testing the internet.

Tests that genuinely need a transport should inject a fake client; see
tests/test_opensky.py for the pattern.

The second rule, added 2026-09-24, is the same principle applied to the
filesystem: a suite whose result depends on what the collectors happened to
have written this minute is not measuring the code. See
_no_production_data below.
"""

from __future__ import annotations

import ipaddress
import socket
import sys
from pathlib import Path

import pytest


class NetworkAccessAttempted(RuntimeError):
    """A test tried to reach a host outside this machine."""


LOOPBACK_NAMES = {"localhost", "localhost.localdomain"}


def is_loopback(address) -> bool:
    """Is this connection staying on this machine?

    LOOPBACK MUST BE ALLOWED, and the reason is a genuine platform trap.

    On Windows, asyncio's ProactorEventLoop has no real socketpair(), so
    CPython emulates one by opening an actual TCP connection to 127.0.0.1.
    Starlette's TestClient builds an event loop for every request. So on
    Windows -- and ONLY on Windows -- every single API test opens a real
    socket, and a naive block on all AF_INET traffic fails ten tests that
    never touch the internet.

    On Linux socketpair() uses AF_UNIX and none of this happens, which is why
    the first version of this fixture passed in the author's Linux sandbox and
    broke the moment it reached the machine that matters.

    The thing worth preventing is a test silently reaching an EXTERNAL
    service. A connection to this machine is a test double, a local server, or
    the interpreter's own plumbing -- never a live third-party API.
    """
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if not isinstance(host, str):
        return False
    if host in LOOPBACK_NAMES:
        return True

    # Parse rather than string-match. `host.startswith("127.")` looks right
    # and is not: it also matches the HOSTNAME "127.example.com", which is a
    # perfectly registrable domain pointing anywhere its owner likes. A guard
    # that can be walked past by naming a server carefully is not a guard.
    #
    # ipaddress also gets the cases the string version missed for free --
    # ::ffff:127.0.0.1, 127.0.0.53, and every other loopback spelling.
    try:
        ip = ipaddress.ip_address(host.split("%")[0])    # strip any zone id
    except ValueError:
        return False        # a hostname, not an address: treat as external

    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_loopback or ip.is_unspecified


@pytest.fixture(autouse=True)
def _no_network(monkeypatch, request):
    """Fail any test that opens a network connection.

    Opt out for a deliberately live test with:

        @pytest.mark.network
        def test_something_against_the_real_api(): ...

    There are currently none, and adding one should be an argued decision
    rather than a convenience -- a suite that sometimes needs the internet is
    a suite nobody trusts to be green.
    """
    if request.node.get_closest_marker("network"):
        return

    real_connect = socket.socket.connect

    def blocked(self, address, *a, **kw):
        # Unix domain sockets, anything non-TCP, and anything staying on this
        # machine are left alone. The target is a test silently reaching an
        # EXTERNAL service -- not local IPC, not TestClient, not the event
        # loop's own self-pipe.
        if (self.family in (socket.AF_INET, socket.AF_INET6)
                and not is_loopback(address)):
            raise NetworkAccessAttempted(
                f"test tried to connect to {address!r}. Tests must not reach "
                f"external services -- inject a fake client instead. If this "
                f"test genuinely must be live, mark it @pytest.mark.network "
                f"and say why in the docstring."
            )
        return real_connect(self, address, *a, **kw)

    monkeypatch.setattr(socket.socket, "connect", blocked)


# --------------------------------------------------------------------------
# the second thing tests must not touch: this machine's real data
# --------------------------------------------------------------------------

class ProductionDataVisible(RuntimeError):
    """A test could see data/ on the machine it is running on."""


@pytest.fixture(autouse=True)
def _no_production_data(monkeypatch, tmp_path, request):
    """Point every data path at an empty temp directory.

    WHY THIS EXISTS -- and it is the network guard's story told again.

    On 2026-09-24 seven tests failed on a machine where nothing had changed
    in the code they test. `/live?domain=sea` returned 439 vessels to a test
    that had injected exactly one; a stream stubbed to have been listening
    four seconds reported 401.9; `/tracks` returned 200 where the test
    demanded a 404 for a missing archive. The numbers were real. The suite
    was reading the live collectors' snapshots out of data/interim/live and
    their Parquet out of data/raw, because the route prefers the collector's
    table over its own socket -- which is the correct behaviour in
    production and ruinous in a test.

    The failure is not that seven tests broke. It is that the suite's
    result had silently become a function of WHETHER THE COLLECTORS WERE
    RUNNING. It went green earlier the same evening only because they were
    down. Making them permanent -- the whole goal of the scheduled-task work
    -- is what turned the suite red, so the reward for fixing the
    infrastructure was a test suite that no longer meant anything.

    A test that reads production data is the filesystem version of a test
    that reaches the network, and it deserves the same answer: not a
    workaround in the seven tests that noticed, but a rule that makes the
    class impossible. Every Path under DATA is rebound to an empty temp
    tree, so a route that goes looking for a collector's table finds
    nothing and falls back exactly as it would on a cold machine.

    Opt out, with an argument, via:

        @pytest.mark.realdata
        def test_something_against_the_archive(): ...
    """
    if request.node.get_closest_marker("realdata"):
        return

    import angels.config as cfg

    # WHAT IS REDIRECTED, AND WHERE THE LINE IS.
    #
    # Only the trees a RUNNING PROCESS writes: raw archives, interim
    # snapshots, derived events. Those are the ones whose contents depend on
    # what the collectors did this minute, and reading them is what makes a
    # test result a fact about the machine rather than about the code.
    #
    # data/reference/ is deliberately left real. It is checked in, it is
    # identical on every machine, and a test that reads the shoreline or the
    # territorial-limit table is reading a fixture that happens to live on
    # disk -- not eavesdropping on production. Redirecting it would break
    # honest tests to punish a sin they did not commit.
    #
    # ROOT is left alone for the same reason: the API serves web/ from there,
    # and a test that cannot find index.html would be a puzzle, not a guard.
    generated = [p.resolve() for p in (cfg.RAW, cfg.INTERIM, cfg.EVENTS)]
    sandbox = tmp_path / "data"
    sandbox.mkdir(parents=True, exist_ok=True)

    def redirected(value):
        """The same path, under the sandbox -- or None if it is not ours."""
        try:
            here = Path(value).resolve()
        except (OSError, RuntimeError):
            return None
        for root in generated:
            try:
                rel = here.relative_to(root)
            except ValueError:
                continue
            return sandbox / root.name / rel
        return None

    # angels.config FIRST, so a module imported later in the run -- a script
    # pulled in by a test halfway down the file -- gets the sandbox too.
    # `from angels.config import RAW` copies the value at import time, which
    # is exactly why the rebinding below has to happen per module as well.
    modules = [cfg] + [m for name, m in list(sys.modules.items())
                       if m is not None and m is not cfg
                       and (name == "angels" or name.startswith("angels."))]

    for mod in modules:
        for attr, value in list(vars(mod).items()):
            if not isinstance(value, Path):
                continue
            moved = redirected(value)
            if moved is not None:
                monkeypatch.setattr(mod, attr, moved, raising=False)


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers", "network: test genuinely requires internet access")
    config.addinivalue_line(
        "markers", "realdata: test genuinely reads this machine's data/ tree")
