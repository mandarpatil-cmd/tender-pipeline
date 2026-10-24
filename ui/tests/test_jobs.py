"""Starting a run, refusing a second one, and a contact dry run."""

from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient
from pipeline_core.db import engine, ensure_schema
from pipeline_core.models import Vendor
from pipeline_core.queries import mark_failed, upsert_vendor

from ops_ui.app import create_app
from ops_ui.present import job_id_from, job_page
from ops_ui.jobs import Busy, captcha_reading, get_job, open_captcha, start_job, wants_typed
from pipeline_core.db import session


def test_status_run_writes_the_log(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    client = TestClient(create_app(path))

    response = client.post("/jobs/status", follow_redirects=False)

    assert response.status_code == 303
    job_id = job_id_from(response.headers["location"])
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
    job_id = job_id_from(response.headers["location"])
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


def test_enrich_page_names_the_limit_and_the_delay(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    client = TestClient(create_app(path))
    html = client.get("/enrich").text

    assert "<h2>Look up contacts</h2>" not in html
    assert "Find contacts" not in html
    assert "Limit is how many waiting companies" not in html
    assert 'value="lookup">Enrich</button>' in html
    assert "Put failed companies back on the queue" not in html
    scrape = client.get("/scrape").text
    assert "60 days before to" in scrape
    assert 'name="contract_to"' in scrape
    assert 'name="published_to"' in scrape
    assert 'name="search"' in scrape
    assert 'name="organ_name"' in scrape
    assert "One organisation" in scrape
    assert "Award of Contract" in scrape
    assert "Add a tender" in scrape
    assert "fetch and store each one again" in scrape
    assert 'name="date_field"' not in scrape
    assert 'type="date"' in scrape
    runs = client.get("/jobs").text
    assert "Scrape portal" not in runs
    assert "<h2>Look up contacts</h2>" not in runs
    assert "Put failed companies back on the queue" not in runs


def test_enrich_dry_run_does_not_requeue(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    with session(engine(path)) as current:
        failed = upsert_vendor(current, name_raw="Failed Works")
        known = upsert_vendor(current, name_raw="Known Absence")
        mark_failed(current, failed)
        vendor = current.get(Vendor, known)
        vendor.enrichment_status = "not_found"
        vendor.email = "known@example.com"
    client = TestClient(create_app(path))
    response = client.post(
        "/jobs/enrich",
        data={
            "limit": "1",
            "delay": "0",
            "source": "scrape",
            "dry_run": "on",
            "retry_failed": "on",
            "retry_not_found": "on",
        },
        follow_redirects=False,
    )
    job_id = job_id_from(response.headers["location"])
    text = ""
    for _ in range(50):
        page = client.get(f"/jobs/{job_id}")
        if "Dry run" in page.text and "Would put" in page.text:
            text = page.text
            break
        threading.Event().wait(0.05)

    assert "Would put 1 failed company back on the queue." in text
    assert "Would put 0 not_found companies back on the queue." in text
    with session(engine(path)) as current:
        assert current.get(Vendor, failed).enrichment_status == "failed"
        assert current.get(Vendor, known).enrichment_status == "not_found"


def test_typed_captcha_reaches_the_same_run(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    image = tmp_path / "captcha.png"
    image.write_bytes(b"\x89PNG\r\n")
    started = threading.Event()
    received: list[str] = []

    def work(log, stop):
        from ops_ui.jobs import wait_for_captcha

        started.set()
        received.append(wait_for_captcha(log, stop, image))
        log.write("continued")
        return 0

    job_id = start_job(path, "scrape", {}, work)
    assert started.wait(2)
    client = TestClient(create_app(path))
    page = ""
    for _ in range(50):
        page = client.get(f"/jobs/{job_id}").text
        if "6 characters" in page:
            break
        threading.Event().wait(0.05)
    assert "6 characters" in page
    wait = open_captcha(job_id)
    assert wait is not None and not wait.retry
    assert f'id="captcha-box-{wait.token}"' in page
    assert 'hx-preserve="true"' in page
    assert 'id="captcha-code"' in page
    assert 'class="captcha-code"' in page
    assert f'name="token" value="{wait.token}"' in page
    assert "autofocus" not in page
    assert "maxlength" not in page
    assert client.get(f"/jobs/{job_id}/captcha").content.startswith(b"\x89PNG")
    short = client.post(
        f"/jobs/{job_id}/captcha",
        data={"token": str(wait.token), "code": "ab"},
        follow_redirects=False,
    )
    assert short.status_code == 303
    assert short.headers["location"].endswith(f"/scrape?job={job_id}&captcha=short")
    assert "Type all 6 characters" in client.get(short.headers["location"]).text
    posted = client.post(
        f"/jobs/{job_id}/captcha",
        data={"token": str(wait.token), "code": " Ab12 Xy "},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert "captcha=" not in posted.headers["location"]
    assert open_captcha(job_id) is None
    assert client.get(f"/jobs/{job_id}/captcha").status_code == 404
    job = None
    for _ in range(50):
        job = get_job(path, job_id)
        if job is not None and job.state == "done":
            break
        threading.Event().wait(0.05)
    assert received == ["Ab12Xy"]
    assert job is not None and job.state == "done"
    assert "continued" in (job.log or "")


def test_captcha_wait_ends_when_nobody_types(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    image = tmp_path / "captcha.png"
    image.write_bytes(b"\x89PNG\r\n")
    monkeypatch.setattr("ops_ui.jobs.CAPTCHA_TIMEOUT_SECONDS", 0.05)

    def work(log, stop):
        from ops_ui.jobs import CaptchaTimedOut, wait_for_captcha

        try:
            wait_for_captcha(log, stop, image)
        except CaptchaTimedOut as exc:
            log.write(str(exc))
            return 1
        return 0

    job_id = start_job(path, "scrape", {}, work)
    job = None
    for _ in range(50):
        job = get_job(path, job_id)
        if job is not None and job.state == "failed":
            break
        threading.Event().wait(0.05)
    assert job is not None
    assert job.state == "failed"
    assert "Nobody typed the captcha in time" in (job.log or "")

    client = TestClient(create_app(path))
    late = client.post(
        f"/jobs/{job_id}/captcha",
        data={"token": "1", "code": "Ab12Xy"},
        follow_redirects=False,
    )
    assert late.status_code == 303
    assert late.headers["location"].endswith(f"/scrape?job={job_id}&captcha=closed")
    page = client.get(late.headers["location"]).text
    assert "no longer waiting" in page
    assert 'id="captcha-box-' not in page


def test_a_rejected_code_brings_a_new_box(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    image = tmp_path / "captcha.png"
    image.write_bytes(b"\x89PNG\r\n")
    received: list[str] = []

    def work(log, stop):
        from ops_ui.jobs import wait_for_captcha

        received.append(wait_for_captcha(log, stop, image))
        received.append(wait_for_captcha(log, stop, image))
        return 0

    def next_wait(after: int):
        for _ in range(50):
            wait = open_captcha(job_id)
            if wait is not None and wait.token != after:
                return wait
            threading.Event().wait(0.05)
        raise AssertionError("no new captcha wait")

    job_id = start_job(path, "scrape", {}, work)
    client = TestClient(create_app(path))
    first = next_wait(0)
    client.post(
        f"/jobs/{job_id}/captcha",
        data={"token": str(first.token), "code": "Wrong1"},
        follow_redirects=False,
    )
    second = next_wait(first.token)
    assert second.retry
    page = ""
    for _ in range(50):
        page = client.get(f"/jobs/{job_id}/live").text
        if f'id="captcha-box-{second.token}"' in page:
            break
        threading.Event().wait(0.05)
    assert f'id="captcha-box-{second.token}"' in page
    assert f"captcha?v={second.token}" in page
    assert "not accepted" in page

    again = client.post(
        f"/jobs/{job_id}/captcha",
        data={"token": str(first.token), "code": "Wrong1"},
        follow_redirects=False,
    )
    assert "captcha=" not in again.headers["location"]
    assert open_captcha(job_id) is second

    client.post(
        f"/jobs/{job_id}/captcha",
        data={"token": str(second.token), "code": "Right2"},
        follow_redirects=False,
    )
    for _ in range(50):
        if len(received) == 2:
            break
        threading.Event().wait(0.05)
    assert received == ["Wrong1", "Right2"]


def test_type_it_myself_opens_the_box_before_the_reads_run_out(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    image = tmp_path / "captcha.png"
    image.write_bytes(b"\x89PNG\r\n")
    received: list[str] = []

    def work(log, stop):
        from ops_ui.jobs import begin_reading, wait_for_captcha

        begin_reading(log.job_id)
        for _ in range(100):
            if wants_typed(log.job_id):
                break
            threading.Event().wait(0.05)
        received.append(wait_for_captcha(log, stop, image))
        return 0

    job_id = start_job(path, "scrape", {}, work)
    client = TestClient(create_app(path))
    live = ""
    for _ in range(50):
        live = client.get(f"/jobs/{job_id}/live").text
        if "Type the captcha myself" in live:
            break
        threading.Event().wait(0.05)
    assert "Type the captcha myself" in live
    assert f'action="/jobs/{job_id}/captcha/typed"' in live

    pressed = client.post(f"/jobs/{job_id}/captcha/typed", follow_redirects=False)
    assert pressed.status_code == 303
    assert pressed.headers["location"].endswith(f"/scrape?job={job_id}")
    for _ in range(50):
        live = client.get(f"/jobs/{job_id}/live").text
        if 'id="captcha-box-' in live:
            break
        threading.Event().wait(0.05)
    assert 'id="captcha-box-' in live
    assert "Type the captcha myself" not in live
    assert captcha_reading(job_id) == ""

    wait = open_captcha(job_id)
    assert wait is not None
    client.post(
        f"/jobs/{job_id}/captcha",
        data={"token": str(wait.token), "code": "Ab12Xy"},
        follow_redirects=False,
    )
    for _ in range(50):
        if received:
            break
        threading.Event().wait(0.05)
    assert received == ["Ab12Xy"]


def test_type_it_myself_after_the_captcha_step_does_nothing(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    job_id = start_job(path, "scrape", {}, lambda log, stop: 0)
    client = TestClient(create_app(path))

    pressed = client.post(f"/jobs/{job_id}/captcha/typed", follow_redirects=False)

    assert pressed.status_code == 303
    assert not wants_typed(job_id)
    assert captcha_reading(job_id) == ""
    assert client.post("/jobs/999/captcha/typed").status_code == 404


def test_a_pdf_run_keeps_the_captcha_notice():
    assert job_page("pdfs", 7) == "/jobs/7"
    assert job_page("pdfs", 7, captcha="short") == "/jobs/7?captcha=short"
    assert job_page("pdfs", 7, refused=True) == "/jobs/7?refused=1"


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


def test_stop_finishes_a_run_with_no_worker(tmp_path):
    from pipeline_core.models import UiJob
    from pipeline_core.queries import utcnow

    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    with session(engine(path)) as current:
        job = UiJob(
            stage="scrape",
            state="running",
            operator="local",
            log="OpenRouter OCR\n",
            started_at=utcnow(),
        )
        current.add(job)
        current.flush()
        job_id = int(job.job_id)

    client = TestClient(create_app(path))
    response = client.post(f"/jobs/{job_id}/stop", follow_redirects=False)

    assert response.status_code == 303
    stopped = get_job(path, job_id)
    assert stopped is not None
    assert stopped.state == "stopped"
    assert stopped.finished_at
    assert "No worker was running" in (stopped.log or "")

    def work(log, stop):
        log.write("started")
        return 0

    new_id = start_job(path, "scrape", {}, work)
    assert new_id != job_id
    state = ""
    for _ in range(50):
        later = get_job(path, new_id)
        state = later.state if later is not None else ""
        if state == "done":
            break
        threading.Event().wait(0.05)
    assert state == "done"


def test_startup_closes_runs_left_by_a_dead_process(tmp_path):
    from pipeline_core.models import UiJob
    from pipeline_core.queries import utcnow

    from ops_ui.jobs import close_orphaned_jobs

    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    with session(engine(path)) as current:
        waiting = UiJob(
            stage="enrich",
            state="waiting",
            operator="local",
            log="ocr\n",
            started_at=utcnow(),
        )
        finished = UiJob(
            stage="scrape",
            state="done",
            operator="local",
            log="kept\n",
            started_at=utcnow(),
        )
        current.add(waiting)
        current.add(finished)
        current.flush()
        waiting_id = int(waiting.job_id)
        finished_id = int(finished.job_id)

    assert close_orphaned_jobs(path) == 1
    closed = get_job(path, waiting_id)
    kept = get_job(path, finished_id)
    assert closed is not None and closed.state == "stopped"
    assert closed.finished_at
    assert "window restarted" in (closed.log or "")
    assert kept is not None and kept.state == "done"
    assert "window restarted" not in (kept.log or "")


def test_the_log_stays_outside_the_live_fragment(tmp_path):
    from pipeline_core.models import UiJob
    from pipeline_core.queries import utcnow

    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    text = "one\ntwo\n"
    with session(engine(path)) as current:
        job = UiJob(
            stage="enrich",
            state="running",
            operator="local",
            log=text,
            started_at=utcnow(),
        )
        current.add(job)
        current.flush()
        job_id = int(job.job_id)

    client = TestClient(create_app(path))
    page = client.get(f"/jobs/{job_id}")
    assert page.status_code == 200
    assert 'id="job-log"' in page.text
    assert page.text.index('id="live"') < page.text.index('id="job-log"')

    live = client.get(f"/jobs/{job_id}/live")
    assert live.status_code == 200
    assert 'id="job-log"' not in live.text
    assert "<pre" not in live.text
    assert "one" not in live.text

    chunk = client.get(f"/jobs/{job_id}/log", params={"offset": 4})
    assert chunk.status_code == 200
    assert chunk.text == "two\n"
    assert chunk.headers["x-log-next"] == str(len(text))
    assert chunk.headers["x-job-state"] == "running"


def test_no_cap_requires_both_dates(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    monkeypatch.setattr("ops_ui.routes.jobs.organisation_choices", _organisations)
    client = TestClient(create_app(path))

    missing = client.post(
        "/jobs/scrape",
        data={"no_cap": "on", "contract_from": "01/01/2026"},
    )
    reversed_dates = client.post(
        "/jobs/scrape",
        data={"no_cap": "on", "contract_from": "31/03/2026", "contract_to": "01/01/2026"},
    )
    published = client.post(
        "/jobs/scrape",
        data={"no_cap": "on", "search": "organisation", "organ_name": "534"},
    )

    assert missing.status_code == 400
    assert missing.text == "Set a contract from and to before fetching without a cap."
    assert reversed_dates.status_code == 400
    assert reversed_dates.text == "Contract from date is after to date."
    assert published.status_code == 400
    assert published.text == "Set a published from and to before fetching without a cap."


def test_no_cap_stores_unlimited_bounds(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))

    def fake(log, stop, **params):
        log.write("listed")
        return 0

    monkeypatch.setattr("ops_ui.routes.jobs.run_scrape", fake)
    client = TestClient(create_app(path))
    response = client.post(
        "/jobs/scrape",
        data={
            "no_cap": "on",
            "search": "awarded",
            "contract_from": "01/01/2026",
            "contract_to": "31/01/2026",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    job_id = job_id_from(response.headers["location"])
    job = get_job(path, job_id)
    assert job is not None
    params = json.loads(job.params_json)
    assert params["max_tenders"] is None
    assert params["max_pages"] is None
    assert params["search"] == "awarded"
    assert params["fromDate"] == "01/01/2026"
    assert params["toDate"] == "31/01/2026"
    assert params["publishedFromDate"] == ""
    assert params["publishedToDate"] == ""
    assert params["organ_name"] == ""


def test_scrape_preset_is_measured_from_the_to_date(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))

    def fake(log, stop, **params):
        log.write("listed")
        return 0

    monkeypatch.setattr("ops_ui.routes.jobs.run_scrape", fake)
    client = TestClient(create_app(path))
    response = client.post(
        "/jobs/scrape",
        data={
            "no_cap": "on",
            "contract_to": "2026-10-07",
            "contract_from_preset": "60",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    job_id = job_id_from(response.headers["location"])
    job = get_job(path, job_id)
    params = json.loads(job.params_json)
    assert params["fromDate"] == "08/08/2026"
    assert params["toDate"] == "07/10/2026"
    assert params["publishedFromDate"] == ""
    assert params["publishedToDate"] == ""


def test_runs_list_pages_and_filters(tmp_path):
    from pipeline_core.models import UiJob

    path = tmp_path / "pipeline.sqlite3"
    bind = engine(path)
    ensure_schema(bind)
    with session(bind) as current:
        for index in range(51):
            current.add(
                UiJob(
                    stage="enrich" if index == 50 else "scrape",
                    state="failed" if index % 2 else "done",
                    operator="local",
                    log="",
                    started_at=f"2026-01-{(index % 28) + 1:02d}T00:00:00+00:00",
                )
            )
    client = TestClient(create_app(path))

    first = client.get("/jobs")
    assert first.status_code == 200
    assert "Page 1 of 2" in first.text
    assert "No runs yet." not in first.text

    second = client.get("/jobs?page=2")
    assert "Page 2 of 2" in second.text
    assert second.text.count('href="/jobs/') < first.text.count('href="/jobs/')

    enrich = client.get("/jobs?stage=enrich")
    assert "No runs match." not in enrich.text
    assert "enrich</td>" in enrich.text
    assert "Page 1 of 2" not in enrich.text

    missing = client.get("/jobs?stage=pdfs")
    assert "No runs match." in missing.text

    window = client.get("/jobs?started_from=2026-01-20&started_to=2026-01-22").text
    assert "2026-01-01" not in window
    assert "2026-01-20" in window

    empty = TestClient(create_app(tmp_path / "other.sqlite3"))
    ensure_schema(engine(tmp_path / "other.sqlite3"))
    assert "No runs yet." in empty.get("/jobs").text


def test_refresh_reruns_stage_one_and_contract_dates_stay_contract(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    seen = {}

    def fake(out, **kwargs):
        seen.update(kwargs)
        return {"scraped": 1, "vendors": 1}

    monkeypatch.setattr("stage1_scrape.app.run.run_scrape", fake)
    client = TestClient(create_app(path))
    response = client.post(
        "/jobs/scrape",
        data={
            "refresh": "on",
            "contract_from": "01/01/2026",
            "contract_to": "31/01/2026",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = job_id_from(response.headers["location"])
    for _ in range(50):
        if seen:
            break
        threading.Event().wait(0.05)
    assert seen["skip_known"] is False
    assert seen["only_aoc"] is False
    assert seen["extra_fields"] == {
        "tenderStatus": "6",
        "fromDate": "01/01/2026",
        "toDate": "31/01/2026",
        "OrganName": "0",
        "publishedFromDate": "",
        "publishedToDate": "",
        "tenderId": "",
    }
    job = get_job(path, job_id)
    assert job is not None and job.state == "done"


def test_a_ui_scrape_tells_stage_one_its_search_and_run(tmp_path, monkeypatch):
    """Every tender this run saves is stamped with the search and the run id."""
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    seen = {}

    def fake(out, **kwargs):
        seen.update(kwargs)
        return {"scraped": 0, "vendors": 0}

    monkeypatch.setattr("stage1_scrape.app.run.run_scrape", fake)
    client = TestClient(create_app(path))
    response = client.post("/jobs/scrape", data={"search": "awarded"}, follow_redirects=False)

    assert response.status_code == 303
    job_id = job_id_from(response.headers["location"])
    for _ in range(50):
        if seen:
            break
        threading.Event().wait(0.05)
    assert seen["search_mode"] == "awarded"
    assert seen["job_id"] == job_id
    assert seen["prefer_typed"]() is False
    assert callable(seen["searched"])


def test_an_enrich_run_tells_stage_two_its_job(tmp_path, monkeypatch):
    from pipeline_core.db import session
    from pipeline_core.queries import upsert_vendor

    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    with session(engine(path)) as current:
        upsert_vendor(current, name_raw="Waiting Ltd")
    seen = {}

    class Summary:
        def render(self) -> str:
            return "1 vendor(s) attempted: 0 done, 0 not found, 0 failed"

    def fake(**kwargs):
        seen.update(kwargs)
        return Summary()

    monkeypatch.setattr("stage2_enrich.runner.enrich_pending", fake)
    monkeypatch.setattr("stage2_enrich.settings.require_env", lambda: None)
    monkeypatch.setattr("stage2_enrich.settings.model", lambda: "test-model")
    client = TestClient(create_app(path))
    response = client.post(
        "/jobs/enrich",
        data={"limit": "1", "delay": "0", "source": "scrape"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    job_id = job_id_from(response.headers["location"])
    for _ in range(50):
        if seen:
            break
        threading.Event().wait(0.05)
    assert seen["job_id"] == job_id


def test_download_pdfs_tells_stage_one_its_run(tmp_path, monkeypatch):
    from ops_ui.runs import run_fetch_pdfs

    seen = {}

    def fake(out, tender_id, **kwargs):
        seen.update(kwargs, tender_id=tender_id)
        return {"ok": True, "stopped": False, "message": "Saved clues."}

    class Log:
        job_id = 12
        database = tmp_path / "pipeline.sqlite3"

        def __init__(self) -> None:
            self.lines: list[str] = []

        def write(self, message: str) -> None:
            self.lines.append(message)

    monkeypatch.setattr("stage1_scrape.app.pipeline.fetch_tender_pdfs", fake)
    log = Log()

    assert run_fetch_pdfs(log, threading.Event(), tender_id="2026_X_1") == 0
    assert seen["tender_id"] == "2026_X_1"
    assert seen["job_id"] == 12
    assert seen["prefer_typed"]() is False
    assert callable(seen["searched"])
    assert captcha_reading(12) == ""
    assert "Saved clues." in log.lines


def test_download_pdfs_starts_a_pdf_job(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))

    def fake(log, stop, *, tender_id):
        log.write(tender_id)
        return 0

    monkeypatch.setattr("ops_ui.routes.jobs.run_fetch_pdfs", fake)
    client = TestClient(create_app(path))
    response = client.post(
        "/jobs/pdfs",
        data={"tender_id": "2026_ORG_1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    job_id = job_id_from(response.headers["location"])
    job = get_job(path, job_id)
    assert job is not None
    assert job.stage == "pdfs"
    assert json.loads(job.params_json)["tender_id"] == "2026_ORG_1"


def _organisations():
    return [("534", "Ministry of Road Transport and Highways")]


def test_a_mixed_scrape_post_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    monkeypatch.setattr("ops_ui.routes.jobs.organisation_choices", _organisations)
    client = TestClient(create_app(path))

    awarded = client.post(
        "/jobs/scrape",
        data={
            "search": "awarded",
            "contract_from": "01/01/2026",
            "contract_to": "31/01/2026",
            "published_from": "01/02/2026",
            "published_to": "28/02/2026",
            "organ_name": "534",
        },
    )
    organisation = client.post(
        "/jobs/scrape",
        data={
            "search": "organisation",
            "organ_name": "534",
            "contract_from": "01/01/2026",
            "contract_to": "31/01/2026",
        },
    )

    assert awarded.status_code == 400
    assert awarded.text == "An awarded search cannot also send an organisation or published dates."
    assert organisation.status_code == 400
    assert organisation.text == "An organisation search cannot also send contract dates."


def test_organisation_names_come_from_the_saved_form(tmp_path):
    from ops_ui.runs import organisation_choices

    form = tmp_path / "search_form.html"
    form.write_text(
        """
        <select name="OrganName">
          <option value="0">-Select-</option>
          <option value="605">National Highways and Infrastructure Development Corporation</option>
        </select>
        """,
        encoding="utf-8",
    )
    assert organisation_choices(form) == [
        ("605", "National Highways and Infrastructure Development Corporation")
    ]
    assert organisation_choices(tmp_path / "missing.html") == []


def test_organisation_search_posts_only_that_group(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    seen = {}

    def fake(out, **kwargs):
        seen.update(kwargs)
        return {"scraped": 1, "vendors": 1}

    monkeypatch.setattr("ops_ui.routes.jobs.organisation_choices", _organisations)
    monkeypatch.setattr("stage1_scrape.app.run.run_scrape", fake)
    client = TestClient(create_app(path))
    response = client.post(
        "/jobs/scrape",
        data={
            "search": "organisation",
            "organ_name": "534",
            "published_from": "01/02/2026",
            "published_to": "28/02/2026",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    for _ in range(50):
        if seen:
            break
        threading.Event().wait(0.05)
    assert seen["only_aoc"] is True
    assert seen["extra_fields"] == {
        "tenderStatus": "0",
        "OrganName": "534",
        "publishedFromDate": "01/02/2026",
        "publishedToDate": "28/02/2026",
        "fromDate": "",
        "toDate": "",
        "tenderId": "",
        "KeyWord": "",
    }
    job = get_job(path, job_id_from(response.headers["location"]))
    assert seen["search_mode"] == "organisation"
    assert seen["job_id"] == job.job_id
    params = json.loads(job.params_json)
    assert params["organ_label"] == "Ministry of Road Transport and Highways"
    assert params["fromDate"] == ""
    assert params["toDate"] == ""


def test_unknown_organisation_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    monkeypatch.setattr("ops_ui.routes.jobs.organisation_choices", _organisations)
    client = TestClient(create_app(path))
    response = client.post(
        "/jobs/scrape",
        data={"search": "organisation", "organ_name": "999"},
    )
    assert response.status_code == 400
    assert response.text == "Choose an organisation from the portal list."


def test_missing_organisation_list_is_named_on_the_form(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    monkeypatch.setattr("ops_ui.routes.awards.organisation_choices", lambda: [])
    client = TestClient(create_app(path))
    html = client.get("/scrape").text
    assert "Organisation names are not loaded" in html
    assert 'value="534"' not in html
