from __future__ import annotations

import socket

import pytest

from pipeline_core.db import dispose_engines

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def _dispose_engines():
    """Engines are cached per path; Windows will not delete a file still open."""
    yield
    dispose_engines()


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """A test that opens the default database gets a throwaway file, never the real one."""
    monkeypatch.setenv("PIPELINE_DB", str(tmp_path / "default.sqlite3"))


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No test may use the network. Loopback stays open.

    A blocked attempt raises like a real outage, and the test still fails at
    teardown, so code that swallows the error cannot hide it.
    """
    tried: list[object] = []
    connect = socket.socket.connect
    lookup = socket.getaddrinfo

    def guarded_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in _LOOPBACK:
            tried.append(address)
            raise OSError(f"Tests do not use the network: {address}")
        return connect(sock, address)

    def guarded_lookup(host, *args, **kwargs):
        if host is not None and host not in _LOOPBACK:
            tried.append(host)
            raise socket.gaierror(f"Tests do not use the network: {host}")
        return lookup(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_lookup)
    yield tried
    assert not tried, f"A test tried to reach the network: {tried}"
