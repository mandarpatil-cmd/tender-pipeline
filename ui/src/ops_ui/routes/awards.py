"""Home counts, the stage tables, one award, and the Excel and CSV downloads."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pipeline_core.db import engine, session
from pipeline_core.detail import award_detail
from pipeline_core.grid import COLUMNS, FILE_COLUMNS, PAGE_SIZE, award_view, choices

from ops_ui.deps import database_path
from ops_ui.evidence import fill_evidence, read_documents
from ops_ui.export import csv_bytes, xlsx_bytes
from ops_ui.jobs import active_jobs
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
    query_from,
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


def stage_context(request: Request, path: Path, stage: str) -> dict:
    """Everything a stage table renders. Mail uses the same dict."""
    query = stage_query(stage, request.query_params)
    stage_path = STAGE_PATHS[stage]
    snapshot, view, picked = load_view(path, query, page_size=PAGE_SIZE)
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
    can_enrich = stage == "enrich" and status != "done"
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
        "excel_href": export_href(query, "xlsx", path=stage_path),
        "csv_href": export_href(query, "csv", path=stage_path),
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
        "enrich_cost_text": "",
        "organisations": organisation_choices() if stage == "scrape" else [],
    }


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
    snapshot = load_home(path)
    running = active_jobs(path) if snapshot.ready else []
    return TEMPLATES.TemplateResponse(
        request=request,
        name="home.html",
        context={
            "snapshot": snapshot,
            "running": running,
            "status_cost": status_cost() if snapshot.ready else "",
        },
    )


def _stage_page(request: Request, path: Path, stage: str):
    context = stage_context(request, path, stage)
    if stage == "enrich" and context["snapshot"].ready:
        context["enrich_cost_text"] = enrich_cost(path, limit=1, source="scrape", dry_run=True)
    response = TEMPLATES.TemplateResponse(
        request=request,
        name="stage.html",
        context=context,
    )
    if context["snapshot"].ready:
        remember(response, stage, context["query"])
    return response


@router.get("/scrape")
def scrape_page(request: Request, path: Path = Depends(database_path)):
    return _stage_page(request, path, "scrape")


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
    if snapshot.ready:
        with session(engine(path)) as current:
            detail = award_detail(current, tender_id, bid)
            if detail is not None:
                documents = read_documents(current, tender_id)
    status = 200 if detail is not None or not snapshot.ready else 404
    return TEMPLATES.TemplateResponse(
        request=request,
        name="detail.html",
        status_code=status,
        context={
            "snapshot": snapshot,
            "detail": detail,
            "documents": documents,
            "back": back_href(back),
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
    query = query_from(request.query_params)
    snapshot, view, _picked = load_view(path, query, page_size=None)
    if not snapshot.ready or view is None:
        return Response(snapshot.message, status_code=404, media_type="text/plain")
    return _file(view.rows, kind, "awards", None)


def _stage_export(stage: str, path: Path, request: Request, kind: str) -> Response:
    if stage not in STAGE_PATHS:
        return Response("No such export.", status_code=404, media_type="text/plain")
    query = stage_query(stage, request.query_params)
    snapshot, view, _picked = load_view(path, query, page_size=None)
    if not snapshot.ready or view is None:
        return Response(snapshot.message, status_code=404, media_type="text/plain")
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
