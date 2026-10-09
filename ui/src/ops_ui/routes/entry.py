"""Hand-entered tenders and contacts, and a lookup of the ticked rows."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.manual import NeedsAttach, WinnerInput, save_manual_tender
from pipeline_core.queries import (
    ContactError,
    classify_selection,
    set_typed_contact,
)

from ops_ui.deps import database_path
from ops_ui.jobs import Busy, start_job
from ops_ui.present import enrich_plan
from ops_ui.queries import load_home
from ops_ui.runs import enrich_cost_line, run_enrich
from ops_ui.selection import selected_vendor_ids
from ops_ui.templating import TEMPLATES

router = APIRouter()


@router.get("/tender/new")
def new_tender(request: Request):
    path = database_path(request)
    snapshot = load_home(path)
    return TEMPLATES.TemplateResponse(
        request=request,
        name="tender_new.html",
        context={"snapshot": snapshot, "error": ""},
    )


@router.post("/tender/new")
async def post_tender(request: Request):
    return await _save_tender(request, attach=False)


@router.post("/tender/new/attach")
async def post_attach(request: Request):
    return await _save_tender(request, attach=True)


@router.post("/vendor/{vendor_id}/contact")
async def post_contact(request: Request, vendor_id: int):
    path = database_path(request)
    snapshot = load_home(path)
    if not snapshot.ready:
        return Response(snapshot.message, status_code=404, media_type="text/plain")
    form = await request.form()
    back = str(form.get("back") or "/")
    ensure_schema(engine(path))
    try:
        with session(engine(path)) as current:
            set_typed_contact(
                current,
                vendor_id,
                email=str(form.get("email") or ""),
                phone=str(form.get("phone") or ""),
            )
    except ContactError as exc:
        return Response(str(exc), status_code=400, media_type="text/plain")
    return RedirectResponse(back, status_code=303)


@router.post("/selection")
async def selection(request: Request):
    path = database_path(request)
    snapshot = load_home(path)
    if not snapshot.ready:
        return Response(snapshot.message, status_code=404, media_type="text/plain")
    form = await request.form()
    ids = selected_vendor_ids(form, path)
    if not ids:
        message = (
            "No companies match this filter."
            if str(form.get("select_all") or "")
            else "Tick at least one row."
        )
        return Response(message, status_code=400, media_type="text/plain")
    ensure_schema(engine(path))
    with session(engine(path)) as current:
        queue = classify_selection(current, ids)
    looked_up = len(queue.waiting_ids)
    return TEMPLATES.TemplateResponse(
        request=request,
        name="selection.html",
        context={
            "ids": list(queue.waiting_ids),
            "names": list(queue.names),
            "queue": queue,
            "skipped_done": queue.done,
            "skipped": list(queue.skipped),
            "plan": enrich_plan(queue.pending, queue.not_found, queue.failed, queue.not_found_ready),
            "cost": enrich_cost_line(looked_up, dry_run=True),
            "live_cost": enrich_cost_line(looked_up, dry_run=False),
        },
    )


@router.post("/selection/lookup")
async def selection_lookup(request: Request, path: Path = Depends(database_path)):
    form = await request.form()
    ids = _ids(form.getlist("vendor_id"))
    dry_run = form.get("dry_run") in {"on", "true", "1"}
    if not ids:
        return Response("No companies are waiting in that selection.", status_code=400)
    params = {"vendor_ids": ids, "dry_run": dry_run, "limit": len(ids)}

    def work(log, stop):
        return run_enrich(
            log,
            stop,
            database=path,
            limit=len(ids),
            delay=1.0,
            dry_run=dry_run,
            source=None,
            retry_failed=False,
            retry_not_found=False,
            vendor_ids=ids,
        )

    snapshot = load_home(path)
    if not snapshot.ready:
        return Response(snapshot.message, status_code=404, media_type="text/plain")
    try:
        job_id = start_job(path, "enrich", params, work)
    except Busy as exc:
        return RedirectResponse(f"/jobs/{exc.job_id}?refused=1", status_code=303)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


async def _save_tender(request: Request, *, attach: bool):
    path = database_path(request)
    snapshot = load_home(path)
    if not snapshot.ready:
        return Response(snapshot.message, status_code=404, media_type="text/plain")
    form = await request.form()
    winners = _winners(form)
    fields = {
        "tender_id": str(form.get("tender_id") or ""),
        "title": str(form.get("title") or ""),
        "organisation": str(form.get("organisation") or ""),
        "status": str(form.get("status") or ""),
        "contract_date": str(form.get("contract_date") or ""),
        "contract_value": str(form.get("contract_value") or ""),
    }
    ensure_schema(engine(path))
    try:
        with session(engine(path)) as current:
            tender_id = save_manual_tender(current, winners=winners, attach=attach, **fields)
    except NeedsAttach as exc:
        return TEMPLATES.TemplateResponse(
            request=request,
            name="tender_attach.html",
            context={
                "snapshot": snapshot,
                "matches": exc.matches,
                "fields": fields,
                "winners": winners,
            },
        )
    except ValueError as exc:
        return TEMPLATES.TemplateResponse(
            request=request,
            name="tender_new.html",
            status_code=400,
            context={"snapshot": snapshot, "error": str(exc)},
        )
    return RedirectResponse(f"/scrape?q={tender_id}", status_code=303)


def _ids(values) -> list[int]:
    ids = []
    for value in values:
        text = str(value).strip()
        if text.isdigit():
            ids.append(int(text))
    return ids


def _winners(form) -> list[WinnerInput]:
    names = form.getlist("winner_name")
    bids = form.getlist("bid_number")
    ranks = form.getlist("rank")
    quoted = form.getlist("quoted_value")
    awarded = form.getlist("awarded_value")
    currency = form.getlist("awarded_currency")
    titles = form.getlist("work_title")

    def at(items, index: int) -> str:
        return str(items[index]) if index < len(items) else ""

    winners = []
    for index, name in enumerate(names):
        if not str(name).strip():
            continue
        winners.append(
            WinnerInput(
                name=str(name),
                bid_number=at(bids, index),
                rank=at(ranks, index),
                quoted_value=at(quoted, index),
                awarded_value=at(awarded, index),
                awarded_currency=at(currency, index),
                work_title=at(titles, index),
            )
        )
    return winners
