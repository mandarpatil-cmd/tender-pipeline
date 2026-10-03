"""The award table, one award, and the Excel and CSV downloads."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pipeline_core.db import engine, session
from pipeline_core.detail import award_detail
from pipeline_core.grid import COLUMNS, FILE_COLUMNS, PAGE_SIZE, award_view, choices

from ops_ui.deps import database_path
from ops_ui.evidence import fill_evidence, read_documents
from ops_ui.export import csv_bytes, xlsx_bytes
from ops_ui.present import (
    back_href,
    export_href,
    form_fields,
    groups,
    headers,
    href,
    query_from,
    strip_cards,
)
from ops_ui.queries import load_home
from ops_ui.templating import TEMPLATES

router = APIRouter()

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


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


def _context(path, query, snapshot, view, picked):
    filtered = query.is_filtered()
    columns = [
        {"key": key, "evidence": evidence}
        for key, _label, _group, evidence in COLUMNS
    ]
    previous = None
    following = None
    if view is not None and view.page > 1:
        previous = href(query, page=str(view.page - 1))
    if view is not None and view.page < view.pages:
        following = href(query, page=str(view.page + 1))
    column_headers = headers(query)
    return {
        "snapshot": snapshot,
        "view": view,
        "query": query,
        "choices": picked,
        "cards": strip_cards(snapshot, view, filtered),
        "filtered": filtered,
        "columns": columns,
        "headers": column_headers,
        "groups": groups(column_headers),
        "previous": previous,
        "following": following,
        "excel_href": export_href(query, "xlsx"),
        "csv_href": export_href(query, "csv"),
        "database_path": path,
        "form_fields": form_fields(query),
        "sort_columns": [
            (key, label) for key, label, _group, _evidence in COLUMNS if key not in FILE_COLUMNS
        ],
    }


@router.get("/")
def home(request: Request, path: Path = Depends(database_path)):
    query = query_from(request.query_params)
    snapshot, view, picked = load_view(path, query, page_size=PAGE_SIZE)
    return TEMPLATES.TemplateResponse(
        request=request,
        name="home.html",
        context=_context(path, query, snapshot, view, picked),
    )


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


def _export(path: Path, request: Request, kind: str) -> Response:
    query = query_from(request.query_params)
    snapshot, view, _picked = load_view(path, query, page_size=None)
    if not snapshot.ready or view is None:
        return Response(snapshot.message, status_code=404, media_type="text/plain")
    if kind == "csv":
        body = csv_bytes(view.rows)
        media = "text/csv; charset=utf-8"
        filename = "awards.csv"
    else:
        body = xlsx_bytes(view.rows)
        media = _XLSX
        filename = "awards.xlsx"
    return Response(
        body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
