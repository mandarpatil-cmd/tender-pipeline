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
from ops_ui.selection import selected_awards
from ops_ui.templating import TEMPLATES

router = APIRouter()

_ON = {"1", "on", "true", "yes"}
_DELAY_FLOOR = 5.0


@router.get("/mail")
def mail_page(request: Request, path: Path = Depends(database_path)):
    return _render(request, path, awards=None, error="")


@router.post("/mail/from-table")
async def mail_from_table(request: Request):
    path = database_path(request)
    snapshot = load_home(path)
    if not snapshot.ready:
        return Response(snapshot.message, status_code=404, media_type="text/plain")
    form = await request.form()
    awards = selected_awards(form, path)
    if not awards:
        message = (
            "No awards match this filter."
            if str(form.get("select_all") or "")
            else "Tick at least one row."
        )
        return Response(message, status_code=400, media_type="text/plain")
    return _render(request, path, awards=awards, error="")


@router.post("/mail")
async def post_mail(request: Request, path: Path = Depends(database_path)):
    snapshot = load_home(path)
    if not snapshot.ready:
        return Response(snapshot.message, status_code=404, media_type="text/plain")
    form = await request.form()
    mode = str(form.get("mode") or "preview")
    transport = str(form.get("transport") or "graph")
    if transport not in {"gmail", "graph"}:
        transport = "graph"
    try:
        delay = float(form.get("delay") or _DELAY_FLOOR)
    except ValueError:
        delay = _DELAY_FLOOR
    delay = max(delay, _DELAY_FLOOR)
    redirect_to = str(form.get("redirect_to") or "").strip()
    awards = _awards(form.getlist("award"))
    chosen = awards or None
    include_sent = str(form.get("send_again") or "") in _ON
    preflight = mode == "preflight"
    send = mode == "send" and str(form.get("send") or "") in _ON
    if mode == "send" and not send:
        return _render(
            request,
            path,
            awards=chosen,
            error="Tick Send for real to mail these awards. Preview writes files and sends nothing.",
            transport=transport,
            delay=delay,
            redirect_to=redirect_to,
            include_sent=include_sent,
        )
    if preflight:
        problem = credential_gap(transport, preflight=True)
    elif send:
        problem = live_send_refusal(transport, bind=engine(path))
    else:
        problem = None
    if problem:
        return _render(
            request,
            path,
            awards=chosen,
            error=problem,
            transport=transport,
            delay=delay,
            redirect_to=redirect_to,
            include_sent=include_sent,
        )
    params = {
        "awards": [f"{vendor_id}:{tender_id}" for vendor_id, tender_id in chosen] if chosen else None,
        "send": send,
        "send_again": include_sent,
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
            awards=chosen,
            include_sent=include_sent,
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


_LETTER_FIELDS = (
    "subject",
    "body",
    "sender_name",
    "sender_designation",
    "sender_org",
    "sender_mobile",
    "sender_email",
)


@router.post("/mail/letter")
async def post_letter(request: Request, path: Path = Depends(database_path)):
    snapshot = load_home(path)
    if not snapshot.ready:
        return Response(snapshot.message, status_code=404, media_type="text/plain")
    form = await request.form()
    fields = {key: str(form.get(key) or "") for key in _LETTER_FIELDS}
    campaign = load_campaign()
    error = campaign.save_letter(
        subject=fields["subject"],
        body=fields["body"],
        sender_name=fields["sender_name"],
        sender_designation=fields["sender_designation"],
        sender_org=fields["sender_org"],
        sender_mobile=fields["sender_mobile"],
        sender_email=fields["sender_email"],
        bind=engine(path),
    )
    return _render(
        request,
        path,
        awards=None,
        error=error or "",
        saved=not error,
        draft=fields if error else None,
    )


@router.post("/mail/attachment")
async def post_attachment(request: Request, path: Path = Depends(database_path)):
    snapshot = load_home(path)
    if not snapshot.ready:
        return Response(snapshot.message, status_code=404, media_type="text/plain")
    form = await request.form()
    upload = form.get("pdf")
    campaign = load_campaign()
    filename = str(getattr(upload, "filename", "") or "").strip()
    if upload is None or not filename or not hasattr(upload, "read"):
        error = "Choose a PDF to add."
    else:
        data = await upload.read()
        error = campaign.add_attachment(filename, data, bind=engine(path))
    return _render(request, path, awards=None, error=error or "", saved=not error)


@router.post("/mail/attachment/remove")
async def remove_attachment(request: Request, path: Path = Depends(database_path)):
    snapshot = load_home(path)
    if not snapshot.ready:
        return Response(snapshot.message, status_code=404, media_type="text/plain")
    form = await request.form()
    campaign = load_campaign()
    error = campaign.remove_attachment(str(form.get("filename") or ""), bind=engine(path))
    return _render(request, path, awards=None, error=error or "", saved=not error)


def _render(
    request: Request,
    path: Path,
    *,
    awards: list[tuple[int, str]] | None,
    error: str,
    transport: str = "graph",
    delay: float = _DELAY_FLOOR,
    redirect_to: str = "",
    include_sent: bool = False,
    saved: bool = False,
    draft: dict[str, str] | None = None,
):
    snapshot = load_home(path)
    waiting = 0
    mailable: list = []
    already = 0
    letter = None
    form = draft or {}
    sample_company = "Example Company"
    sample_subject = ""
    sample_body = ""
    files: list[dict[str, str]] = []
    if snapshot.ready:
        campaign = load_campaign()
        bind = engine(path)
        letter = campaign.load_letter(bind)
        if draft is None:
            form = {
                "subject": letter.subject,
                "body": letter.body,
                "sender_name": letter.sender_name,
                "sender_designation": letter.sender_designation,
                "sender_org": letter.sender_org,
                "sender_mobile": letter.sender_mobile,
                "sender_email": letter.sender_email,
            }
        with session(bind) as current:
            unsent = [
                person
                for person in outreach_targets(current)
                if EMAIL_RE.match(person.email or "")
            ]
            everyone = [
                person
                for person in outreach_targets(current, include_sent=True)
                if EMAIL_RE.match(person.email or "")
            ]
        waiting = len(unsent)
        queue = everyone if include_sent else unsent
        sent_keys = {(person.vendor_id, person.tender_id) for person in everyone} - {
            (person.vendor_id, person.tender_id) for person in unsent
        }
        if awards is None:
            mailable = queue
        else:
            wanted = set(awards)
            mailable = [
                person for person in queue if (person.vendor_id, person.tender_id) in wanted
            ]
            already = len(wanted & sent_keys)
        sample_company = mailable[0].company if mailable else "Example Company"
        shown = letter
        if draft is not None:
            shown = campaign.Letter(
                subject=form.get("subject", ""),
                body=form.get("body", ""),
                sender_name=form.get("sender_name", ""),
                sender_designation=form.get("sender_designation", ""),
                sender_org=form.get("sender_org", ""),
                sender_mobile=form.get("sender_mobile", ""),
                sender_email=form.get("sender_email", ""),
                attachment_names=letter.attachment_names,
            )
        sample_subject, sample_body = campaign.render_letter(shown, sample_company)
        for name in letter.attachment_names:
            file_path = campaign.attachment_path(name)
            size = file_path.stat().st_size if file_path.is_file() else 0
            files.append(
                {
                    "name": name,
                    "size": campaign.format_size(size),
                    "missing": "" if file_path.is_file() else "missing",
                }
            )
    return TEMPLATES.TemplateResponse(
        request=request,
        name="mail.html",
        context={
            "snapshot": snapshot,
            "error": error,
            "saved": saved,
            "waiting": waiting,
            "selected": awards,
            "already": already,
            "include_sent": include_sent,
            "mailable": mailable,
            "form": form,
            "files": files,
            "sample_company": sample_company,
            "sample_subject": sample_subject,
            "sample_body": sample_body,
            "transport": transport,
            "delay": delay,
            "redirect_to": redirect_to,
        },
    )


def _awards(values) -> list[tuple[int, str]]:
    chosen = []
    seen = set()
    for value in values:
        text = str(value).strip()
        vendor_text, separator, tender_id = text.partition(":")
        if not separator or not vendor_text.isdigit() or not tender_id:
            continue
        key = (int(vendor_text), tender_id)
        if key not in seen:
            seen.add(key)
            chosen.append(key)
    return chosen
