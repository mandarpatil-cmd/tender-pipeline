from __future__ import annotations

import socket

import pytest

from pipeline_core.db import dispose_engines, engine, ensure_schema

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test gets its own database, never the real pipeline one."""
    path = tmp_path / "pipeline.sqlite3"
    monkeypatch.setenv("PIPELINE_DB", str(path))
    ensure_schema(engine(path))
    yield path
    dispose_engines()


@pytest.fixture(autouse=True)
def _model_env(monkeypatch):
    """`llm_runs.model` is read from the environment even when the chain is fake."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MODEL", "google/gemini-2.5-flash")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No test may reach a model. Loopback stays open.

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
