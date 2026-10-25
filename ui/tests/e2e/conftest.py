"""A real window on a free local port, with seeded data, driven by Chromium.

Nothing leaves this computer: the browser may only load this window, so any
outside request is refused, and the stage work is replaced in-process.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn
from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.queries import mark_enriched, replace_awards, upsert_tender, upsert_vendor, utcnow

from ops_ui.app import create_app

#: Tender id, title, organisation, status, contract date, company, email.
TENDERS = (
    ("2026_ALPHA_901001_1", "Road repair", "PWD Goa", "AOC", "10-Jul-2026", "Alpha Builders", "office@alpha.example"),
    ("2026_BETA_902002_1", "Bridge works", "PWD Goa", "AOC", "20-Aug-2026", "Beta Works", None),
    ("2026_GAMMA_903003_1", "Cable supply", "NIT Goa", "Retender", "05-Sep-2026", "Gamma Traders", "sales@gamma.example"),
)


@dataclass
class Window:
    base: str
    path: Path

    def url(self, path: str) -> str:
        return self.base + path


def seed(path: Path) -> None:
    ensure_schema(engine(path))
    with session(engine(path)) as current:
        for tender_id, title, organisation, status, date, company, email in TENDERS:
            vendor_id = upsert_vendor(current, name_raw=company, state="Goa")
            if email:
                mark_enriched(current, vendor_id, email=email, phone=None)
            upsert_tender(
                current,
                tender_id=tender_id,
                title=title,
                organisation=organisation,
                status=status,
                contract_date=date,
                contract_value="INR 1,00,000",
                scraped_at=utcnow(),
            )
            replace_awards(
                current, tender_id, [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": company}]
            )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def window(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.sqlite3"
    # Stage code finds the shared file by PIPELINE_DB; point it where the window reads.
    monkeypatch.setenv("PIPELINE_DB", str(path))
    seed(path)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(path), host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(250):
        if server.started:
            break
        time.sleep(0.02)
    yield Window(f"http://127.0.0.1:{port}", path)
    server.should_exit = True
    thread.join(10)


@pytest.fixture
def browser_page(page, window):
    """The page may load this window and nothing else."""

    def gate(route):
        if route.request.url.startswith(window.base):
            route.continue_()
        else:
            route.abort()

    page.route("**/*", gate)
    page.set_default_timeout(15_000)
    return page
