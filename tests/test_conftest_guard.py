"""Tests for the network guard in conftest.py.

The guard decides whether every other test in the suite is allowed to run, so
a bug in it is a bug in all of them. Its first version blocked every AF_INET
connection, which is correct on Linux and catastrophically wrong on Windows:
there, asyncio's ProactorEventLoop emulates socketpair() with a real TCP
connection to 127.0.0.1, and Starlette's TestClient builds an event loop per
request. Ten API tests that never touch the internet failed on the author's
machine while passing in a Linux sandbox.

So the predicate is tested directly, on both address families, rather than
trusted.
"""

from __future__ import annotations

import socket

import pytest

from tests.conftest import NetworkAccessAttempted, is_loopback


# -- what must be allowed --------------------------------------------------

@pytest.mark.parametrize("address", [
    ("127.0.0.1", 8000),          # TestClient, local servers
    ("127.0.0.1", 64920),         # the Windows socketpair emulation
    ("127.0.0.53", 53),           # systemd-resolved stub
    ("localhost", 5432),
    ("::1", 8000, 0, 0),          # IPv6 loopback, 4-tuple form
    ("::ffff:127.0.0.1", 80),     # v4-mapped
    ("0.0.0.0", 0),
])
def test_loopback_is_allowed(address) -> None:
    assert is_loopback(address)


# -- what must be blocked --------------------------------------------------

@pytest.mark.parametrize("address", [
    ("identity.dataspace.copernicus.eu", 443),
    ("opensky-network.org", 443),
    ("93.184.216.34", 80),
    ("8.8.8.8", 53),
    ("2606:4700:4700::1111", 443),     # a real public IPv6
    ("127.example.com", 443),          # starts with "127." but is a HOSTNAME
])
def test_external_hosts_are_blocked(address) -> None:
    assert not is_loopback(address)


def test_a_hostname_beginning_127_is_not_loopback() -> None:
    """The prefix check must not be fooled by a registrable domain that
    happens to start with the same characters."""
    assert not is_loopback(("127.attacker.example", 443))


# -- shape robustness ------------------------------------------------------

@pytest.mark.parametrize("address", [None, (), "not-a-tuple", (None, 80),
                                     (b"127.0.0.1", 80)])
def test_odd_address_shapes_do_not_crash(address) -> None:
    """A guard that raises TypeError inside socket.connect would be far worse
    than the problem it prevents -- it would break connections it meant to
    permit, in a traceback nobody could read."""
    assert is_loopback(address) is False


# -- the guard end to end --------------------------------------------------

def test_the_guard_blocks_a_real_outbound_connection() -> None:
    """Proof the fixture is actually installed and biting.

    If this ever passes silently, the autouse fixture has stopped working and
    the whole suite is free to reach the internet again.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkAccessAttempted) as e:
            s.connect(("example.com", 80))
        assert "example.com" in str(e.value)
        assert "@pytest.mark.network" in str(e.value)
    finally:
        s.close()


def test_the_guard_permits_loopback_end_to_end() -> None:
    """A local listener must still be reachable -- this is what the ten
    Windows API-test failures were about."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.settimeout(5)
            client.connect(("127.0.0.1", port))    # must not raise
        finally:
            client.close()
    finally:
        server.close()
