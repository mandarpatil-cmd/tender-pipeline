"""The operations window: the award table, runs, and hand-entered records."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from pipeline_core import settings

from ops_ui.jobs import close_orphaned_jobs
from ops_ui.routes.awards import router as awards_router
from ops_ui.routes.entry import router as entry_router
from ops_ui.routes.jobs import router as jobs_router
from ops_ui.routes.mail import router as mail_router

#: This machine only. The window is not a shared server.
HOST = "127.0.0.1"
PORT = 8000

_STATIC = Path(__file__).resolve().parent / "static"
_LOCAL_ORIGIN_HOSTS = {"127.0.0.1", "localhost"}


def _cross_site_post(request: Request) -> bool:
    """A browser form posted from another site. A missing Origin is allowed."""
    if request.method != "POST":
        return False
    path = request.url.path
    if not (path == "/mail" or path.startswith("/mail/") or path == "/jobs" or path.startswith("/jobs/")):
        return False
    origin = request.headers.get("origin")
    if not origin:
        return False
    host = (urlparse(origin).hostname or "").lower()
    return host not in _LOCAL_ORIGIN_HOSTS


def create_app(database: Path | None = None) -> FastAPI:
    """`database` overrides the shared file. Tests pass a throwaway path."""
    app = FastAPI(title="Tender pipeline", docs_url=None, redoc_url=None)
    app.state.database = database

    @app.middleware("http")
    async def reject_cross_site_posts(request: Request, call_next):
        if _cross_site_post(request):
            return Response(
                "This form can only be sent from this computer.",
                status_code=403,
                media_type="text/plain",
            )
        return await call_next(request)

    app.include_router(awards_router)
    app.include_router(jobs_router)
    app.include_router(entry_router)
    app.include_router(mail_router)
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
    return app


app = create_app()


def main() -> None:
    close_orphaned_jobs(settings.db_path())
    uvicorn.run(app, host=HOST, port=PORT)
