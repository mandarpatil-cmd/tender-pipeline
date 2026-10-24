import socket
from pathlib import Path

import pytest

from pipeline_core.db import dispose_engines

FIXTURES = Path(__file__).parent / "fixtures"
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture
def html():
    def load(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return load


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point every test at its own database.

    `Store()` now defaults to the pipeline's shared database. Without this a test
    run would scribble on real scraped data, so `PIPELINE_DB` is redirected for
    the whole suite rather than trusting each test to pass a path.
    """
    monkeypatch.setenv("PIPELINE_DB", str(tmp_path / "pipeline.sqlite3"))
    yield
    # Engines are cached per path, and Windows will not delete an open file.
    dispose_engines()


@pytest.fixture(autouse=True)
def _no_real_key(monkeypatch):
    """The real 01-scrape/.env is read on every lookup. An empty value wins over it."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No test may reach the portal or a model. Loopback stays open.

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
