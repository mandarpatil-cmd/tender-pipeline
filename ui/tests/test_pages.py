"""Every page opens, and every link, form and poll on it goes to a real route."""

from __future__ import annotations

import re
import threading
from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.queries import (
    mark_enriched,
    record_llm_run,
    record_outreach,
    replace_awards,
    upsert_tender,
    upsert_vendor,
    utcnow,
)

from ops_ui.app import create_app
from ops_ui.jobs import get_job, start_job


def _seed(path) -> int:
    """Two winners on one tender, a lookup, a send, and one finished run."""
    bind = engine(path)
    ensure_schema(bind)
    with session(bind) as current:
        winner = upsert_vendor(current, name_raw="Large Electricals", state="Assam")
        other = upsert_vendor(current, name_raw="Other Bidder Ltd")
        mark_enriched(current, winner, email="found@large.example", phone="9876543210")
        upsert_tender(
            current,
            tender_id="LARGE",
            title="Large rooms",
            organisation="NIT Durgapur",
            status="AOC",
            contract_date="21-Sep-2026",
            contract_value="INR 100",
            scraped_at=utcnow(),
        )
        replace_awards(
            current,
            "LARGE",
            [
                {"bid_number": "1", "vendor_id": winner, "bidder_name": "Large Electricals"},
                {"bid_number": "2", "vendor_id": other, "bidder_name": "Other Bidder Ltd"},
            ],
        )
        record_llm_run(
            current,
            vendor_id=winner,
            model="test-model",
            prompt_version="v1",
            input_json="{}",
            output_json='{"email":"found@large.example"}',
            status="ok",
        )
        record_outreach(
            current,
            vendor_id=winner,
            tender_id="LARGE",
            email="found@large.example",
            subject="Hello",
            transport="outlook",
            status="failed",
            error="mailbox full",
        )
    job_id = start_job(path, "status", {}, lambda log, stop: log.write("counted") or 0)
    for _ in range(100):
        if get_job(path, job_id).state == "done":
            return job_id
        threading.Event().wait(0.02)
    raise AssertionError(f"the seeded run ended {get_job(path, job_id).state}")


def _pages(job_id: int) -> list[str]:
    return [
        "/",
        "/scrape",
        "/enrich",
        "/mail",
        "/jobs",
        "/tender/new",
        "/award?tender_id=LARGE&bid=1",
        "/award?tender_id=LARGE&bid=2",
        f"/jobs/{job_id}",
        f"/jobs/{job_id}/live",
        f"/scrape?job={job_id}",
        f"/enrich?job={job_id}",
        f"/mail?job={job_id}",
        "/?q=large&tender_status=AOC&sort=contract_value&dir=desc",
        "/enrich?enrichment_status=done",
        "/mail?outreach_status=failed",
        "/jobs?stage=status&state=done",
    ]


def _routes(app):
    for route in app.routes:
        router = getattr(route, "original_router", None)
        yield from router.routes if router is not None else [route]


def _patterns(app) -> list[re.Pattern]:
    patterns = [re.compile(r"^/static/[\w.\-]+$")]
    for route in _routes(app):
        if isinstance(route, APIRoute):
            patterns.append(re.compile("^" + re.sub(r"\{[^}]+\}", r"[^/]+", route.path) + "$"))
    return patterns


class _Links(HTMLParser):
    ATTRS = {"href", "action", "formaction", "hx-get", "hx-post", "src"}

    def __init__(self) -> None:
        super().__init__()
        self.found: list[str] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in self.ATTRS and value:
                self.found.append(value)


@pytest.fixture
def seeded(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    job_id = _seed(path)
    return TestClient(create_app(path)), job_id


def test_every_page_opens(seeded):
    client, job_id = seeded
    for page in _pages(job_id):
        answer = client.get(page)
        assert answer.status_code == 200, page
        assert "Traceback" not in answer.text and "Internal Server Error" not in answer.text, page


def test_every_link_form_and_poll_goes_to_a_route(seeded):
    client, job_id = seeded
    patterns = _patterns(client.app)
    broken: set[tuple[str, str]] = set()
    for page in _pages(job_id):
        parser = _Links()
        parser.feed(client.get(page).text)
        for link in parser.found:
            if "{{" in link or "{%" in link or "/None" in link:
                broken.add((page, link))
                continue
            parts = urlsplit(link)
            if parts.scheme or parts.netloc or link.startswith(("#", "data:")):
                continue
            target = parts.path or urlsplit(page).path
            if not any(pattern.match(target) for pattern in patterns):
                broken.add((page, link))
    assert not broken


def test_the_pages_stay_up_without_a_database(tmp_path):
    client = TestClient(create_app(tmp_path / "missing.sqlite3"))
    for page in ["/", "/scrape", "/enrich", "/mail", "/jobs", "/award?tender_id=X&bid=1", "/tender/new"]:
        answer = client.get(page)
        assert answer.status_code < 500, page
    assert not (tmp_path / "missing.sqlite3").exists()


def test_an_unknown_run_is_a_404_not_an_error(seeded):
    client, _ = seeded
    for page in ["/jobs/999", "/jobs/999/live", "/jobs/999/log", "/jobs/999/pdfs", "/jobs/999/captcha"]:
        assert client.get(page).status_code == 404, page


def test_the_style_sheet_and_htmx_are_served(seeded):
    client, _ = seeded
    css = client.get("/static/app.css")
    js = client.get("/static/htmx.min.js")

    assert css.status_code == 200 and "text/css" in css.headers["content-type"]
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert "htmx" in js.text[:2000]


def test_the_fonts_are_served_from_the_window(seeded):
    """Every font the style sheet names is a local file, so no page calls out."""
    client, _ = seeded
    css = client.get("/static/app.css").text
    fonts = re.findall(r'url\("([^"]+\.woff2)"\)', css)

    assert len(fonts) == 4
    for name in fonts:
        answer = client.get(f"/static/{name}")
        assert answer.status_code == 200, name
        assert answer.content[:4] == b"wOF2", name
    assert "fonts.googleapis" not in client.get("/").text


def test_the_api_schema_is_not_published(seeded):
    """The window is not an API. Its routes are not listed for anyone to call."""
    client, _ = seeded
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
