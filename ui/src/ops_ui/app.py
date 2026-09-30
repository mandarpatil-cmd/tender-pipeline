"""The operations window: the award table, runs, and hand-entered records."""

from __future__ import annotations

from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ops_ui.routes.awards import router as awards_router
from ops_ui.routes.entry import router as entry_router
from ops_ui.routes.jobs import router as jobs_router
from ops_ui.routes.mail import router as mail_router

#: This machine only. The window is not a shared server.
HOST = "127.0.0.1"
PORT = 8000

_STATIC = Path(__file__).resolve().parent / "static"


def create_app(database: Path | None = None) -> FastAPI:
    """`database` overrides the shared file. Tests pass a throwaway path."""
    app = FastAPI(title="Tender pipeline", docs_url=None, redoc_url=None)
    app.state.database = database
    app.include_router(awards_router)
    app.include_router(jobs_router)
    app.include_router(entry_router)
    app.include_router(mail_router)
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
    return app


app = create_app()


def main() -> None:
    uvicorn.run(app, host=HOST, port=PORT)
