"""Run forms refuse bad numbers, keep safe floors, and the run's own pages answer."""

from __future__ import annotations

import threading
import zipfile

import pytest
from fastapi.testclient import TestClient
from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.models import UiJob

from ops_ui.app import create_app
from ops_ui.jobs import get_job, pdf_zip_path, start_job
from ops_ui.present import job_id_from


@pytest.fixture
def window(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    return TestClient(create_app(path)), path


def _capture(monkeypatch, name: str) -> list[dict]:
    runs: list[dict] = []

    def run(log, stop, **params):
        runs.append(params)
        return 0

    monkeypatch.setattr(f"ops_ui.routes.jobs.{name}", run)
    return runs


def _done(path, job_id: int) -> str:
    for _ in range(100):
        state = get_job(path, job_id).state
        if state in {"done", "failed", "stopped"}:
            return state
        threading.Event().wait(0.02)
    return get_job(path, job_id).state


@pytest.mark.parametrize("limit", ["0", "-3", "two", "1.5"])
def test_a_lookup_limit_that_is_not_a_whole_number_is_refused(window, monkeypatch, limit):
    client, path = window
    runs = _capture(monkeypatch, "run_enrich")

    answer = client.post("/jobs/enrich", data={"limit": limit, "dry_run": "on"})

    assert answer.status_code == 400
    assert answer.text == "Limit must be a whole number of at least 1."
    assert runs == []


def test_a_blank_lookup_limit_is_one_and_all_sources_is_no_filter(window, monkeypatch):
    client, path = window
    runs = _capture(monkeypatch, "run_enrich")

    started = client.post("/jobs/enrich", data={"limit": "", "source": "all"}, follow_redirects=False)
    _done(path, job_id_from(started.headers["location"]))

    assert runs[0]["limit"] == 1
    assert runs[0]["source"] is None
    assert runs[0]["dry_run"] is False


@pytest.mark.parametrize(("field", "value"), [("max_tenders", "0"), ("max_pages", "many")])
def test_scrape_counts_that_are_not_whole_numbers_are_refused(window, monkeypatch, field, value):
    client, _path = window
    runs = _capture(monkeypatch, "run_scrape")

    answer = client.post("/jobs/scrape", data={"search": "awarded", field: value})

    assert answer.status_code == 400
    assert answer.text == "Tenders and pages must be whole numbers of at least 1."
    assert runs == []


def test_a_scrape_keeps_its_delay_floor_and_passes_its_switches(window, monkeypatch):
    client, path = window
    runs = _capture(monkeypatch, "run_scrape")

    started = client.post(
        "/jobs/scrape",
        data={"search": "awarded", "delay": "0", "probe": "on", "download_pdfs": "on", "refresh": "on"},
        follow_redirects=False,
    )
    _done(path, job_id_from(started.headers["location"]))

    assert runs[0]["delay"] == 1.5
    assert runs[0]["probe"] is True
    assert runs[0]["download_pdfs"] is True
    assert runs[0]["refresh"] is True
    assert runs[0]["max_tenders"] == 1 and runs[0]["max_pages"] == 1


def test_a_scrape_by_default_fetches_one_tender_and_no_pdfs(window, monkeypatch):
    """FR-202: the form's own defaults are the cheap run."""
    client, path = window
    runs = _capture(monkeypatch, "run_scrape")

    started = client.post("/jobs/scrape", data={"search": "awarded"}, follow_redirects=False)
    _done(path, job_id_from(started.headers["location"]))

    assert runs[0]["max_tenders"] == 1
    assert runs[0]["probe"] is False and runs[0]["refresh"] is False


def test_a_pdf_fetch_needs_a_tender_id(window, monkeypatch):
    client, _path = window
    runs = _capture(monkeypatch, "run_fetch_pdfs")

    answer = client.post("/jobs/pdfs", data={"tender_id": "  "})

    assert answer.status_code == 400
    assert answer.text == "A tender id is required."
    assert runs == []


def test_a_second_status_run_is_refused_while_one_is_going(window):
    client, path = window
    release = threading.Event()
    first = start_job(path, "status", {}, lambda log, stop: release.wait(5) and 0)

    answer = client.post("/jobs/status", follow_redirects=False)
    release.set()

    assert answer.status_code == 303
    assert answer.headers["location"] == f"/jobs/{first}?refused=1"
    with session(engine(path)) as current:
        assert current.query(UiJob).count() == 1


def test_the_log_is_read_from_an_offset_and_never_cached(window):
    client, path = window
    job_id = start_job(path, "status", {}, lambda log, stop: log.write("one\ntwo") or 0)
    assert _done(path, job_id) == "done"
    text = get_job(path, job_id).log
    assert text == "one\ntwo\n"

    whole = client.get(f"/jobs/{job_id}/log?offset=-5")
    tail = client.get(f"/jobs/{job_id}/log?offset=4")
    past = client.get(f"/jobs/{job_id}/log?offset=100000")

    assert whole.text == text
    assert tail.text == text[4:]
    assert past.text == ""
    assert past.headers["x-log-next"] == str(len(text))
    assert whole.headers["x-job-state"] == "done"
    assert whole.headers["cache-control"] == "no-store"


def test_the_pdf_zip_downloads_once_it_exists(window):
    client, path = window
    job_id = start_job(path, "pdfs", {"tender_id": "T-1"}, lambda log, stop: 0)
    _done(path, job_id)

    assert client.get(f"/jobs/{job_id}/pdfs").status_code == 404

    archive = pdf_zip_path(job_id)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("work_order.pdf", b"%PDF-1.4")
    answer = client.get(f"/jobs/{job_id}/pdfs")

    assert answer.status_code == 200
    assert answer.headers["content-disposition"] == f'attachment; filename="tender-{job_id}.zip"'
    assert zipfile.ZipFile(__import__("io").BytesIO(answer.content)).namelist() == ["work_order.pdf"]


def test_stopping_a_finished_run_changes_nothing(window):
    client, path = window
    job_id = start_job(path, "status", {}, lambda log, stop: log.write("done") or 0)
    assert _done(path, job_id) == "done"
    before = get_job(path, job_id)

    answer = client.post(f"/jobs/{job_id}/stop", follow_redirects=False)

    assert answer.status_code == 303
    after = get_job(path, job_id)
    assert (after.state, after.log, after.exit_code) == (before.state, before.log, before.exit_code)


@pytest.mark.parametrize("url", ["/jobs/999/stop", "/jobs/999/captcha/typed"])
def test_a_run_that_does_not_exist_cannot_be_stopped_or_switched(window, url):
    client, _path = window

    assert client.post(url).status_code == 404


def test_a_code_for_a_run_that_does_not_exist_is_not_an_error(window):
    client, _path = window

    answer = client.post("/jobs/999/captcha", data={"token": "1", "code": "ABC123"}, follow_redirects=False)

    assert answer.status_code < 500
