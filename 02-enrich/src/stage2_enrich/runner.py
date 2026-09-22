"""Work the queue: pending vendors in, contact details back.

The whole point of this stage living on the database is **resumability**. Each
vendor is committed on its own, the moment its answer arrives, so a crash — or a
Ctrl-C, or a rate limit at vendor 300 of 500 — costs one call, not the run. The
old version held everything in a DataFrame and wrote one spreadsheet at the end;
an interrupted run threw away every paid-for answer.

There is no per-company cache here, and it is not an oversight: the queue comes
from `vendors`, which is unique on `name_norm`, so a company cannot appear twice.
Deduplication moved upstream, where it belongs.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from langchain_core.runnables import Runnable
from pipeline_core.db import session
from pipeline_core.models import STATUS_DONE, STATUS_FAILED, STATUS_NOT_FOUND
from pipeline_core.queries import (
    PendingVendor,
    mark_enriched,
    mark_failed,
    pending_vendors,
    record_llm_run,
)

from . import settings
from .llm import ContactInfo, build_chain, build_evidence, fetch_contact

#: Called after each vendor with (vendor, status, detail) so a runner can print.
Reporter = Callable[[PendingVendor, str, str], None]


@dataclass
class Summary:
    attempted: int = 0
    done: int = 0
    not_found: int = 0
    failed: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)

    def render(self) -> str:
        return (
            f"{self.attempted} vendor(s) attempted: "
            f"{self.done} done, {self.not_found} not found, {self.failed} failed"
        )


def queue(
    limit: int | None = None, *, source: str | None = None
) -> list[PendingVendor]:
    """Who is waiting. Read in one go so the work list cannot shift under us.

    Vendors already answered are not in it: the query asks for `pending` only.
    """
    with session() as current:
        return pending_vendors(current, limit, source=source)


def enrich_one(vendor: PendingVendor, chain: Runnable) -> tuple[str, str]:
    """One vendor, one call, one commit. Returns (status, detail).

    Never raises for a model or network failure: the vendor is marked `failed`,
    the error is written to `llm_runs`, and the caller moves on. A run of 500
    should not stop because one company upset the provider.
    """
    evidence = build_evidence(vendor.pdf_email, vendor.pdf_phone)
    payload = json.dumps(
        {
            "company": vendor.name_raw,
            "title": vendor.title,
            "pdf_email": vendor.pdf_email,
            "pdf_phone": vendor.pdf_phone,
        },
        ensure_ascii=False,
    )

    try:
        info: ContactInfo = fetch_contact(chain, vendor.name_raw, vendor.title, evidence)
    except Exception as exc:  # provider error, timeout, bad shape
        detail = f"{type(exc).__name__}: {exc}"[:500]
        with session() as current:
            mark_failed(current, vendor.vendor_id)
            record_llm_run(
                current,
                vendor_id=vendor.vendor_id,
                model=settings.model(),
                prompt_version=settings.PROMPT_VERSION,
                input_json=payload,
                output_json=None,
                status="error",
                error=detail,
            )
        return STATUS_FAILED, detail

    with session() as current:
        status = mark_enriched(
            current, vendor.vendor_id, email=info.email, phone=info.phone
        )
        record_llm_run(
            current,
            vendor_id=vendor.vendor_id,
            model=settings.model(),
            prompt_version=settings.PROMPT_VERSION,
            input_json=payload,
            output_json=info.model_dump_json(),
            status="ok",
        )
    detail = info.email or info.phone or "nothing public found"
    return status, detail


def enrich_pending(
    *,
    limit: int | None = None,
    delay: float = 0.0,
    chain: Runnable | None = None,
    vendors: Iterable[PendingVendor] | None = None,
    source: str | None = None,
    report: Reporter | None = None,
) -> Summary:
    """Work the whole queue. `chain` and `vendors` are injectable for tests."""
    work = list(vendors) if vendors is not None else queue(limit, source=source)
    summary = Summary()
    if not work:
        return summary

    active = chain if chain is not None else build_chain()

    try:
        for index, vendor in enumerate(work, start=1):
            status, detail = enrich_one(vendor, active)
            summary.attempted += 1
            if status == STATUS_DONE:
                summary.done += 1
            elif status == STATUS_NOT_FOUND:
                summary.not_found += 1
            else:
                summary.failed += 1
                summary.errors.append((vendor.vendor_id, detail))
            if report:
                report(vendor, status, detail)
            if delay and index < len(work):
                time.sleep(delay)
    except KeyboardInterrupt:
        # Everything already answered is committed. Re-running resumes here.
        pass

    return summary
