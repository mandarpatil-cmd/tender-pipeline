"""Starting a run and reading its log."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from ops_ui.deps import database_path
from ops_ui.jobs import (
    JOB_STAGES,
    JOB_STATES,
    Busy,
    JobQuery,
    captcha_image,
    get_job,
    job_query_from,
    list_jobs,
    pdf_zip_path,
    request_stop,
    start_job,
    submit_captcha,
)
from ops_ui.queries import load_home
from ops_ui.runs import (
    date_error,
    enrich_cost,
    run_enrich,
    run_fetch_pdfs,
    run_scrape,
    run_status,
    scrape_cost,
    status_cost,
)
from ops_ui.templating import TEMPLATES

router = APIRouter()


@router.get("/jobs")
def jobs_page(request: Request, path: Path = Depends(database_path)):
    snapshot = load_home(path)
    query = job_query_from(request.query_params)
    listing = list_jobs(path, query) if snapshot.ready else None
    waiting = ""
    if snapshot.ready:
        waiting = enrich_cost(path, limit=1, source="scrape", dry_run=True)
    return TEMPLATES.TemplateResponse(
        request=request,
        name="jobs.html",
        context={
            "snapshot": snapshot,
            "jobs": listing.rows if listing else [],
            "listing": listing,
            "job_query": query,
            "stages": JOB_STAGES,
            "states": JOB_STATES,
            "previous": _jobs_href(query, page=listing.page - 1) if listing and listing.page > 1 else None,
            "following": (
                _jobs_href(query, page=listing.page + 1)
                if listing and listing.page < listing.pages
                else None
            ),
            "status_cost": status_cost(),
            "enrich_cost_text": waiting,
            "scrape_cost_text": scrape_cost(max_tenders=1, probe=False),
        },
    )


@router.get("/jobs/enrich-cost")
def enrich_cost_line(
    limit: int = 1,
    source: str = "scrape",
    dry_run: str = "",
    path: Path = Depends(database_path),
):
    if not load_home(path).ready:
        return Response("", media_type="text/plain")
    text = enrich_cost(
        path,
        limit=max(1, limit),
        source=None if source == "all" else source,
        dry_run=dry_run in {"on", "true", "1"},
    )
    return Response(text, media_type="text/plain")


@router.post("/jobs/status")
def post_status(path: Path = Depends(database_path)):
    return _start(path, "status", {}, lambda log, stop: run_status(log, stop, path))


@router.post("/jobs/enrich")
async def post_enrich(request: Request, path: Path = Depends(database_path)):
    form = await request.form()
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


@router.post("/jobs/scrape")
async def post_scrape(request: Request, path: Path = Depends(database_path)):
    form = await request.form()
    uncapped = form.get("no_cap") in {"on", "true", "1"}
    from_date = str(form.get("from_date") or "")
    to_date = str(form.get("to_date") or "")
    date_field = str(form.get("date_field") or "contract")
    if uncapped:
        if not from_date.strip() or not to_date.strip():
            return Response(
                "Set a from date and a to date before fetching without a cap.",
                status_code=400,
                media_type="text/plain",
            )
        error = date_error(from_date, to_date, date_field)
        if error:
            return Response(error, status_code=400, media_type="text/plain")
        max_tenders = None
        max_pages = None
        try:
            delay = float(form.get("delay") or 1.5)
        except ValueError:
            return Response("Delay must be a number.", status_code=400)
    else:
        try:
            max_tenders = _at_least(form.get("max_tenders"), 1)
            max_pages = _at_least(form.get("max_pages"), 1)
            delay = float(form.get("delay") or 1.5)
        except ValueError:
            return Response(
                "Tenders and pages must be whole numbers of at least 1.",
                status_code=400,
            )
    probe = form.get("probe") in {"on", "true", "1"}
    params = {
        "max_tenders": max_tenders,
        "max_pages": max_pages,
        "delay": max(delay, 1.5),
        "download_pdfs": form.get("download_pdfs") in {"on", "true", "1"},
        "refresh": form.get("refresh") in {"on", "true", "1"},
        "probe": probe,
        "from_date": from_date,
        "to_date": to_date,
        "date_field": date_field,
    }

    def work(log, stop):
        return run_scrape(log, stop, **params)

    return _start(path, "scrape", params, work)


@router.post("/jobs/pdfs")
async def post_pdfs(request: Request, path: Path = Depends(database_path)):
    form = await request.form()
    tender_id = str(form.get("tender_id") or "").strip()
    if not tender_id:
        return Response("A tender id is required.", status_code=400, media_type="text/plain")
    params = {"tender_id": tender_id}

    def work(log, stop):
        return run_fetch_pdfs(log, stop, tender_id=tender_id)

    return _start(path, "pdfs", params, work)


@router.get("/jobs/{job_id}")
def job_page(request: Request, job_id: int, refused: int = 0, path: Path = Depends(database_path)):
    job = get_job(path, job_id)
    if job is None:
        return Response("No run with that id.", status_code=404)
    return TEMPLATES.TemplateResponse(
        request=request,
        name="job.html",
        context=_job_view(job, refused=bool(refused)),
    )


@router.get("/jobs/{job_id}/live")
def job_live(request: Request, job_id: int, path: Path = Depends(database_path)):
    job = get_job(path, job_id)
    if job is None:
        return Response("No run with that id.", status_code=404)
    return TEMPLATES.TemplateResponse(
        request=request,
        name="job_live.html",
        context=_job_view(job),
    )


@router.get("/jobs/{job_id}/pdfs")
def job_pdfs(job_id: int, path: Path = Depends(database_path)):
    if get_job(path, job_id) is None:
        return Response("No run with that id.", status_code=404)
    archive = pdf_zip_path(job_id)
    if not archive.is_file():
        return Response("No PDFs were saved for this run.", status_code=404)
    return FileResponse(archive, filename=f"tender-{job_id}.zip")


@router.get("/jobs/{job_id}/captcha")
def captcha_png(job_id: int):
    image = captcha_image(job_id)
    if image is None:
        return Response("No captcha is waiting.", status_code=404)
    return FileResponse(image)


@router.post("/jobs/{job_id}/captcha")
async def post_captcha(request: Request, job_id: int):
    form = await request.form()
    if not submit_captcha(job_id, str(form.get("code") or "")):
        return Response("Type exactly 6 characters.", status_code=400)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/jobs/{job_id}/stop")
def post_stop(job_id: int, path: Path = Depends(database_path)):
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


def _job_view(job, **extra) -> dict:
    return {"job": job, "zip_ready": pdf_zip_path(job.job_id).is_file(), **extra}


def _jobs_href(query: JobQuery, *, page: int) -> str:
    params: dict[str, str] = {"page": str(page)}
    if query.stage:
        params["stage"] = query.stage
    if query.state:
        params["state"] = query.state
    if query.started_from:
        params["started_from"] = query.started_from
    if query.started_to:
        params["started_to"] = query.started_to
    return "/jobs?" + urlencode(params)


def _at_least(raw, default: int) -> int:
    text = str(raw or "").strip()
    value = default if not text else int(text)
    if value < 1:
        raise ValueError(value)
    return value
