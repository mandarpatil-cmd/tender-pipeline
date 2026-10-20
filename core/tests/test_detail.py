"""One award opened out: which runs touched it, and what each PDF said."""

from __future__ import annotations

import json

import pytest

from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.detail import award_addresses, record_jobs, tender_clues, vendor_documents
from pipeline_core.models import UiJob
from pipeline_core.queries import (
    mark_enriched,
    record_job_tender,
    record_llm_run,
    record_outreach,
    record_pdf_contacts,
    replace_awards,
    replace_tender_documents,
    upsert_tender,
    upsert_vendor,
    utcnow,
)


@pytest.fixture
def bind(tmp_path):
    eng = engine(tmp_path / "pipeline.sqlite3")
    ensure_schema(eng)
    return eng


def _job(current, job_id: int, stage: str, **params) -> None:
    current.add(
        UiJob(
            job_id=job_id,
            stage=stage,
            state="done",
            params_json=json.dumps(params),
            log="",
            started_at=f"2026-10-{job_id % 28 + 1:02d}T00:00:00",
        )
    )


def _seed(current) -> tuple[int, int]:
    """S.K Sales won T-A and T-B. Another company won T-C."""
    vendor_id = upsert_vendor(current, name_raw="S.K Sales Company")
    other = upsert_vendor(current, name_raw="Other Ltd")
    for tender_id, winner, name in (
        ("T-A", vendor_id, "S.K Sales Company"),
        ("T-B", vendor_id, "S.K Sales Company"),
        ("T-C", other, "Other Ltd"),
    ):
        upsert_tender(current, tender_id=tender_id, title=f"Work {tender_id}", scraped_at=utcnow())
        replace_awards(current, tender_id, [{"bid_number": "7", "vendor_id": winner, "bidder_name": name}])
    return vendor_id, other


def test_record_jobs_lists_every_run_that_touched_this_award_newest_first(bind):
    with session(bind) as current:
        vendor_id, other = _seed(current)
        _job(current, 9, "scrape")
        _job(current, 12, "scrape")
        _job(current, 40, "pdfs", tender_id="T-A")
        _job(current, 26, "enrich")
        _job(current, 43, "outreach", awards=[f"{vendor_id}:T-A"], send=True)
        _job(current, 44, "outreach", awards=[f"{vendor_id}:T-A"], send=False)
        _job(current, 45, "outreach", awards=[f"{vendor_id}:T-B"], send=True)
        _job(current, 46, "pdfs", tender_id="T-C")
        current.flush()
        upsert_tender(current, tender_id="T-A", scraped_at=utcnow(), scrape_job_id=9)
        for job_id in (9, 12, 40):
            record_job_tender(current, job_id, "T-A")
        record_job_tender(current, 46, "T-C")
        record_llm_run(current, vendor_id=vendor_id, model="m", prompt_version="v", input_json="{}",
                       output_json="{}", status="ok", job_id=26)
        record_llm_run(current, vendor_id=other, model="m", prompt_version="v", input_json="{}",
                       output_json="{}", status="ok", job_id=27)
        record_outreach(current, vendor_id=vendor_id, tender_id="T-A", email="a@sk.example",
                        subject="s", transport="gmail", status="sent", job_id=43)
        record_outreach(current, vendor_id=vendor_id, tender_id="T-A", email="b@sk.example",
                        subject="s", transport="gmail", status="failed", job_id=43)

        jobs = record_jobs(current, vendor_id, "T-A")

    assert [job["job_id"] for job in jobs] == [44, 43, 40, 26, 12, 9]
    by_id = {job["job_id"]: job for job in jobs}
    assert by_id[9]["stage"] == "Scrape"
    assert by_id[9]["did"] == "first saved this tender"
    assert by_id[12]["did"] == "saved this tender again"
    assert by_id[40]["stage"] == "Download PDFs"
    assert by_id[26]["did"] == "looked up the company's contact (ok)"
    assert by_id[43]["did"] == "sent mail for this award; sent a@sk.example; failed b@sk.example"
    assert by_id[44]["did"] == "previewed mail for this award"
    assert by_id[43]["href"] == f"/jobs/43?award={vendor_id}:T-A"


def test_tender_clues_name_the_file_each_clue_came_from(bind):
    with session(bind) as current:
        vendor_id, _other = _seed(current)
        replace_tender_documents(
            current,
            "T-A",
            [
                {"filename": "LOA1043.pdf", "emails": [], "phones": []},
                {"filename": "techsummary.pdf", "emails": [], "phones": ["6877812012"]},
            ],
        )
        record_pdf_contacts(current, vendor_id, "T-A", [], ["6877812012"])
        record_pdf_contacts(current, vendor_id, "T-B", ["loa@sk.example"], [])

        clues = tender_clues(current, vendor_id, "T-A")

    assert clues == [
        {"channel": "phone", "value": "6877812012", "valid": True, "filenames": ("techsummary.pdf",)}
    ]


def test_vendor_documents_lists_the_other_tenders_and_their_files(bind):
    with session(bind) as current:
        vendor_id, other = _seed(current)
        replace_tender_documents(current, "T-B", [{"filename": "LOA.pdf", "emails": ["loa@sk.example"]}])
        replace_tender_documents(current, "T-C", [{"filename": "theirs.pdf", "emails": ["x@other.example"]}])

        shown = vendor_documents(current, vendor_id, except_tender="T-A")

    assert [item["tender_id"] for item in shown] == ["T-B"]
    assert shown[0]["bid_number"] == "7"
    assert shown[0]["attributed"] is True
    assert [doc["filename"] for doc in shown[0]["documents"]] == ["LOA.pdf"]
    assert shown[0]["documents"][0]["emails"] == "loa@sk.example"


def test_award_addresses_cover_every_email_and_phone_with_its_origin(bind):
    with session(bind) as current:
        vendor_id, _other = _seed(current)
        _job(current, 26, "enrich")
        current.flush()
        replace_tender_documents(current, "T-B", [{"filename": "LOA.pdf", "emails": ["loa@sk.example"]}])
        record_pdf_contacts(current, vendor_id, "T-B", ["-loa@sk.example"], [])
        record_pdf_contacts(current, vendor_id, "T-A", [], ["6877812012"])
        run_id = record_llm_run(current, vendor_id=vendor_id, model="m", prompt_version="v",
                                input_json="{}", output_json="{}", status="ok", job_id=26)
        mark_enriched(current, vendor_id, email="sales@sk.example", phone="+91 98154 31020",
                      source_url="https://sk.example", llm_run_id=run_id)

        shown = award_addresses(current, vendor_id, "T-A")

    emails = [item for item in shown if item["channel"] == "email"]
    phones = [item for item in shown if item["channel"] == "phone"]
    assert [(item["value"], item["source"], item["tender_id"], item["will_mail"]) for item in emails] == [
        ("loa@sk.example", "pdf", "T-B", True),
        ("sales@sk.example", "model", None, True),
    ]
    assert emails[0]["filenames"] == ("LOA.pdf",)
    assert emails[0]["this_tender"] is False
    assert emails[1]["job_id"] == 26
    assert emails[1]["source_url"] == "https://sk.example"
    assert [(item["value"], item["this_tender"]) for item in phones] == [
        ("6877812012", True),
        ("+91 98154 31020", False),
    ]
