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
from pathlib import Path

from dotenv import load_dotenv
from pipeline_core.db import session
from pipeline_core.models import OUTREACH_FAILED, OUTREACH_SENT
from pipeline_core.queries import OutreachTarget, outreach_targets, record_outreach

from transport import TransportError, build_transport

BASE = Path(__file__).resolve().parent
PREVIEW_DIR = BASE / "previews"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

load_dotenv(BASE / ".env")

#: Who signs every message. Set both in .env; `send_all` refuses while either
#: is blank, and previews show where they will appear.
SENDER_NAME = os.getenv("SENDER_NAME", "").strip()
SENDER_ORG = os.getenv("SENDER_ORG", "").strip()
SUBJECT_TEMPLATE = "Enquiry for {company}"

#: The pitch has not been written yet. `send_all` refuses while this string is
#: still in the body, because a dry run and a live send build the same text and
#: nothing else would stop the placeholder reaching real companies.
PLACEHOLDER = "<-- replace this paragraph with your actual pitch -->"


def build_body(company: str) -> str:
    name = SENDER_NAME or "<SENDER_NAME from .env>"
    org = SENDER_ORG or "<SENDER_ORG from .env>"
    return f"""Hello {company} team,

I came across your organisation in connection with recent public tender
awards, and wanted to introduce what we do.

<-- replace this paragraph with your actual pitch -->

If this is not the right contact for such enquiries, I would be grateful if
you could point me to the right person.

Regards,
{name}
{org}

---
You received this one-off message because your address is published as a
business contact. Reply with "unsubscribe" and I will not contact you again.
"""


def load_recipients(limit: int | None = None) -> list[OutreachTarget]:
    """Everyone with an address who has not already been sent to.

    The query already excludes anyone with a successful `outreach` row, so there
    is no `already_sent()` set to maintain — an interrupted run resumes by being
    run again. Addresses that cannot be valid are dropped here rather than being
    handed to a transport that would only reject them.
    """
    with session() as current:
        people = outreach_targets(current, limit=None)

    usable = [p for p in people if EMAIL_RE.match(p.email or "")]
    if len(usable) != len(people):
        print(f"skipping {len(people) - len(usable)} malformed address(es)")

    if limit is not None:
        usable = usable[:limit]
    return usable


def log_result(
    target: OutreachTarget,
    email: str,
    subject: str,
    transport_name: str,
    status: str,
    error: str = "",
) -> None:
    """Record one attempt. Committed immediately, so a crash cannot re-mail anyone."""
    with session() as current:
        record_outreach(
            current,
            vendor_id=target.vendor_id,
            email=email,
            subject=subject,
            transport=transport_name,
            status=status,
            error=error[:500] or None,
        )


def write_preview(index: int, company: str, to: str, subject: str, body: str) -> None:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", company)[:60].strip("_")
    path = PREVIEW_DIR / f"{index:03d}_{safe}.txt"
    path.write_text(f"To: {to}\nSubject: {subject}\n\n{body}", encoding="utf-8")


def preflight(transport_name: str = "gmail") -> int:
    """Authenticate, then send one mail to yourself."""
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
        transport.send(self_addr, subject, body)
    except Exception as err:
        print(f"\nFAILED - {err}", file=sys.stderr)
        return 1
    finally:
        transport.close()

    print(f"\nPASS - message accepted. Check the inbox for {self_addr}.")
    print("Nothing was logged to the database: a preflight is not outreach.")
    return 0


def dry_run(people: list[OutreachTarget], override_to: str | None) -> None:
    PREVIEW_DIR.mkdir(exist_ok=True)
    for stale in PREVIEW_DIR.glob("*.txt"):
        stale.unlink()
    for i, person in enumerate(people, start=1):
        to = override_to or person.email
        write_preview(
            i,
            person.company,
            to,
            SUBJECT_TEMPLATE.format(company=person.company),
            build_body(person.company),
        )
    print(f"DRY RUN: wrote {len(people)} previews to {PREVIEW_DIR}")
    print("Nothing was sent. Set SEND = True in main.py to transmit.")


def send_all(
    people: list[OutreachTarget],
    delay: float,
    override_to: str | None,
    transport_name: str = "gmail",
) -> None:
    if PLACEHOLDER in build_body("Test"):
        raise SystemExit(
            "Refusing to send: the message body still contains the placeholder\n"
            f"    {PLACEHOLDER}\n\n"
            "Replace it in build_body() in campaign.py with your actual pitch.\n"
            "Previews (SEND = False) still work -- this only blocks live sends."
        )
    if not (SENDER_NAME and SENDER_ORG):
        raise SystemExit(
            "Refusing to send: SENDER_NAME and SENDER_ORG must both be set in .env.\n"
            "They sign every message. Previews (SEND = False) still work."
        )

    target_note = f" (all redirected to {override_to})" if override_to else ""
    print(f"Transport: {transport_name}")
    print(f"Sending {len(people)} message(s){target_note}, {delay}s apart ...")
    transport = build_transport(transport_name)
    sent = failed = 0
    try:
        for i, person in enumerate(people, start=1):
            to = override_to or person.email
            subject = SUBJECT_TEMPLATE.format(company=person.company)
            body = build_body(person.company)
            try:
                transport.send(to, subject, body)
            except TransportError as err:
                failed += 1
                log_result(person, to, subject, transport_name, OUTREACH_FAILED, str(err))
                print(f"  [{i}/{len(people)}] FAILED {person.email}: {err}")
                continue

            sent += 1
            log_result(person, to, subject, transport_name, OUTREACH_SENT)
            print(f"  [{i}/{len(people)}] sent to {person.company} <{to}>")
            if i < len(people):
                time.sleep(delay)
    except KeyboardInterrupt:
        print("\ninterrupted - progress is saved, re-run to resume")
    finally:
        transport.close()

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
