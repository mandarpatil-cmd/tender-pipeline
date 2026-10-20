"""Opening one award shows the tender, the company history, and the PDF clues."""

from __future__ import annotations

from fastapi.testclient import TestClient
from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.queries import (
    record_llm_run,
    record_outreach,
    replace_awards,
    record_pdf_contacts,
    replace_tender_documents,
    set_pdf_contacts,
    upsert_tender,
    upsert_vendor,
    utcnow,
)

from ops_ui.app import create_app


def _seed(path, json_path: str):
    bind = engine(path)
    ensure_schema(bind)
    with session(bind) as current:
        winner = upsert_vendor(current, name_raw="Large Electricals", state="Assam")
        other = upsert_vendor(current, name_raw="Other Bidder Ltd")
        set_pdf_contacts(current, winner, email="clue@example.com", phone="9876543210")
        upsert_tender(
            current,
            tender_id="LARGE",
            title="Large rooms",
            organisation="NIT Durgapur",
            status="AOC",
            contract_date="21-Sep-2026",
            contract_value="INR 100",
            json_path=json_path,
            scraped_at=utcnow(),
        )
        replace_awards(
            current,
            "LARGE",
            [
                {"bid_number": "1", "vendor_id": winner, "bidder_name": "Large Electricals"},
                {"bid_number": "2", "vendor_id": other, "bidder_name": "Other Bidder Ltd"},
            ],
        )
        record_llm_run(
            current,
            vendor_id=winner,
            model="test-model",
            prompt_version="v1",
            input_json="{}",
            output_json='{"email":"found@example.com"}',
            status="error",
            error="provider timeout",
        )
        record_outreach(
            current,
            vendor_id=winner,
            email="found@example.com",
            subject="Hello",
            transport="gmail",
            status="failed",
            error="mailbox full",
        )


def test_do_not_mail_is_saved_from_the_award_page(tmp_path):
    from pipeline_core.models import Vendor

    path = tmp_path / "pipeline.sqlite3"
    _seed(path, json_path="")
    with session(engine(path)) as current:
        vendor_id = current.query(Vendor).filter_by(name_raw="Large Electricals").one().vendor_id
    client = TestClient(create_app(path))
    page = client.get("/award?tender_id=LARGE&bid=1")
    assert "Do not mail this company" in page.text

    saved = client.post(
        f"/vendor/{vendor_id}/do-not-mail",
        data={"do_not_mail": "1", "back": "/award?tender_id=LARGE&bid=1"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    with session(engine(path)) as current:
        assert current.get(Vendor, vendor_id).do_not_mail

    cleared = client.post(
        f"/vendor/{vendor_id}/do-not-mail",
        data={"back": "/award?tender_id=LARGE&bid=1"},
        follow_redirects=False,
    )
    assert cleared.status_code == 303
    with session(engine(path)) as current:
        assert current.get(Vendor, vendor_id).do_not_mail is None


def test_award_page_names_the_portal_search_that_found_it(tmp_path):
    from stage1_scrape.config import SEARCH_PAGE_URL

    path = tmp_path / "pipeline.sqlite3"
    _seed(path, json_path="")
    with session(engine(path)) as current:
        upsert_tender(
            current,
            tender_id="LARGE",
            scraped_at=utcnow(),
            origin="eprocure.gov.in",
            search_mode="awarded",
            search_fields='{"fromDate": "01/01/2026", "tenderStatus": "6"}',
            listing_page=2,
            listing_serial=14,
            scrape_job_id=7,
        )
    html = TestClient(create_app(path)).get("/award?tender_id=LARGE&bid=1").text

    assert "Where this came from" in html
    assert "<dd>eprocure.gov.in</dd>" in html
    assert SEARCH_PAGE_URL.replace("&", "&amp;") in html
    assert "Awarded (AOC)" in html
    assert "fromDate=01/01/2026, tenderStatus=6 (AOC)" in html
    assert "<dd>2</dd>" in html
    assert "<dd>14</dd>" in html
    assert '<a href="/jobs/7?award=1:LARGE">7</a>' in html


def test_award_page_says_when_the_source_was_not_recorded(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path, json_path="")
    client = TestClient(create_app(path))
    with session(engine(path)) as current:
        upsert_tender(current, tender_id="LARGE", scraped_at=utcnow(), origin="eprocure.gov.in")
    older = client.get("/award?tender_id=LARGE&bid=1").text
    with session(engine(path)) as current:
        upsert_tender(current, tender_id="LARGE", scraped_at=utcnow(), origin="manual")
    typed = client.get("/award?tender_id=LARGE&bid=1").text

    assert "<dd>eprocure.gov.in</dd>" in older
    assert "<dd>Not recorded</dd>" in older
    assert "Typed by hand on the Add a tender page." in typed


def test_award_page_shows_where_the_model_found_the_address(tmp_path):
    from pipeline_core.models import Vendor
    from pipeline_core.queries import mark_enriched, record_llm_run
    from sqlalchemy import select

    path = tmp_path / "pipeline.sqlite3"
    _seed(path, json_path="")
    with session(engine(path)) as current:
        vendor_id = current.scalar(select(Vendor.vendor_id).where(Vendor.name_raw == "Large Electricals"))
        run_id = record_llm_run(
            current,
            vendor_id=vendor_id,
            model="test-model",
            prompt_version="contact-v1",
            input_json="{}",
            output_json='{"email": "found@co.example", "source": "https://co.example/about"}',
            status="ok",
            job_id=9,
        )
        mark_enriched(
            current,
            vendor_id,
            email="found@co.example",
            phone=None,
            source_url="https://co.example/about",
            llm_run_id=run_id,
        )
        record_llm_run(
            current,
            vendor_id=vendor_id,
            model="test-model",
            prompt_version="contact-v1",
            input_json="{}",
            output_json='{"email": "rfi@ord.gov.in", "phone": "+918043867273,6957"}',
            status="ok",
            job_id=9,
        )
    html = TestClient(create_app(path)).get("/award?tender_id=LARGE&bid=1").text

    assert "found at https://co.example/about" in html
    assert 'href="/jobs/9?award=1:LARGE"' in html
    assert "Not kept:" in html
    assert "rfi@ord.gov.in (government or buyer address)" in html
    assert "6957 (not a phone number)" in html


def test_opened_award_fields_can_be_saved(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path, json_path="")
    client = TestClient(create_app(path))
    saved = client.post(
        "/award",
        data={
            "tender_id": "LARGE",
            "bid": "1",
            "back": "/",
            "title": "Rewritten title",
            "organisation": "New org",
            "status": "AOC",
            "contract_date": "21-Sep-2026",
            "contract_value": "INR 10",
            "contract_currency": "INR",
            "rank": "L1",
            "quoted_value": "100",
            "awarded_value": "90",
            "awarded_currency": "INR",
            "work_title": "Road work",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    page = client.get(saved.headers["location"]).text
    assert 'value="Rewritten title"' in page
    assert 'value="Road work"' in page
    assert "LARGE" in page


def test_row_links_to_the_award(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path, json_path="")
    html = TestClient(create_app(path)).get("/").text
    assert "tender_id=LARGE" in html
    assert "bid=1" in html
    assert ">LARGE</a>" in html
    assert 'class="open-row"' in html
    assert "tender_id=LARGE" in html.split('class="open-row"', 1)[1]


def test_detail_shows_tender_awards_model_call_and_mail(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path, json_path="")
    with session(engine(path)) as current:
        replace_tender_documents(
            current,
            "LARGE",
            [
                {
                    "filename": "work-order.pdf",
                    "emails": ["clue@example.com"],
                    "phones": [],
                    "gstins": ["27ABCDE1234F1Z5"],
                }
            ],
        )
    response = TestClient(create_app(path)).get("/award?tender_id=LARGE&bid=1")

    assert response.status_code == 200
    html = response.text
    assert "Large rooms" in html
    assert "Other Bidder Ltd" in html
    assert "provider timeout" in html
    assert "mailbox full" in html
    assert "found@example.com" in html
    assert "work-order.pdf" in html
    assert "27ABCDE1234F1Z5" in html
    assert "clue@example.com" in html
    assert "A mail run uses a valid PDF address first." in html
    assert "Download PDFs" in html
    assert "The files were not kept." in html


def test_tender_screen_counts_every_email(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    bind = engine(path)
    ensure_schema(bind)
    with session(bind) as current:
        vendor_id = upsert_vendor(current, name_raw="Two Addresses Ltd")
        upsert_tender(current, tender_id="TWO", title="Bridge", scraped_at=utcnow())
        replace_awards(
            current,
            "TWO",
            [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": "Two Addresses Ltd"}],
        )
        record_pdf_contacts(
            current,
            vendor_id,
            "TWO",
            ["one@roads.example", "two@roads.example"],
            ["9811111111"],
        )
    html = TestClient(create_app(path)).get("/award?tender_id=TWO&bid=1").text
    assert "2 valid email addresses for this company. 2 will be mailed for this award." in html
    assert "one@roads.example" in html
    assert "two@roads.example" in html
    assert "9811111111" in html


def test_unknown_award_is_not_found(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    response = TestClient(create_app(path)).get("/award?tender_id=MISSING&bid=1")
    assert response.status_code == 404
    assert "No award is stored" in response.text


def _two_tender_company(path):
    """The case from the field: S.K Sales won T-A (no email in its PDFs) and T-B (LOA had one)."""
    import json

    from pipeline_core.models import UiJob
    from pipeline_core.queries import mark_enriched, record_job_tender

    bind = engine(path)
    ensure_schema(bind)
    with session(bind) as current:
        for job_id, stage, params in (
            (9, "scrape", {}),
            (26, "enrich", {}),
            (40, "pdfs", {"tender_id": "T-A"}),
            (43, "outreach", {"awards": ["1:T-A"], "send": True}),
        ):
            current.add(UiJob(job_id=job_id, stage=stage, state="done", params_json=json.dumps(params),
                              log="", started_at=utcnow()))
        vendor_id = upsert_vendor(current, name_raw="S.K Sales Company")
        for tender_id in ("T-A", "T-B"):
            upsert_tender(current, tender_id=tender_id, title=f"Work {tender_id}", scraped_at=utcnow())
            replace_awards(
                current,
                tender_id,
                [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": "S.K Sales Company"}],
            )
        upsert_tender(
            current,
            tender_id="T-A",
            scraped_at=utcnow(),
            origin="eprocure.gov.in",
            search_mode="awarded",
            scrape_job_id=9,
        )
        record_job_tender(current, 9, "T-A")
        record_job_tender(current, 40, "T-A")
        replace_tender_documents(
            current,
            "T-A",
            [
                {"filename": "LOA1043.pdf", "emails": [], "phones": []},
                {"filename": "techsummary_873377.pdf", "emails": [], "phones": ["6877812012"]},
            ],
        )
        replace_tender_documents(
            current, "T-B", [{"filename": "LOA.pdf", "emails": ["-sksalescompany@gmail.com"]}]
        )
        record_pdf_contacts(current, vendor_id, "T-A", [], ["6877812012"])
        record_pdf_contacts(current, vendor_id, "T-B", ["-sksalescompany@gmail.com"], [])
        # The old company-wide column still holds the other tender's dash spelling.
        set_pdf_contacts(current, vendor_id, email="-sksalescompany@gmail.com", phone="6877812012")
        run_id = record_llm_run(
            current,
            vendor_id=vendor_id,
            model="m",
            prompt_version="v",
            input_json="{}",
            output_json='{"email": "sksalescompany@gmail.com"}',
            status="ok",
            job_id=26,
        )
        mark_enriched(
            current,
            vendor_id,
            email="sksalescompany@gmail.com",
            phone="+91 98154 31020",
            source_url="https://sjvn.nic.in/tender-awarded?page=38",
            llm_run_id=run_id,
        )
        record_outreach(
            current,
            vendor_id=vendor_id,
            tender_id="T-A",
            email="sksalescompany@gmail.com",
            subject="Hello",
            transport="gmail",
            status="failed",
            error="mailbox full",
            job_id=43,
        )
    return vendor_id


def _section(html: str, start: str, end: str) -> str:
    return html.split(start, 1)[1].split(end, 1)[0]


def test_pdf_evidence_shows_only_what_this_tenders_files_said(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _two_tender_company(path)
    html = TestClient(create_app(path)).get("/award?tender_id=T-A&bid=1").text

    assert "-sksalescompany" not in html
    clues = _section(html, 'id="tender-clues"', "Each file and what it said.")
    assert "PDF phone" in clues
    assert "6877812012" in clues
    assert "found in techsummary_873377.pdf" in clues
    assert "sksalescompany" not in clues
    assert "PDF email" not in clues

    others = _section(html, "Other PDFs for this company", '<h2 class="section">Addresses</h2>')
    assert 'href="/award?tender_id=T-B&bid=1"' in others
    assert "From tender T-B, file LOA.pdf: Email sksalescompany@gmail.com" in others


def test_a_tender_whose_pdfs_held_nothing_says_so(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _two_tender_company(path)
    with session(engine(path)) as current:
        from pipeline_core.queries import replace_pdf_contacts

        # The PDFs were read again and held nothing.
        replace_tender_documents(current, "T-A", [{"filename": "LOA1043.pdf"}])
        replace_pdf_contacts(current, 1, "T-A", [], [])
    html = TestClient(create_app(path)).get("/award?tender_id=T-A&bid=1").text
    assert "No email or phone was read from this tender's PDFs." in html


def test_addresses_show_every_email_its_source_and_whether_it_is_mailed(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _two_tender_company(path)
    html = TestClient(create_app(path)).get("/award?tender_id=T-A&bid=1").text

    table = _section(html, 'id="addresses"', "</table>")
    rows = table.split("<tr>")[2:]
    assert len(rows) == 4
    pdf, model, phone_pdf, phone_model = rows
    assert "sksalescompany@gmail.com" in pdf
    assert "pdf — LOA.pdf on tender T-B" in pdf
    assert "will be mailed" in pdf
    assert "model" in model
    assert "found at https://sjvn.nic.in/tender-awarded?page=38" in model
    assert 'href="/jobs/26?award=1:T-A">run 26</a>' in model
    assert "not mailed: same mailbox as an address above" in model
    assert "6877812012" in phone_pdf and "techsummary_873377.pdf on this tender" in phone_pdf
    assert "+91 98154 31020" in phone_model
    assert "1 valid email address for this company. 1 will be mailed for this award." in html


def test_runs_list_every_run_that_touched_the_record_and_open_on_it(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _two_tender_company(path)
    html = TestClient(create_app(path)).get("/award?tender_id=T-A&bid=1").text

    runs = _section(html, 'id="runs"', "</table>")
    for job_id in (43, 40, 26, 9):
        assert f'href="/jobs/{job_id}?award=1:T-A">{job_id}</a>' in runs
    assert runs.index("/jobs/43?") < runs.index("/jobs/40?") < runs.index("/jobs/26?") < runs.index("/jobs/9?")
    assert "Download PDFs" in runs
    assert "first saved this tender" in runs
    assert "sent mail for this award; failed sksalescompany@gmail.com" in runs
    # Where this came from: Run stays the first scrape, not the download.
    assert '<dd><a href="/jobs/9?award=1:T-A">9</a></dd>' in html
    attempts = _section(html, '<h2 class="section">Mail attempts</h2>', "</table>")
    assert 'href="/jobs/43?award=1:T-A">43</a>' in attempts

    other = TestClient(create_app(path)).get("/award?tender_id=T-B&bid=1").text
    runs_b = _section(other, 'id="runs"', "</table>")
    assert "/jobs/26?award=1:T-B" in runs_b
    assert "/jobs/40?" not in runs_b
    assert "/jobs/43?" not in runs_b


def test_the_grid_never_shows_another_tenders_pdf_email(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _two_tender_company(path)
    html = TestClient(create_app(path)).get("/").text
    assert "-sksalescompany" not in html
    row_a = html.split(">T-A</a>", 1)[1].split("</tr>", 1)[0]
    row_b = html.split(">T-B</a>", 1)[1].split("</tr>", 1)[0]
    assert 'data-col="pdf_email"></td>' in row_a
    assert 'data-col="pdf_email">sksalescompany@gmail.com</td>' in row_b
