"""Preview and send outreach. A live send is never the default."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from pipeline_core.db import engine, session
from pipeline_core.queries import mail_messages, ordered_contacts

from ops_ui.deps import database_path
from ops_ui.jobs import Busy, start_job
from ops_ui.queries import load_home
from ops_ui.present import job_page
from ops_ui.routes.awards import (
    remember,
    remember_job_stage,
    remembered_run,
    stage_context,
    watched_job,
)
from ops_ui.runs import credential_gap, live_send_refusal, load_campaign, run_outreach
from ops_ui.selection import selected_awards
from ops_ui.templating import TEMPLATES

router = APIRouter()

_ON = {"1", "on", "true", "yes"}
_DELAY_FLOOR = 5.0


@router.get("/mail")
def mail_page(request: Request, path: Path = Depends(database_path)):
    return _render(request, path, awards=None, error="", remember_filter=True)


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
    transport = str(form.get("transport") or "outlook")
    if transport not in {"gmail", "outlook", "graph"}:
        transport = "outlook"
    include_sent = str(form.get("send_again") or "") in _ON
    preflight = mode == "preflight"
    send = mode == "send"
    delay = _DELAY_FLOOR
    redirect_to = str(form.get("redirect_to") or "").strip()
    picked = _awards(form.getlist("award"))
    letter_mode = str(form.get("letter") or "default")
    if letter_mode not in {"default", "custom"}:
        letter_mode = "default"
    phrases = _phrases(form)
    if mode == "remove":
        return _render(
            request,
            path,
            awards=picked or None,
            error="",
            transport=transport,
            delay=delay,
            redirect_to=redirect_to,
            include_sent=include_sent,
            letter_mode="default",
        )
    if not picked and not preflight:
        return _render(
            request,
            path,
            awards=None,
            error="Tick at least one row in the table.",
            transport=transport,
            delay=delay,
            redirect_to=redirect_to,
            include_sent=include_sent,
            letter_mode=letter_mode,
            phrases=phrases if letter_mode == "custom" else None,
        )
    custom = None
    if letter_mode == "custom" and not preflight:
        campaign = load_campaign()
        saved = campaign.load_letter(engine(path))
        custom = campaign.Letter(
            subject=phrases["subject"],
            body=phrases["body"],
            sender_name=phrases["sender_name"],
            sender_designation=phrases["sender_designation"],
            sender_org=phrases["sender_org"],
            sender_mobile=phrases["sender_mobile"],
            sender_email=phrases["sender_email"],
            attachment_names=saved.attachment_names,
        )
    if preflight:
        problem = credential_gap(transport, preflight=True)
    elif send:
        problem = live_send_refusal(transport, bind=engine(path), letter=custom)
    else:
        problem = None
    if problem:
        return _render(
            request,
            path,
            awards=picked,
            error=problem,
            transport=transport,
            delay=delay,
            redirect_to=redirect_to,
            include_sent=include_sent,
            letter_mode=letter_mode,
            phrases=phrases if letter_mode == "custom" else None,
        )
    params = {
        "awards": [f"{vendor_id}:{tender_id}" for vendor_id, tender_id in picked],
        "send": send,
        "send_again": include_sent,
        "preflight": preflight,
        "transport": transport,
        "delay": delay,
        "redirect_to": redirect_to,
        "letter": letter_mode,
    }

    def work(log, stop):
        return run_outreach(
            log,
            stop,
            database=path,
            awards=picked,
            include_sent=include_sent,
            send=send,
            preflight=preflight,
            transport=transport,
            delay=delay,
            redirect_to=redirect_to,
            letter=custom,
        )

    try:
        job_id = start_job(path, "outreach", params, work)
    except Busy as exc:
        response = RedirectResponse(job_page("outreach", exc.job_id, refused=True), status_code=303)
        remember_job_stage(response, "outreach", exc.job_id)
        return response
    response = RedirectResponse(job_page("outreach", job_id), status_code=303)
    remember_job_stage(response, "outreach", job_id)
    return response


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
    awards = _awards(form.getlist("award")) or None
    campaign = load_campaign()
    if str(form.get("restore") or "") in _ON:
        error = campaign.restore_original_letter(bind=engine(path))
    else:
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
        awards=awards,
        error=error or "",
        saved=not error,
        draft=fields if error else None,
        editing=True,
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
    return _render(
        request,
        path,
        awards=_awards(form.getlist("award")) or None,
        error=error or "",
        saved=not error,
    )


@router.post("/mail/attachment/remove")
async def remove_attachment(request: Request, path: Path = Depends(database_path)):
    snapshot = load_home(path)
    if not snapshot.ready:
        return Response(snapshot.message, status_code=404, media_type="text/plain")
    form = await request.form()
    campaign = load_campaign()
    error = campaign.remove_attachment(str(form.get("filename") or ""), bind=engine(path))
    return _render(
        request,
        path,
        awards=_awards(form.getlist("award")) or None,
        error=error or "",
        saved=not error,
    )


def _render(
    request: Request,
    path: Path,
    *,
    awards: list[tuple[int, str]] | None,
    error: str,
    transport: str = "outlook",
    delay: float = _DELAY_FLOOR,
    redirect_to: str = "",
    include_sent: bool = False,
    saved: bool = False,
    draft: dict[str, str] | None = None,
    letter_mode: str = "default",
    phrases: dict[str, str] | None = None,
    editing: bool = False,
    remember_filter: bool = False,
):
    snapshot = load_home(path)
    waiting = 0
    mailable: list = []
    addresses: list[dict] = []
    message_count = 0
    already = 0
    letter = None
    form = draft or {}
    sample_company = "Example Company"
    sample_title = ""
    sample_date = ""
    sample_subject = ""
    sample_body = ""
    files: list[dict[str, str]] = []
    send_block = ""
    if letter_mode not in {"default", "custom"}:
        letter_mode = "default"
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
            unsent = list(mail_messages(current))
            waiting = len(unsent)
            if awards:
                messages = list(
                    mail_messages(current, include_sent=include_sent, awards=awards)
                )
                unsent_pairs = {(person.vendor_id, person.tender_id) for person in unsent}
                again_pairs = {
                    (person.vendor_id, person.tender_id)
                    for person in mail_messages(current, include_sent=True, awards=awards)
                }
                already = len(again_pairs - unsent_pairs)
                seen: set[tuple[int, str]] = set()
                mailable = []
                for person in messages:
                    pair = (person.vendor_id, person.tender_id)
                    if pair not in seen:
                        seen.add(pair)
                        mailable.append(person)
                    source = ""
                    for row in ordered_contacts(current, person.vendor_id, person.tender_id):
                        if row.value.casefold() == person.email.casefold():
                            source = row.source
                            break
                    addresses.append(
                        {
                            "company": person.company,
                            "value": person.email,
                            "source": source or "email",
                            "valid": True,
                        }
                    )
                message_count = len(messages)
        sample_company = mailable[0].company if mailable else "Example Company"
        sample_title = mailable[0].title if mailable else ""
        sample_date = mailable[0].contract_date if mailable else ""
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
        if phrases is None:
            phrases = {
                "subject": letter.subject,
                "body": letter.body,
                "sender_name": letter.sender_name,
                "sender_designation": letter.sender_designation,
                "sender_org": letter.sender_org,
                "sender_mobile": letter.sender_mobile,
                "sender_email": letter.sender_email,
            }
        sample_subject, sample_body = campaign.render_letter(
            shown,
            sample_company,
            title=sample_title,
            contract_date=sample_date,
        )
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
        send_block = ""
        if awards:
            checked = None
            if letter_mode == "custom":
                checked = campaign.Letter(
                    subject=(phrases or {}).get("subject", ""),
                    body=(phrases or {}).get("body", ""),
                    sender_name=(phrases or {}).get("sender_name", ""),
                    sender_designation=(phrases or {}).get("sender_designation", ""),
                    sender_org=(phrases or {}).get("sender_org", ""),
                    sender_mobile=(phrases or {}).get("sender_mobile", ""),
                    sender_email=(phrases or {}).get("sender_email", ""),
                    attachment_names=letter.attachment_names,
                )
            send_block = (
                live_send_refusal(transport, bind=bind, letter=checked) or ""
            )
    context = {
        "snapshot": snapshot,
        "error": error,
        "saved": saved,
        "waiting": waiting,
        "selected": awards,
        "already": already,
        "include_sent": include_sent,
        "mailable": mailable,
        "addresses": addresses,
        "message_count": message_count,
        "form": form,
        "phrases": phrases or {},
        "files": files,
        "sample_company": sample_company,
        "sample_subject": sample_subject,
        "sample_body": sample_body,
        "letter_mode": letter_mode,
        "editing": editing,
        "transport": transport,
        "delay": delay,
        "redirect_to": redirect_to,
        "send_block": send_block,
        "job": None,
        "view": None,
    }
    if snapshot.ready:
        job, override = watched_job(request, path, "mail")
        context.update(
            stage_context(request, path, "mail", query_override=override, job=job)
        )
        context["error"] = error
        context["saved"] = saved
        context["selected"] = awards
        context["form"] = form
        if job is not None:
            remember_filter = False
        else:
            context["remembered_job"] = remembered_run(request, path, "mail")
    response = TEMPLATES.TemplateResponse(request=request, name="mail.html", context=context)
    if snapshot.ready and context.get("job") is not None:
        remember_job_stage(response, "outreach", context["job"].job_id)
    if remember_filter and snapshot.ready:
        remember(response, "mail", context["query"])
    return response


def _phrases(form) -> dict[str, str]:
    return {key: str(form.get(key) or "") for key in _LETTER_FIELDS}


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
