"""A scrape, lookup, or send stays on the page that started it."""

from __future__ import annotations

from fastapi.testclient import TestClient
from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.models import UiJob
from pipeline_core.queries import (
    mark_enriched,
    replace_awards,
    upsert_tender,
    upsert_vendor,
    utcnow,
)

from ops_ui.app import create_app
from ops_ui.present import job_id_from


def _job(current, stage: str, *, params: str = "{}", log: str = "started") -> int:
    job = UiJob(
        stage=stage,
        state="done",
        operator="local",
        params_json=params,
        log=log,
        started_at=utcnow(),
        finished_at=utcnow(),
    )
    current.add(job)
    current.flush()
    return int(job.job_id)


def test_scrape_job_page_lists_only_that_run(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    bind = engine(path)
    ensure_schema(bind)
    with session(bind) as current:
        job_id = _job(current, "scrape", log="saved the tender")
        winner = upsert_vendor(current, name_raw="This Run Ltd")
        other = upsert_vendor(current, name_raw="Older Run Ltd")
        upsert_tender(
            current,
            tender_id="THIS",
            title="This run",
            scraped_at=utcnow(),
            scrape_job_id=job_id,
        )
        upsert_tender(current, tender_id="OLDER", title="Older", scraped_at=utcnow(), scrape_job_id=9)
        replace_awards(
            current,
            "THIS",
            [{"bid_number": "1", "vendor_id": winner, "bidder_name": "This Run Ltd"}],
        )
        replace_awards(
            current,
            "OLDER",
            [{"bid_number": "1", "vendor_id": other, "bidder_name": "Older Run Ltd"}],
        )
    client = TestClient(create_app(path))
    html = client.get(f"/scrape?job={job_id}").text

    assert "saved the tender" in html
    assert ">THIS</a>" in html
    assert ">OLDER</a>" not in html
    assert f'href="/scrape/export.xlsx?job={job_id}"' in html
    assert "Stored awards, with their filters, are on" not in html
    assert "Add a tender" not in html
    assert "Scrape portal" not in html


def test_scrape_and_enrich_and_mail_stay_on_their_pages(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    with session(engine(path)) as current:
        vendor_id = upsert_vendor(current, name_raw="Waiting Works")
        mark_enriched(current, vendor_id, email="wait@example.com", phone=None)
        upsert_tender(current, tender_id="T-1", title="Wait", scraped_at=utcnow())
        replace_awards(
            current,
            "T-1",
            [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": "Waiting Works"}],
        )
    monkeypatch.setattr("ops_ui.routes.jobs.run_scrape", lambda log, stop, **params: 0)
    monkeypatch.setattr("ops_ui.routes.jobs.run_enrich", lambda log, stop, **params: 0)
    monkeypatch.setattr("ops_ui.routes.entry.run_enrich", lambda log, stop, **params: 0)
    monkeypatch.setattr("ops_ui.routes.mail.run_outreach", lambda log, stop, **params: 0)
    client = TestClient(create_app(path))

    scrape = client.post("/jobs/scrape", data={"search": "awarded"}, follow_redirects=False)
    assert scrape.status_code == 303
    assert scrape.headers["location"].startswith("/scrape?job=")

    enrich = client.post(
        "/jobs/enrich",
        data={"limit": "1", "delay": "0", "source": "scrape", "dry_run": "on"},
        follow_redirects=False,
    )
    assert enrich.headers["location"].startswith("/enrich?job=")
    enrich_page = client.get(enrich.headers["location"]).text
    assert "Nothing was looked up." in enrich_page
    assert 'name="enrichment_status"' not in enrich_page

    lookup = client.post(
        "/selection/lookup",
        data={"vendor_id": str(vendor_id), "dry_run": "on"},
        follow_redirects=False,
    )
    assert lookup.headers["location"].startswith("/enrich?job=")

    sent = client.post(
        "/mail",
        data={"mode": "preview", "delay": "5", "award": f"{vendor_id}:T-1"},
        follow_redirects=False,
    )
    assert sent.headers["location"].startswith("/mail?job=")
    mail_page = client.get(sent.headers["location"]).text
    assert ">T-1</a>" in mail_page
    assert "Send state" not in mail_page
    job_id = job_id_from(sent.headers["location"])
    assert f'href="/mail/export.xlsx?job={job_id}"' in mail_page


def test_the_start_page_points_at_the_last_run(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    monkeypatch.setattr("ops_ui.routes.jobs.run_scrape", lambda log, stop, **params: 0)
    client = TestClient(create_app(path))

    started = client.post("/jobs/scrape", data={"search": "awarded"}, follow_redirects=False)
    assert started.status_code == 303
    job_id = job_id_from(started.headers["location"])
    assert started.cookies.get("scrape_job") == str(job_id)

    again = client.get("/scrape", follow_redirects=False)
    assert again.status_code == 200
    assert f'href="/scrape?job={job_id}"' in again.text
    assert "→ Run" in again.text
    assert "Add a tender" in again.text

    grid = client.get(f"/scrape?job={job_id}")
    assert 'id="back"' in grid.text
    assert str(path) not in grid.text


def test_activity_run_shows_the_rows_that_run_wrote(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    bind = engine(path)
    ensure_schema(bind)
    with session(bind) as current:
        scrape_id = _job(current, "scrape", log="saved the tender")
        winner = upsert_vendor(current, name_raw="Fetched Ltd")
        upsert_tender(
            current,
            tender_id="FETCHED",
            title="Fetched tender",
            scraped_at=utcnow(),
            scrape_job_id=scrape_id,
        )
        replace_awards(
            current,
            "FETCHED",
            [{"bid_number": "1", "vendor_id": winner, "bidder_name": "Fetched Ltd"}],
        )
        status_id = _job(current, "status", log="funnel")
        mail_id = _job(
            current,
            "outreach",
            params='{"awards": ["%d:FETCHED"]}' % winner,
            log="sent the letter",
        )
    client = TestClient(create_app(path))

    scraped = client.get(f"/jobs/{scrape_id}").text
    assert ">← Back</a>" in scraped
    assert ">FETCHED</a>" in scraped
    assert "saved the tender" in scraped

    mailed = client.get(f"/jobs/{mail_id}").text
    assert ">FETCHED</a>" in mailed
    assert "sent the letter" in mailed

    status = client.get(f"/jobs/{status_id}").text
    assert "This run has no table." in status
    assert ">FETCHED</a>" not in status


def _two_tenders_by_one_run(path) -> tuple[int, int, int, int]:
    from pipeline_core.queries import record_job_tender

    bind = engine(path)
    ensure_schema(bind)
    with session(bind) as current:
        scrape_id = _job(current, "scrape", log="saved two tenders")
        winner = upsert_vendor(current, name_raw="Two Tender Ltd")
        for tender_id in ("T-A", "T-B"):
            upsert_tender(
                current,
                tender_id=tender_id,
                title=f"Work {tender_id}",
                scraped_at=utcnow(),
                scrape_job_id=scrape_id,
            )
            replace_awards(
                current,
                tender_id,
                [{"bid_number": "1", "vendor_id": winner, "bidder_name": "Two Tender Ltd"}],
            )
            record_job_tender(current, scrape_id, tender_id)
        pdf_id = _job(current, "pdfs", params='{"tender_id": "T-B"}', log="read the PDFs")
        record_job_tender(current, pdf_id, "T-B")
        mail_id = _job(current, "outreach", params='{"awards": ["%d:T-A"]}' % winner)
    return winner, scrape_id, pdf_id, mail_id


def test_a_pdf_download_run_shows_the_tender_it_read(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _winner, _scrape_id, pdf_id, _mail_id = _two_tenders_by_one_run(path)

    html = TestClient(create_app(path)).get(f"/jobs/{pdf_id}").text
    assert "read the PDFs" in html
    assert ">T-B</a>" in html
    assert ">T-A</a>" not in html
    assert "This run has no table." not in html


def test_a_run_opened_from_the_award_page_shows_that_record(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    winner, scrape_id, _pdf_id, _mail_id = _two_tenders_by_one_run(path)
    client = TestClient(create_app(path))

    whole = client.get(f"/jobs/{scrape_id}").text
    assert ">T-A</a>" in whole and ">T-B</a>" in whole
    assert 'id="focus-note"' not in whole

    focused = client.get(f"/jobs/{scrape_id}?award={winner}:T-A").text
    assert ">T-A</a>" in focused
    assert ">T-B</a>" not in focused
    assert f'<a href="/jobs/{scrape_id}">Show every row of this run</a>' in focused
    assert "saved two tenders" in focused


def test_a_focus_on_an_award_the_run_never_touched_shows_no_row(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    winner, _scrape_id, _pdf_id, mail_id = _two_tenders_by_one_run(path)
    client = TestClient(create_app(path))

    named = client.get(f"/jobs/{mail_id}?award={winner}:T-A").text
    assert ">T-A</a>" in named
    other = client.get(f"/jobs/{mail_id}?award={winner}:T-B").text
    assert ">T-A</a>" not in other and ">T-B</a>" not in other
    junk = client.get(f"/jobs/{mail_id}?award=not-a-pair").text
    assert ">T-A</a>" in junk
