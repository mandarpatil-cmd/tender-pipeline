from __future__ import annotations

import shutil
import socket

import pytest

from pipeline_core.db import dispose_engines, engine, ensure_schema

#: Every setting the real 03-outreach/.env can hold. Tests see a fresh clone.
_MAIL_SETTINGS = (
    "SENDER_NAME",
    "SENDER_DESIGNATION",
    "SENDER_ORG",
    "SENDER_MOBILE",
    "SENDER_EMAIL",
    "OUTLOOK_USER",
    "OUTLOOK_PASSWORD",
    "GRAPH_CLIENT_ID",
    "GRAPH_TENANT_ID",
    "GMAIL_USER",
    "GMAIL_APP_PASSWORD",
)
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No test may reach a mailbox or a model. Loopback stays open.

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


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test gets its own database, never the real pipeline one."""
    path = tmp_path / "pipeline.sqlite3"
    monkeypatch.setenv("PIPELINE_DB", str(path))
    ensure_schema(engine(path))
    yield path
    dispose_engines()


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    """No real mailbox setting, previews folder, attachment or token cache.

    The two pitch PDFs are copied, so a test that removes one leaves the real
    file in place.
    """
    for name in _MAIL_SETTINGS:
        monkeypatch.setenv(name, "")
    import campaign
    import graph.auth

    monkeypatch.setattr(campaign, "ENV_PATH", tmp_path / ".env")
    for name in ("SENDER_NAME", "SENDER_DESIGNATION", "SENDER_ORG", "SENDER_MOBILE", "SENDER_EMAIL"):
        monkeypatch.setattr(campaign, name, "")
    monkeypatch.setattr(campaign, "PREVIEW_DIR", tmp_path / "previews")
    attachments = tmp_path / "attachments"
    shutil.copytree(campaign.BASE / "attachments", attachments)
    monkeypatch.setattr(campaign, "ATTACHMENT_DIR", attachments)
    monkeypatch.setattr(graph.auth, "CACHE_PATH", tmp_path / "token_cache.json")
    monkeypatch.setattr(graph.auth, "CLIENT_ID", None)
    monkeypatch.setattr(graph.auth, "TENANT_ID", None)
