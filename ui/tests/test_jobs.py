"""Starting a run, refusing a second one, and a contact dry run."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from pipeline_core.db import engine, ensure_schema
from pipeline_core.queries import upsert_vendor

from ops_ui.app import create_app
from ops_ui.jobs import Busy, get_job, start_job
from pipeline_core.db import session


def test_status_run_writes_the_log(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    client = TestClient(create_app(path))

    response = client.post("/jobs/status", follow_redirects=False)

    assert response.status_code == 303
    job_id = int(response.headers["location"].rsplit("/", 1)[-1])
    log = ""
    for _ in range(50):
        page = client.get(f"/jobs/{job_id}")
        if "done" in page.text and "funnel" in page.text:
            log = page.text
            break
        threading.Event().wait(0.05)
    assert "funnel" in log
    assert "tenders" in log


def test_a_second_run_of_the_same_stage_is_refused(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    started = threading.Event()
    release = threading.Event()

    def work(log, stop):
        started.set()
        release.wait(2)
        log.write("finished")
        return 0

    job_id = start_job(path, "status", {}, work)
    assert started.wait(2)
    try:
        with pytest.raises(Busy) as caught:
            start_job(path, "status", {}, work)
        assert caught.value.job_id == job_id
    finally:
        release.set()
        for _ in range(50):
            job = get_job(path, job_id)
            if job is not None and job.state != "running":
                break
            threading.Event().wait(0.05)


def test_enrich_dry_run_lists_the_company_and_does_not_spend(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    with session(engine(path)) as current:
        upsert_vendor(current, name_raw="Waiting Works")
    client = TestClient(create_app(path))

    response = client.post(
        "/jobs/enrich",
        data={"limit": "1", "delay": "0", "source": "scrape", "dry_run": "on"},
        follow_redirects=False,
    )
    job_id = int(response.headers["location"].rsplit("/", 1)[-1])
    text = ""
    for _ in range(50):
        page = client.get(f"/jobs/{job_id}")
        if "Dry run" in page.text:
            text = page.text
            break
        threading.Event().wait(0.05)

    assert "Waiting Works" in text
    assert "Dry run" in text
    with session(engine(path)) as current:
        from pipeline_core.models import Vendor

        vendor = current.query(Vendor).one()
        assert vendor.enrichment_status == "pending"


def test_stop_marks_the_run_stopped(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    started = threading.Event()

    def work(log, stop):
        started.set()
        while not stop.is_set():
            stop.wait(0.05)
        log.write("saw stop")
        return 0

    job_id = start_job(path, "scrape", {}, work)
    assert started.wait(2)
    client = TestClient(create_app(path))
    client.post(f"/jobs/{job_id}/stop", follow_redirects=False)
    state = ""
    for _ in range(50):
        job = get_job(path, job_id)
        state = job.state if job is not None else ""
        if state == "stopped":
            break
        threading.Event().wait(0.05)
    assert state == "stopped"
