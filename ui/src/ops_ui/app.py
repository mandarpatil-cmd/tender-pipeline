"""The operations window: counts, one row per award, and an export of that view."""

from __future__ import annotations

from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pipeline_core import settings
from pipeline_core.db import engine, session
from pipeline_core.detail import award_detail
from pipeline_core.grid import COLUMNS, PAGE_SIZE, award_view, choices

from ops_ui.evidence import fill_evidence, read_documents
from ops_ui.export import csv_bytes, xlsx_bytes
from ops_ui.present import (
    back_href,
    export_href,
    groups,
    headers,
    href,
    query_from,
    strip_cards,
)
from ops_ui.queries import load_home
from ops_ui.job_pages import mount_jobs

#: This machine only. The window is not a shared server.
HOST = "127.0.0.1"
PORT = 8000

_HERE = Path(__file__).resolve().parent
_TEMPLATES = _HERE / "templates"
_STATIC = _HERE / "static"

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def create_app(database: Path | None = None) -> FastAPI:
    """`database` overrides the shared file. Tests pass a throwaway path."""
    app = FastAPI(title="Tender pipeline", docs_url=None, redoc_url=None)
    app.state.database = database
    templates = Jinja2Templates(directory=str(_TEMPLATES))

    def database_path() -> Path:
        if app.state.database is not None:
            return app.state.database
        return settings.db_path()

    def load_view(path: Path, query, *, page_size: int | None):
        snapshot = load_home(path)
        if not snapshot.ready:
            return snapshot, None, {}
        with session(engine(path)) as current:
            view = award_view(current, query, page_size=page_size)
            picked = choices(current) if page_size is not None else {}
        fill_evidence(view.rows, settings.project_root())
        return snapshot, view, picked

    @app.get("/")
    def home(request: Request):
        path = database_path()
        query = query_from(request.query_params)
        snapshot, view, picked = load_view(path, query, page_size=PAGE_SIZE)
        return templates.TemplateResponse(
            request=request,
            name="home.html",
            context=_context(path, query, snapshot, view, picked),
        )

    @app.get("/award")
    def award(request: Request, tender_id: str = "", bid: str = "", back: str = ""):
        path = database_path()
        snapshot = load_home(path)
        detail = None
        documents: list[dict[str, str]] = []
        if snapshot.ready:
            with session(engine(path)) as current:
                detail = award_detail(current, tender_id, bid)
            if detail is not None:
                documents = read_documents(
                    detail.tender.get("json_path") or "",
                    settings.project_root(),
                )
        status = 200 if detail is not None or not snapshot.ready else 404
        return templates.TemplateResponse(
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

    @app.get("/export.xlsx")
    def export_xlsx(request: Request):
        return _export(database_path(), request, "xlsx")

    @app.get("/export.csv")
    def export_csv(request: Request):
        return _export(database_path(), request, "csv")

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

    mount_jobs(app, templates, database_path)
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
    return app


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
    }


app = create_app()


def main() -> None:
    uvicorn.run(app, host=HOST, port=PORT)
