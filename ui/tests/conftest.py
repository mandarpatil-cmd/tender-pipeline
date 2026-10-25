from __future__ import annotations

import shutil
import socket
import sys
import threading

import pytest

from pipeline_core.db import dispose_engines

try:
    import playwright  # noqa: F401
except ImportError:
    # The browser suite runs with: uv run --with pytest-playwright python -m pytest tests/e2e
    collect_ignore = ["e2e"]

#: Every setting the real stage .env files can hold. Tests see a fresh clone.
_STAGE_SETTINGS = (
    "OPENROUTER_API_KEY",
    "MODEL",
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
def _dispose_engines(_isolated_files):
    """Runs a test left going are stopped first; Windows will not delete a file still open.

    Depending on `_isolated_files` makes this teardown run before the real
    folders are patched back in, so a late run still writes to the temp ones.
    """
    yield
    from ops_ui import jobs

    for stop in list(jobs._STOPS.values()):
        stop.set()
    for waiting in list(jobs._CAPTCHA.values()):
        waiting.event.set()
    for thread in threading.enumerate():
        if thread.name.endswith("(_execute)"):
            thread.join(timeout=10)
    for state in (jobs._STOPS, jobs._CAPTCHA, jobs._ANSWERED, jobs._READING, jobs._TYPED):
        state.clear()
    dispose_engines()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No test may reach the portal, a model or a mailbox. Loopback stays open.

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
def _isolated_files(tmp_path, monkeypatch):
    """No test may open the real database, settings, previews, attachments or folders.

    `PIPELINE_ROOT` moves the scrape output, the saved portal form and the PDF
    zips to an empty folder. The two pitch PDFs are copied, so a test that
    removes one leaves the real file in place.
    """
    monkeypatch.setenv("PIPELINE_DB", str(tmp_path / "default.sqlite3"))
    for name in _STAGE_SETTINGS:
        monkeypatch.setenv(name, "")
    from ops_ui.runs import load_campaign

    campaign = load_campaign()
    import graph.auth

    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("PIPELINE_ROOT", str(tmp_path / "root"))
    monkeypatch.setattr(campaign, "ENV_PATH", tmp_path / ".env")
    for name in ("SENDER_NAME", "SENDER_DESIGNATION", "SENDER_ORG", "SENDER_MOBILE", "SENDER_EMAIL"):
        monkeypatch.setattr(campaign, name, "")
    monkeypatch.setattr(campaign, "PREVIEW_DIR", tmp_path / "previews")
    attachments = tmp_path / "attachments"
    shutil.copytree(campaign.BASE / "attachments", attachments)
    monkeypatch.setattr(campaign, "ATTACHMENT_DIR", attachments)
    monkeypatch.setattr(graph.auth, "CACHE_PATH", tmp_path / "token_cache.json")
