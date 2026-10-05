"""Load recipients from the pipeline database, render messages, hand each to a transport.

Dry run is the default — nothing is transmitted until SEND = True in main.py.

"Who is left" is one query against the pipeline database, and every attempt is
recorded in that same database, so the queue cannot drift from what was actually
sent and an interrupted run resumes by simply running it again.
"""

from __future__ import annotations

import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path

from dotenv import load_dotenv
from pipeline_core.db import session
from pipeline_core.emailcheck import EMAIL_RE
from pipeline_core.models import OUTREACH_FAILED, OUTREACH_SENT, MailLetter
from pipeline_core.queries import OutreachTarget, outreach_targets, record_outreach, utcnow

from transport import TransportError, build_transport

BASE = Path(__file__).resolve().parent
PREVIEW_DIR = BASE / "previews"

load_dotenv(BASE / ".env")

#: Used only to seed the first saved letter. After that, the Mail page is the editor.
#: SENDER_EMAIL is a line in the signature. It is not the From address.
SENDER_NAME = os.getenv("SENDER_NAME", "").strip()
SENDER_DESIGNATION = os.getenv("SENDER_DESIGNATION", "").strip()
SENDER_ORG = os.getenv("SENDER_ORG", "").strip()
SENDER_MOBILE = os.getenv("SENDER_MOBILE", "").strip()
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "").strip()

ATTACHMENT_DIR = BASE / "attachments"
DEFAULT_ATTACHMENT_NAMES = (
    "PolicyPact_Corporate_Insurance_Pitch.pdf",
    "Surety_Bond_Corporate_Presentation_PolicyPact.pdf",
)
MAX_PDF_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = int(2.5 * 1024 * 1024)

#: A live send refuses while this string is still in the saved letter.
PLACEHOLDER = "<-- replace this paragraph with your actual pitch -->"

DEFAULT_SUBJECT = "Corporate insurance introduction for {{company}}"
DEFAULT_BODY = """Dear {{company}} team,

I am writing to introduce Policy Pact Insurance Broker, a corporate insurance specialist working with business leaders on risk assessment, programme structuring and competitive market placement.

We benchmark terms across multiple insurers, negotiate on your behalf, and support clients through claims and renewals, so that {{company}} gets cover that fits its risks and is serviced end to end.

Four areas where we can add value:

Surety Insurance: A smart alternative to Bank Guarantees, helping businesses secure contracts with minimal or no collateral and improving liquidity.
Fire Insurance: Protect assets and business continuity through a structured property programme.
Marine Insurance: Protect cargo and goods in transit against loss or damage, whether by road, rail, sea or air.
Engineering Insurance: Cover projects, machinery and engineering assets against construction, erection and breakdown risks.

I would welcome a short conversation to understand how {{company}} manages these risks today, and whether there are gaps to close or efficiencies to unlock.

Two documents are attached: our Corporate Insurance Presentation (PolicyPact_Corporate_Insurance_Pitch.pdf) and our Surety Bond presentation (Surety_Bond_Corporate_Presentation_PolicyPact.pdf).

Would a 20-minute call this week or next suit you?

Warm regards,
{{sender_name}}
{{sender_designation}}
{{sender_org}}
{{sender_mobile}}
{{sender_email}}"""

UNSUBSCRIBE = """---
You received this one-off message because your address is published as a
business contact. Reply with "unsubscribe" and I will not contact you again."""

#: Tokens the letter may use. company comes from each recipient. The rest come
#: from the signature saved with the letter. There is no person-name token.
SIGNATURE_FIELDS = (
    ("sender_name", "Your name"),
    ("sender_designation", "Job title"),
    ("sender_org", "Organisation"),
    ("sender_mobile", "Mobile"),
    ("sender_email", "Email"),
)
KNOWN_TOKENS = frozenset({"company", *(key for key, _label in SIGNATURE_FIELDS)})
_TOKEN_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


class Letter:
    """The saved letter, detached from the database session."""

    def __init__(
        self,
        subject: str,
        body: str,
        sender_name: str,
        sender_designation: str,
        sender_org: str,
        sender_mobile: str,
        sender_email: str,
        attachment_names: tuple[str, ...],
    ) -> None:
        self.subject = subject
        self.body = body
        self.sender_name = sender_name
        self.sender_designation = sender_designation
        self.sender_org = sender_org
        self.sender_mobile = sender_mobile
        self.sender_email = sender_email
        self.attachment_names = attachment_names

    def signature(self) -> dict[str, str]:
        return {key: getattr(self, key) for key, _label in SIGNATURE_FIELDS}


def _names(text: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in (text or "").splitlines() if line.strip())


def _letter_from(row: MailLetter) -> Letter:
    return Letter(
        subject=row.subject,
        body=row.body,
        sender_name=row.sender_name,
        sender_designation=row.sender_designation,
        sender_org=row.sender_org,
        sender_mobile=row.sender_mobile,
        sender_email=row.sender_email,
        attachment_names=_names(row.attachment_names),
    )


def existing_default_attachments() -> tuple[str, ...]:
    return tuple(
        name for name in DEFAULT_ATTACHMENT_NAMES if (ATTACHMENT_DIR / name).is_file()
    )


def load_letter(bind=None) -> Letter:
    """The saved letter. The first call writes today's letter into an empty table."""
    with session(bind) as current:
        row = current.get(MailLetter, 1)
        if row is None:
            row = MailLetter(
                letter_id=1,
                subject=DEFAULT_SUBJECT,
                body=DEFAULT_BODY.strip(),
                sender_name=SENDER_NAME,
                sender_designation=SENDER_DESIGNATION,
                sender_org=SENDER_ORG,
                sender_mobile=SENDER_MOBILE,
                sender_email=SENDER_EMAIL,
                attachment_names="\n".join(existing_default_attachments()),
                updated_at=utcnow(),
            )
            current.add(row)
            current.flush()
        return _letter_from(row)


def tokens_in(text: str) -> list[str]:
    found = []
    for name in _TOKEN_RE.findall(text or ""):
        if name not in found:
            found.append(name)
    return found


def unknown_tokens(text: str) -> list[str]:
    return [name for name in tokens_in(text) if name not in KNOWN_TOKENS]


def missing_signature_labels(subject: str, body: str, signature: dict[str, str]) -> list[str]:
    used = set(tokens_in(subject) + tokens_in(body))
    return [label for key, label in SIGNATURE_FIELDS if key in used and not signature.get(key, "").strip()]


def letter_problem(subject: str, body: str, signature: dict[str, str]) -> str | None:
    """Why this text cannot be saved or sent. None means it is usable."""
    if not subject.strip():
        return "Write a subject."
    if not body.strip():
        return "Write the letter."
    if PLACEHOLDER in subject or PLACEHOLDER in body:
        return (
            "Refusing to send: the letter still contains the placeholder. "
            "Edit it on the Mail page. Preview still works."
        )
    unknown = unknown_tokens(subject + "\n" + body)
    if unknown:
        shown = ", ".join(f"{{{{{name}}}}}" for name in unknown)
        return (
            f"This letter uses {shown}, which cannot be filled in. "
            "Use {{company}}, or the signature fields."
        )
    missing = missing_signature_labels(subject, body, signature)
    if missing:
        return (
            "Refusing to send: fill in "
            + ", ".join(missing)
            + " on the Mail page. The letter uses them. Preview still works."
        )
    return None


def fill(text: str, values: dict[str, str]) -> str:
    """Swap known tokens. Any other {{token}} is left as written."""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values:
            return values[key]
        return match.group(0)

    return _TOKEN_RE.sub(replace, text)


def render_letter(letter: Letter, company: str) -> tuple[str, str]:
    values = {"company": company, **letter.signature()}
    subject = fill(letter.subject, values)
    body = fill(letter.body, values).rstrip()
    if 'Reply with "unsubscribe"' not in body:
        body = f"{body}\n\n{UNSUBSCRIBE}"
    return subject, body


def save_letter(
    *,
    subject: str,
    body: str,
    sender_name: str,
    sender_designation: str,
    sender_org: str,
    sender_mobile: str,
    sender_email: str,
    bind=None,
) -> str | None:
    """Store the letter. Returns a plain reason when the text cannot be saved."""
    signature = {
        "sender_name": sender_name.strip(),
        "sender_designation": sender_designation.strip(),
        "sender_org": sender_org.strip(),
        "sender_mobile": sender_mobile.strip(),
        "sender_email": sender_email.strip(),
    }
    problem = letter_problem(subject, body, signature)
    if problem:
        return problem
    load_letter(bind)
    with session(bind) as current:
        row = current.get(MailLetter, 1)
        row.subject = subject.strip()
        row.body = body.strip()
        row.sender_name = signature["sender_name"]
        row.sender_designation = signature["sender_designation"]
        row.sender_org = signature["sender_org"]
        row.sender_mobile = signature["sender_mobile"]
        row.sender_email = signature["sender_email"]
        row.updated_at = utcnow()
    return None


def safe_pdf_name(raw: str) -> str:
    base = Path(raw or "").name
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    if not base:
        base = "attachment.pdf"
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    return base[:120]


def attachment_path(name: str) -> Path:
    """A file inside the attachments folder. A crafted name cannot escape it."""
    path = (ATTACHMENT_DIR / safe_pdf_name(name)).resolve()
    root = ATTACHMENT_DIR.resolve()
    if path.parent != root:
        raise ValueError(f"attachment name escapes the folder: {name}")
    return path


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{round(size / 1024)} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def pdf_error(filename: str, data: bytes, existing: tuple[str, ...]) -> str | None:
    if not data.startswith(b"%PDF"):
        return "That file is not a PDF."
    if len(data) > MAX_PDF_BYTES:
        return "That PDF is over 2 MB. Outlook cannot send a file that large this way."
    safe = safe_pdf_name(filename)
    total = len(data)
    for name in existing:
        if name == safe:
            continue
        path = attachment_path(name)
        if path.is_file():
            total += path.stat().st_size
    if total > MAX_TOTAL_BYTES:
        return "Those PDFs together are over 2.5 MB. Remove one, or use a smaller file."
    return None


def _store_names(names: tuple[str, ...], bind=None) -> None:
    load_letter(bind)
    with session(bind) as current:
        row = current.get(MailLetter, 1)
        row.attachment_names = "\n".join(names)
        row.updated_at = utcnow()


def add_attachment(filename: str, data: bytes, bind=None) -> str | None:
    letter = load_letter(bind)
    problem = pdf_error(filename, data, letter.attachment_names)
    if problem:
        return problem
    ATTACHMENT_DIR.mkdir(exist_ok=True)
    safe = safe_pdf_name(filename)
    attachment_path(safe).write_bytes(data)
    names = tuple(name for name in letter.attachment_names if name != safe) + (safe,)
    _store_names(names, bind)
    return None


def remove_attachment(filename: str, bind=None) -> str | None:
    letter = load_letter(bind)
    safe = safe_pdf_name(filename)
    if safe not in letter.attachment_names:
        return "That PDF is not on the letter."
    path = attachment_path(safe)
    if path.is_file():
        path.unlink()
    _store_names(tuple(name for name in letter.attachment_names if name != safe), bind)
    return None


def letter_refusal(bind=None) -> str | None:
    """Why a live send must not start. Preview does not call this."""
    letter = load_letter(bind)
    problem = letter_problem(letter.subject, letter.body, letter.signature())
    if problem:
        return problem
    if not letter.attachment_names:
        return (
            "Refusing to send: add at least one PDF on the Mail page. Preview still works."
        )
    missing = [name for name in letter.attachment_names if not attachment_path(name).is_file()]
    if missing:
        return (
            "Refusing to send: attachment file(s) missing: "
            + ", ".join(missing)
            + ". Add them on the Mail page. Preview still works."
        )
    return None


def saved_attachment_bytes(bind=None) -> list[tuple[str, bytes]]:
    """The PDFs on the saved letter. Does not check the wording."""
    letter = load_letter(bind)
    if not letter.attachment_names:
        raise SystemExit(
            "Refusing to send: add at least one PDF on the Mail page. Preview still works."
        )
    missing = [name for name in letter.attachment_names if not attachment_path(name).is_file()]
    if missing:
        raise SystemExit(
            "Refusing to send: attachment file(s) missing: "
            + ", ".join(missing)
            + ". Add them on the Mail page. Preview still works."
        )
    return [(name, attachment_path(name).read_bytes()) for name in letter.attachment_names]


def read_attachments(bind=None) -> list[tuple[str, bytes]]:
    problem = letter_refusal(bind)
    if problem:
        raise SystemExit(problem)
    return saved_attachment_bytes(bind)


def load_recipients(
    limit: int | None = None, *, include_sent: bool = False
) -> list[OutreachTarget]:
    """Everyone with an address who has not already been sent to.

    The query already excludes anyone with a successful `outreach` row, so there
    is no `already_sent()` set to maintain — an interrupted run resumes by being
    run again. Addresses that cannot be valid are dropped here rather than being
    handed to a transport that would only reject them.
    """
    with session() as current:
        people = outreach_targets(current, limit=None, include_sent=include_sent)

    usable = [p for p in people if EMAIL_RE.match(p.email or "")]
    if len(usable) != len(people):
        print(f"skipping {len(people) - len(usable)} malformed address(es)")

    if limit is not None:
        usable = usable[:limit]
    return usable


def message_for(person: OutreachTarget, bind=None) -> tuple[str, str]:
    return render_letter(load_letter(bind), person.company)


def log_result(
    target: OutreachTarget,
    email: str,
    subject: str,
    transport_name: str,
    status: str,
    error: str = "",
    bind=None,
) -> None:
    """Record one attempt. Committed immediately, so a crash cannot re-mail anyone."""
    with session(bind) as current:
        record_outreach(
            current,
            vendor_id=target.vendor_id,
            tender_id=target.tender_id,
            email=email,
            subject=subject,
            transport=transport_name,
            status=status,
            error=error[:500] or None,
        )


def write_preview(
    index: int,
    company: str,
    to: str,
    subject: str,
    body: str,
    attachments: tuple[str, ...] = (),
) -> None:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", company)[:60].strip("_")
    path = PREVIEW_DIR / f"{index:03d}_{safe}.txt"
    attached = "\n".join(f"Attachment: {name}" for name in attachments)
    path.write_text(f"To: {to}\nSubject: {subject}\n{attached}\n\n{body}", encoding="utf-8")


def preflight(transport_name: str = "gmail", bind=None) -> int:
    """Authenticate, then send one mail to yourself, with the saved PDFs."""
    try:
        files = saved_attachment_bytes(bind)
    except SystemExit as err:
        print(err, file=sys.stderr)
        return 1

    if transport_name == "gmail":
        self_addr = os.getenv("GMAIL_USER")
        if not self_addr:
            print("Missing GMAIL_USER in .env - needed as the test recipient.", file=sys.stderr)
            return 1
        subject = "Gmail preflight - send test"
        body = "If you are reading this, Gmail SMTP is working and this mailbox can send the campaign."
    else:
        self_addr = os.getenv("OUTLOOK_USER")
        if not self_addr:
            print("Missing OUTLOOK_USER in .env - needed as the test recipient.", file=sys.stderr)
            return 1
        subject = "Graph preflight - Outlook send test"
        body = (
            "If you are reading this, Microsoft Graph Mail.Send is working "
            "and this mailbox can send the campaign."
        )

    print(f"Connecting with transport={transport_name} ...")
    transport = build_transport(transport_name)
    print(f"Sending one test mail to {self_addr} ...")
    try:
        transport.send(self_addr, subject, body, attachments=files)
    except Exception as err:
        print(f"\nFAILED - {err}", file=sys.stderr)
        return 1
    finally:
        transport.close()

    print(f"\nPASS - message accepted. Check the inbox for {self_addr}.")
    print("Nothing was logged to the database: a preflight is not outreach.")
    return 0


def dry_run(people: list[OutreachTarget], override_to: str | None, bind=None) -> None:
    letter = load_letter(bind)
    PREVIEW_DIR.mkdir(exist_ok=True)
    for stale in PREVIEW_DIR.glob("*.txt"):
        stale.unlink()
    for i, person in enumerate(people, start=1):
        to = override_to or person.email
        subject, body = render_letter(letter, person.company)
        write_preview(i, person.company, to, subject, body, letter.attachment_names)
    print(f"DRY RUN: wrote {len(people)} previews to {PREVIEW_DIR}")
    print("Nothing was sent. Set SEND = True in main.py to transmit.")


def _pause(delay: float, should_stop: Callable[[], bool] | None) -> bool:
    """Wait between sends. True means the run was asked to stop."""
    remaining = delay
    while True:
        if should_stop and should_stop():
            return True
        if remaining <= 0:
            return False
        step = min(0.2, remaining)
        time.sleep(step)
        remaining -= step


def send_all(
    people: list[OutreachTarget],
    delay: float,
    override_to: str | None,
    transport_name: str = "gmail",
    *,
    should_stop: Callable[[], bool] | None = None,
    bind=None,
) -> None:
    files = read_attachments(bind)
    letter = load_letter(bind)

    target_note = f" (all redirected to {override_to})" if override_to else ""
    print(f"Transport: {transport_name}")
    print(f"Sending {len(people)} message(s){target_note}, {delay}s apart ...")
    transport = build_transport(transport_name)
    sent = failed = 0
    stopped = False
    try:
        for i, person in enumerate(people, start=1):
            if should_stop and should_stop():
                stopped = True
                break
            to = override_to or person.email
            subject, body = render_letter(letter, person.company)
            try:
                transport.send(to, subject, body, attachments=files)
            except TransportError as err:
                failed += 1
                log_result(
                    person, to, subject, transport_name, OUTREACH_FAILED, str(err), bind=bind
                )
                print(f"  [{i}/{len(people)}] FAILED {person.email}: {err}")
                continue

            sent += 1
            log_result(person, to, subject, transport_name, OUTREACH_SENT, bind=bind)
            print(f"  [{i}/{len(people)}] sent to {person.company} <{to}>")
            if i < len(people) and _pause(delay, should_stop):
                stopped = True
                break
    except KeyboardInterrupt:
        print("\ninterrupted - progress is saved, re-run to resume")
    finally:
        transport.close()

    if stopped:
        print("Stopped. Messages already sent are saved.")
    print(f"\ndone: {sent} sent, {failed} failed. Logged to the outreach table.")


def run(
    *,
    send: bool = False,
    limit: int | None = None,
    delay: float = 5.0,
    to: str | None = None,
    do_preflight: bool = False,
    transport: str = "gmail",
) -> int:
    if do_preflight:
        return preflight(transport)

    people = load_recipients(limit)
    if not people:
        print("0 to send - no vendor with an address is waiting to be mailed.")
        print("Run 01-scrape, then 02-enrich, to find more addresses.")
        return 0

    if not send:
        dry_run(people, to)
        return 0

    send_all(people, delay, to, transport)
    return 0
