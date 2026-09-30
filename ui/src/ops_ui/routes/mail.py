"""Preview and send outreach. A live send is never the default."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from pipeline_core.db import engine, session
from pipeline_core.emailcheck import EMAIL_RE
from pipeline_core.queries import outreach_targets

from ops_ui.deps import database_path
from ops_ui.jobs import Busy, start_job
from ops_ui.queries import load_home
from ops_ui.runs import credential_gap, live_send_refusal, load_campaign, run_outreach
from ops_ui.selection import selected_vendor_ids
from ops_ui.templating import TEMPLATES

router = APIRouter()

_ON = {"1", "on", "true", "yes"}
_DELAY_FLOOR = 5.0


@router.get("/mail")
def mail_page(request: Request, path: Path = Depends(database_path)):
    return _render(request, path, vendor_ids=None, error="")


@router.post("/mail/from-table")
async def mail_from_table(request: Request):
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
    return _render(request, path, vendor_ids=ids, error="")


@router.post("/mail")
async def post_mail(request: Request, path: Path = Depends(database_path)):
    snapshot = load_home(path)
    if not snapshot.ready:
        return Response(snapshot.message, status_code=404, media_type="text/plain")
    form = await request.form()
    mode = str(form.get("mode") or "preview")
    transport = str(form.get("transport") or "gmail")
    if transport not in {"gmail", "graph"}:
        transport = "gmail"
    try:
        delay = float(form.get("delay") or _DELAY_FLOOR)
    except ValueError:
        delay = _DELAY_FLOOR
    delay = max(delay, _DELAY_FLOOR)
    redirect_to = str(form.get("redirect_to") or "").strip()
    vendor_ids = _ids(form.getlist("vendor_id"))
    chosen = vendor_ids or None
    preflight = mode == "preflight"
    send = mode == "send" and str(form.get("send") or "") in _ON
    if mode == "send" and not send:
        return _render(
            request,
            path,
            vendor_ids=chosen,
            error="Tick Send for real to mail companies. Preview writes files and sends nothing.",
            transport=transport,
            delay=delay,
            redirect_to=redirect_to,
        )
    if preflight:
        problem = credential_gap(transport, preflight=True)
    elif send:
        problem = live_send_refusal(transport)
    else:
        problem = None
    if problem:
        return _render(
            request,
            path,
            vendor_ids=chosen,
            error=problem,
            transport=transport,
            delay=delay,
            redirect_to=redirect_to,
        )
    params = {
        "vendor_ids": chosen,
        "send": send,
        "preflight": preflight,
        "transport": transport,
        "delay": delay,
        "redirect_to": redirect_to,
    }

    def work(log, stop):
        return run_outreach(
            log,
            stop,
            database=path,
            vendor_ids=chosen,
            send=send,
            preflight=preflight,
            transport=transport,
            delay=delay,
            redirect_to=redirect_to,
        )

    try:
        job_id = start_job(path, "outreach", params, work)
    except Busy as exc:
        return RedirectResponse(f"/jobs/{exc.job_id}?refused=1", status_code=303)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


def _render(
    request: Request,
    path: Path,
    *,
    vendor_ids: list[int] | None,
    error: str,
    transport: str = "gmail",
    delay: float = _DELAY_FLOOR,
    redirect_to: str = "",
):
    snapshot = load_home(path)
    waiting = 0
    mailable: list = []
    subject = ""
    body = ""
    if snapshot.ready:
        campaign = load_campaign()
        subject = campaign.SUBJECT_TEMPLATE
        body = campaign.build_body("the company")
        with session(engine(path)) as current:
            queue = [
                person
                for person in outreach_targets(current)
                if EMAIL_RE.match(person.email or "")
            ]
        waiting = len(queue)
        if vendor_ids is None:
            mailable = queue
        else:
            wanted = set(vendor_ids)
            mailable = [person for person in queue if person.vendor_id in wanted]
    return TEMPLATES.TemplateResponse(
        request=request,
        name="mail.html",
        context={
            "snapshot": snapshot,
            "error": error,
            "waiting": waiting,
            "selected": vendor_ids,
            "mailable": mailable,
            "subject": subject,
            "body": body,
            "transport": transport,
            "delay": delay,
            "redirect_to": redirect_to,
        },
    )


def _ids(values) -> list[int]:
    ids = []
    for value in values:
        text = str(value).strip()
        if text.isdigit():
            ids.append(int(text))
    return ids
