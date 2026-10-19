"""Home counts, the stage tables, one award, and the Excel and CSV downloads."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pipeline_core.db import engine, session
from pipeline_core.detail import award_detail
from pipeline_core.grid import (
    COLUMNS,
    FILE_COLUMNS,
    PAGE_SIZE,
    award_view,
    choices,
    filtered_award_keys,
)
from pipeline_core.queries import apply_mail_addresses, mail_messages
from stage1_scrape.config import SEARCH_PAGE_URL, TENDER_STATUS_LABELS

from ops_ui.deps import database_path
from ops_ui.evidence import fill_evidence, read_documents
from ops_ui.export import csv_bytes, xlsx_bytes
from ops_ui.jobs import active_jobs
from ops_ui.jobs import captcha_image, get_job, pdf_zip_path
from ops_ui.present import (
    DATE_PRESETS,
    STAGE_COLUMNS,
    STAGE_PATHS,
    active_preset,
    back_href,
    export_href,
    filter_token,
    groups,
    headers,
    href,
    job_query,
    shown_day,
    stage_form_fields,
    stage_query,
)
from ops_ui.queries import load_home
from ops_ui.runs import enrich_cost, organisation_choices, status_cost
from ops_ui.templating import TEMPLATES

router = APIRouter()

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_COOKIE = 60 * 60 * 24 * 30
_PAGE_STAGE = {"scrape": "scrape", "enrich": "enrich", "mail": "outreach"}
_STAGE_PAGE = {"scrape": "scrape", "enrich": "enrich", "outreach": "mail"}
_SEARCH_LABELS = {
    "awarded": "Awarded (AOC)",
    "organisation": "One organisation",
    "tender_id": "One tender id (Download PDFs)",
}


def load_view(path: Path, query, *, page_size: int | None):
    snapshot = load_home(path)
    if not snapshot.ready:
        return snapshot, None, {}
    with session(engine(path)) as current:
        view = award_view(current, query, page_size=page_size)
        picked = choices(current) if page_size is not None else {}
        if view is not None:
            fill_evidence(current, view.rows)
    return snapshot, view, picked


def stage_context(
    request: Request,
    path: Path,
    stage: str,
    *,
    query_override=None,
    job=None,
) -> dict:
    """Everything a stage table renders. Mail uses the same dict."""
    watching = job is not None
    query = query_override if query_override is not None else stage_query(stage, request.query_params)
    stage_path = STAGE_PATHS[stage]
    snapshot, view, picked = load_view(path, query, page_size=PAGE_SIZE)
    if stage == "mail" and view is not None:
        with session(engine(path)) as current:
            apply_mail_addresses(current, view.rows)
    keys = STAGE_COLUMNS[stage]
    columns = [
        {"key": key, "evidence": evidence}
        for key, _label, _group, evidence in COLUMNS
        if key in keys
    ]
    # Keep the stage order, not the master column order.
    by_key = {column["key"]: column for column in columns}
    columns = [by_key[key] for key in keys]
    column_headers = headers(query, path=stage_path, keys=keys)
    previous = None
    following = None
    if view is not None and view.page > 1:
        previous = href(query, path=stage_path, page=str(view.page - 1))
    if view is not None and view.page < view.pages:
        following = href(query, path=stage_path, page=str(view.page + 1))
    status = query.enrichment_status[0] if stage == "enrich" and query.enrichment_status else ""
    can_enrich = (stage == "enrich" and status != "done" and not watching) or (
        stage == "scrape" and watching
    )
    labels = {key: label for key, label, _group, _evidence in COLUMNS}
    return {
        "snapshot": snapshot,
        "view": view,
        "query": query,
        "choices": picked,
        "stage": stage,
        "stage_path": stage_path,
        "columns": columns,
        "headers": column_headers,
        "groups": groups(column_headers),
        "previous": previous,
        "following": following,
        "excel_href": _job_export(stage_path, job, "xlsx")
        if watching
        else export_href(query, "xlsx", path=stage_path),
        "csv_href": _job_export(stage_path, job, "csv")
        if watching
        else export_href(query, "csv", path=stage_path),
        "form_fields": stage_form_fields(stage, query),
        "selectable": stage == "mail" or can_enrich,
        "can_enrich": can_enrich,
        "sort_columns": [(key, labels[key]) for key in keys if key not in FILE_COLUMNS],
        "today": date.today().isoformat(),
        "date_presets": DATE_PRESETS,
        "scraped_from": shown_day(query.scraped_from),
        "scraped_to": shown_day(query.scraped_to) or date.today().isoformat(),
        "scraped_from_preset": active_preset(query.scraped_from, query.scraped_to),
        "scraped_to_mode": "" if query.scraped_to else "any",
        "contract_from_preset": active_preset(query.date_from, query.date_to),
        "contract_to_mode": "" if query.date_to else "any",
        "enrich_cost_text": "",
        "organisations": organisation_choices() if stage == "scrape" else [],
        "letters": _letters(path, query) if stage == "mail" else 0,
        "advanced_open": _advanced_open(query),
        "job": job,
        "watching": watching,
        "refused": request.query_params.get("refused") == "1",
        "captcha_error": request.query_params.get("captcha") == "1",
        "empty_note": _empty_note(stage, job, view) if watching else "",
        "zip_ready": pdf_zip_path(job.job_id).is_file() if watching else False,
        "captcha_version": _captcha_version(job) if watching else 0,
        "fresh": request.query_params.get("fresh") == "1",
        "remembered_job": None,
    }


def _captcha_version(job) -> int:
    image = captcha_image(job.job_id)
    return int(image.stat().st_mtime_ns) if image is not None else 0


def _job_export(stage_path: str, job, suffix: str) -> str:
    base = "/export" if stage_path in {"", "/"} else f"{stage_path}/export"
    return f"{base}.{suffix}?job={job.job_id}"


def _empty_note(stage: str, job, view) -> str:
    if view is None or view.total:
        return ""
    if stage == "enrich":
        return "Nothing was looked up."
    if stage == "scrape":
        return "This run has not saved a tender."
    return "No awards match."


def watched_job(request: Request, path: Path, stage: str):
    """The run named by ``?job=``, and the rows that belong to it."""
    raw = (request.query_params.get("job") or "").strip()
    if not raw.isdigit():
        return None, None
    job = get_job(path, int(raw))
    if job is None:
        return None, None
    return job, job_query(stage, job.job_id, job.params_json)


def _advanced_open(query) -> bool:
    """Open the extra filters when one of them is already narrowing the rows."""
    return any(
        (
            query.organisation,
            query.value_min,
            query.value_max,
            query.pdf_contact,
            query.source,
            query.scraped_from,
            query.scraped_to,
            query.date_from,
            query.date_to,
        )
    )


def _letters(path: Path, query) -> int:
    """Letters ``mail_messages`` would send for the awards in this filter."""
    with session(engine(path)) as current:
        keys = filtered_award_keys(current, query)
        if not keys:
            return 0
        return len(mail_messages(current, awards=keys))


def remember_run(response: Response, page: str, job_id: int) -> None:
    """Keep the latest run for a stage so the nav can open it again."""
    response.set_cookie(
        f"{page}_job",
        str(job_id),
        max_age=_COOKIE,
        httponly=True,
        samesite="lax",
        path="/",
    )


def remember_job_stage(response: Response, stage: str, job_id: int) -> None:
    """``stage`` is the job row (``outreach`` for mail), not the page path."""
    page = _STAGE_PAGE.get(stage)
    if page:
        remember_run(response, page, job_id)


def remembered_run(request: Request, path: Path, page: str) -> int | None:
    raw = (request.cookies.get(f"{page}_job") or "").strip()
    if not raw.isdigit():
        return None
    job = get_job(path, int(raw))
    if job is None or job.stage != _PAGE_STAGE[page]:
        return None
    return job.job_id


def remember(response: Response, stage: str, query) -> None:
    token = filter_token(query, path=STAGE_PATHS[stage])
    name = f"{stage}_query"
    if token:
        response.set_cookie(
            name,
            token,
            max_age=_COOKIE,
            httponly=True,
            samesite="lax",
            path="/",
        )
    else:
        response.delete_cookie(name, path="/")


@router.get("/")
def home(request: Request, path: Path = Depends(database_path)):
    context = stage_context(request, path, "home")
    ready = context["snapshot"].ready
    context["running"] = active_jobs(path) if ready else []
    context["status_cost"] = status_cost() if ready else ""
    response = TEMPLATES.TemplateResponse(
        request=request,
        name="home.html",
        context=context,
    )
    if ready:
        remember(response, "home", context["query"])
    return response


def _stage_page(request: Request, path: Path, stage: str):
    job, override = watched_job(request, path, stage)
    context = stage_context(request, path, stage, query_override=override, job=job)
    if job is None:
        context["remembered_job"] = remembered_run(request, path, stage)
    if stage == "enrich" and context["snapshot"].ready:
        context["enrich_cost_text"] = enrich_cost(path, limit=1, source="scrape", dry_run=True)
    response = TEMPLATES.TemplateResponse(
        request=request,
        name="stage.html",
        context=context,
    )
    if job is not None:
        remember_run(response, stage, job.job_id)
    elif context["snapshot"].ready:
        remember(response, stage, context["query"])
    return response


@router.get("/scrape")
def scrape_page(request: Request, path: Path = Depends(database_path)):
    job, override = watched_job(request, path, "scrape")
    if job is None:
        snapshot = load_home(path)
        return TEMPLATES.TemplateResponse(
            request=request,
            name="stage.html",
            context={
                "snapshot": snapshot,
                "stage": "scrape",
                "stage_path": "/scrape",
                "today": date.today().isoformat(),
                "date_presets": DATE_PRESETS,
                "organisations": organisation_choices() if snapshot.ready else [],
                "job": None,
                "watching": False,
                "error": "",
                "refused": False,
                "captcha_error": False,
                "fresh": request.query_params.get("fresh") == "1",
                "remembered_job": remembered_run(request, path, "scrape"),
            },
        )
    context = stage_context(request, path, "scrape", query_override=override, job=job)
    context["organisations"] = (
        organisation_choices() if context["snapshot"].ready else []
    )
    context["error"] = ""
    response = TEMPLATES.TemplateResponse(request=request, name="stage.html", context=context)
    remember_run(response, "scrape", job.job_id)
    return response


@router.get("/enrich")
def enrich_page(request: Request, path: Path = Depends(database_path)):
    return _stage_page(request, path, "enrich")


@router.get("/award")
def award(
    request: Request,
    tender_id: str = "",
    bid: str = "",
    back: str = "",
    path: Path = Depends(database_path),
):
    snapshot = load_home(path)
    detail = None
    documents: list[dict[str, str]] = []
    contacts: list[dict] = []
    if snapshot.ready:
        with session(engine(path)) as current:
            detail = award_detail(current, tender_id, bid)
            if detail is not None:
                documents = read_documents(current, tender_id)
                from pipeline_core.models import LlmRun
                from pipeline_core.queries import ordered_contacts
                from sqlalchemy import select

                stored = [
                    *ordered_contacts(
                        current,
                        detail.vendor["vendor_id"],
                        tender_id,
                        channel="email",
                    ),
                    *ordered_contacts(
                        current,
                        detail.vendor["vendor_id"],
                        tender_id,
                        channel="phone",
                    ),
                ]
                run_ids = [row.llm_run_id for row in stored if row.llm_run_id]
                jobs = {}
                if run_ids:
                    jobs = {
                        int(run_id): job_id
                        for run_id, job_id in current.execute(
                            select(LlmRun.run_id, LlmRun.job_id).where(
                                LlmRun.run_id.in_(run_ids)
                            )
                        )
                    }
                contacts = [
                    {
                        "channel": row.channel,
                        "value": row.value,
                        "source": row.source,
                        "valid": bool(row.valid),
                        "source_url": row.source_url or "",
                        "job_id": jobs.get(row.llm_run_id),
                    }
                    for row in stored
                ]
    status = 200 if detail is not None or not snapshot.ready else 404
    return TEMPLATES.TemplateResponse(
        request=request,
        name="detail.html",
        status_code=status,
        context={
            "snapshot": snapshot,
            "detail": detail,
            "documents": documents,
            "contacts": contacts,
            "email_count": sum(1 for item in contacts if item["channel"] == "email"),
            "back": back_href(back),
            "portal_search_url": SEARCH_PAGE_URL,
            "search_labels": _SEARCH_LABELS,
            "status_labels": TENDER_STATUS_LABELS,
            "save_error": "",
        },
    )


@router.get("/export.xlsx")
def export_xlsx(request: Request, path: Path = Depends(database_path)):
    return _export(path, request, "xlsx")


@router.get("/export.csv")
def export_csv(request: Request, path: Path = Depends(database_path)):
    return _export(path, request, "csv")


@router.get("/{stage}/export.xlsx")
def stage_export_xlsx(stage: str, request: Request, path: Path = Depends(database_path)):
    return _stage_export(stage, path, request, "xlsx")


@router.get("/{stage}/export.csv")
def stage_export_csv(stage: str, request: Request, path: Path = Depends(database_path)):
    return _stage_export(stage, path, request, "csv")


def _export(path: Path, request: Request, kind: str) -> Response:
    query = stage_query("home", request.query_params)
    snapshot, view, _picked = load_view(path, query, page_size=None)
    if not snapshot.ready or view is None:
        return Response(snapshot.message, status_code=404, media_type="text/plain")
    return _file(view.rows, kind, "awards", STAGE_COLUMNS["home"])


def _stage_export(stage: str, path: Path, request: Request, kind: str) -> Response:
    if stage not in STAGE_PATHS or stage == "home":
        return Response("No such export.", status_code=404, media_type="text/plain")
    job, override = watched_job(request, path, stage)
    query = override if override is not None else stage_query(stage, request.query_params)
    snapshot, view, _picked = load_view(path, query, page_size=None)
    if not snapshot.ready or view is None:
        return Response(snapshot.message, status_code=404, media_type="text/plain")
    if stage == "mail":
        with session(engine(path)) as current:
            apply_mail_addresses(current, view.rows)
    return _file(view.rows, kind, stage, STAGE_COLUMNS[stage])


def _file(rows, kind: str, name: str, columns) -> Response:
    if kind == "csv":
        body = csv_bytes(rows, columns)
        media = "text/csv; charset=utf-8"
        filename = f"{name}.csv"
    else:
        body = xlsx_bytes(rows, columns, sheet_title=name)
        media = _XLSX
        filename = f"{name}.xlsx"
    return Response(
        body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
