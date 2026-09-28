"""Pages for starting a run and reading its log."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ops_ui.jobs import Busy, get_job, recent_jobs, request_stop, start_job
from ops_ui.queries import load_home
from ops_ui.runs import (
    enrich_cost,
    run_enrich,
    run_scrape,
    run_status,
    scrape_cost,
    status_cost,
)


def mount_jobs(app: FastAPI, templates: Jinja2Templates, database_path) -> None:
    @app.get("/jobs")
    def jobs_page(request: Request):
        path = database_path()
        snapshot = load_home(path)
        jobs = recent_jobs(path) if snapshot.ready else []
        waiting = ""
        if snapshot.ready:
            waiting = enrich_cost(path, limit=1, source="scrape", dry_run=True)
        return templates.TemplateResponse(
            request=request,
            name="jobs.html",
            context={
                "snapshot": snapshot,
                "jobs": jobs,
                "status_cost": status_cost(),
                "enrich_cost_text": waiting,
                "scrape_cost_text": scrape_cost(max_tenders=1, probe=False),
            },
        )

    @app.get("/jobs/enrich-cost")
    def enrich_cost_line(
        limit: int = 1,
        source: str = "scrape",
        dry_run: str = "",
    ):
        path = database_path()
        if not load_home(path).ready:
            return Response("", media_type="text/plain")
        text = enrich_cost(
            path,
            limit=max(1, limit),
            source=None if source == "all" else source,
            dry_run=dry_run in {"on", "true", "1"},
        )
        return Response(text, media_type="text/plain")

    @app.post("/jobs/status")
    def post_status():
        return _start(database_path(), "status", {}, lambda log, stop: run_status(log, stop, database_path()))

    @app.post("/jobs/enrich")
    async def post_enrich(request: Request):
        form = await request.form()
        path = database_path()
        try:
            limit = _at_least(form.get("limit"), 1)
            delay = float(form.get("delay") or 1)
        except ValueError:
            return Response("Limit must be a whole number of at least 1.", status_code=400)
        if delay < 0:
            delay = 0
        source_raw = str(form.get("source") or "scrape")
        source = None if source_raw == "all" else source_raw
        dry_run = form.get("dry_run") in {"on", "true", "1"}
        retry_failed = form.get("retry_failed") in {"on", "true", "1"}
        retry_not_found = form.get("retry_not_found") in {"on", "true", "1"}
        params = {
            "limit": limit,
            "delay": delay,
            "dry_run": dry_run,
            "source": source_raw,
            "retry_failed": retry_failed,
            "retry_not_found": retry_not_found,
        }

        def work(log, stop):
            return run_enrich(
                log,
                stop,
                database=path,
                limit=limit,
                delay=delay,
                dry_run=dry_run,
                source=source,
                retry_failed=retry_failed,
                retry_not_found=retry_not_found,
            )

        return _start(path, "enrich", params, work)

    @app.post("/jobs/scrape")
    async def post_scrape(request: Request):
        form = await request.form()
        path = database_path()
        try:
            max_tenders = _at_least(form.get("max_tenders"), 1)
            max_pages = _at_least(form.get("max_pages"), 1)
            delay = float(form.get("delay") or 1.5)
        except ValueError:
            return Response("Tenders and pages must be whole numbers of at least 1.", status_code=400)
        probe = form.get("probe") in {"on", "true", "1"}
        params = {
            "max_tenders": max_tenders,
            "max_pages": max_pages,
            "delay": max(delay, 1.5),
            "download_pdfs": form.get("download_pdfs") in {"on", "true", "1"},
            "refresh": form.get("refresh") in {"on", "true", "1"},
            "probe": probe,
            "from_date": str(form.get("from_date") or ""),
            "to_date": str(form.get("to_date") or ""),
            "date_field": str(form.get("date_field") or "contract"),
        }

        def work(log, stop):
            return run_scrape(log, stop, **params)

        return _start(path, "scrape", params, work)

    @app.get("/jobs/{job_id}")
    def job_page(request: Request, job_id: int, refused: int = 0):
        path = database_path()
        job = get_job(path, job_id)
        if job is None:
            return Response("No run with that id.", status_code=404)
        return templates.TemplateResponse(
            request=request,
            name="job.html",
            context={"job": job, "refused": bool(refused)},
        )

    @app.get("/jobs/{job_id}/live")
    def job_live(request: Request, job_id: int):
        job = get_job(database_path(), job_id)
        if job is None:
            return Response("No run with that id.", status_code=404)
        return templates.TemplateResponse(
            request=request,
            name="job_live.html",
            context={"job": job},
        )

    @app.post("/jobs/{job_id}/stop")
    def post_stop(job_id: int):
        path = database_path()
        if get_job(path, job_id) is None:
            return Response("No run with that id.", status_code=404)
        request_stop(path, job_id)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)


def _start(path, stage: str, params: dict, work) -> Response:
    snapshot = load_home(path)
    if not snapshot.ready:
        return Response(snapshot.message, status_code=404, media_type="text/plain")
    try:
        job_id = start_job(path, stage, params, work)
    except Busy as exc:
        return RedirectResponse(f"/jobs/{exc.job_id}?refused=1", status_code=303)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


def _at_least(raw, default: int) -> int:
    text = str(raw or "").strip()
    value = default if not text else int(text)
    if value < 1:
        raise ValueError(value)
    return value
