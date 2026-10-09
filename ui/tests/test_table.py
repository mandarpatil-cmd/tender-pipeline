"""The award table, its filter, and the export of that same filter."""

from __future__ import annotations

from datetime import date
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.grid import award_view
from pipeline_core.models import Tender
from pipeline_core.queries import (
    mark_enriched,
    mark_failed,
    record_outreach,
    replace_awards,
    replace_tender_documents,
    upsert_tender,
    upsert_vendor,
    utcnow,
)

from ops_ui.present import resolve_preset

from ops_ui.app import create_app


def _seed(path, json_path: str | None = None):
    bind = engine(path)
    ensure_schema(bind)
    with session(bind) as current:
        small = upsert_vendor(current, name_raw="Small Roads Ltd", state="Bihar")
        large = upsert_vendor(current, name_raw="Large Electricals", state="Assam")
        odd = upsert_vendor(current, name_raw="Odd Annex Works", state="Goa")
        upsert_tender(
            current,
            tender_id="SMALL",
            title="Small road",
            organisation="NHAI",
            status="AOC",
            contract_date="01-Jan-2020",
            contract_value="INR 10",
            scraped_at=utcnow(),
        )
        upsert_tender(
            current,
            tender_id="LARGE",
            title="Large rooms",
            organisation="NIT Durgapur",
            status="AOC",
            contract_date="21-Sep-2026",
            contract_value="INR 179,853.24",
            json_path=json_path,
            scraped_at=utcnow(),
        )
        upsert_tender(
            current,
            tender_id="ODD",
            title="Annex job",
            organisation="Port office",
            status="Retender",
            contract_date="whenever",
            contract_value="see annex",
            scraped_at=utcnow(),
        )
        for tender_id, vendor_id, name in (
            ("SMALL", small, "Small Roads Ltd"),
            ("LARGE", large, "Large Electricals"),
            ("ODD", odd, "Odd Annex Works"),
        ):
            replace_awards(
                current,
                tender_id,
                [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": name}],
            )


def _count(html: str, key: str) -> str:
    marker = f'data-count="{key}">'
    start = html.index(marker) + len(marker)
    return html[start : html.index("<", start)]


def test_filter_narrows_the_table_and_the_counts(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    response = TestClient(create_app(path)).get("/scrape?q=electrical")

    assert response.status_code == 200
    html = response.text
    assert ">LARGE</a>" in html
    assert "SMALL" not in html
    assert _count(html, "awards") == "1"
    assert _count(html, "vendors") == "1"


def test_evidence_comes_from_the_database(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    with session(engine(path)) as current:
        replace_tender_documents(
            current,
            "LARGE",
            [{"filename": "work-order.pdf", "gstins": ["27ABCDE1234F1Z5"]}],
        )
    html = TestClient(create_app(path)).get("/award?tender_id=LARGE&bid=1").text

    assert "27ABCDE1234F1Z5" in html
    assert "work-order.pdf" in html


def test_each_stage_offers_only_its_own_filters(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    client = TestClient(create_app(path))
    scrape = client.get("/scrape").text
    enrich = client.get("/enrich").text
    mail = client.get("/mail").text

    assert 'name="scraped_from"' in scrape
    assert 'name="scraped_to"' in scrape
    assert "Stored status" in scrape
    assert "Add a tender" in scrape
    assert "60 days before to" in scrape
    assert 'name="mailable"' not in scrape
    assert "Contract value min" in scrape
    assert 'name="organisation"' in scrape
    assert "Contract value min" not in enrich
    assert 'name="organisation"' not in enrich
    assert 'name="enrichment_status"' not in scrape
    assert 'name="enrichment_status"' in enrich
    assert 'name="source"' in enrich
    assert ">Enrich</button>" in enrich
    assert "Mail these" not in enrich
    assert "Look up contacts" in enrich
    assert "To send" in mail
    assert "Mail these" in mail
    assert 'aria-label="Select rows on this page"' in enrich
    assert 'name="sort"' in enrich
    assert "company name, city, state, email, or phone" in enrich
    assert "pin-name" not in enrich
    assert "Put failed companies back on the queue" not in scrape
    assert "Put failed companies back on the queue" not in enrich
    assert "Put not_found companies back on the queue" not in mail


def test_scraped_range_is_kept_on_the_export_link(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    html = TestClient(create_app(path)).get(
        "/scrape?scraped_from=1999-01-01&scraped_to=1999-01-02"
    ).text

    assert "No awards match." in html
    assert "scraped_from=1999-01-01" in html
    assert "scraped_to=1999-01-02" in html


def test_export_matches_the_filter_and_writes_na_for_blanks(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    client = TestClient(create_app(path))

    master = load_workbook(BytesIO(client.get("/export.xlsx").content))
    sheet = master.active
    assert sheet.max_row == 4
    assert sheet.max_column == 34
    headers = [cell.value for cell in sheet[1]]
    assert headers[0] == "Tender"
    assert "PDF email" in headers
    assert "GSTIN" in headers

    workbook = load_workbook(BytesIO(client.get("/scrape/export.xlsx?q=electrical").content))
    sheet = workbook.active
    assert sheet.max_row == 2
    assert sheet.max_column == 20
    headers = [cell.value for cell in sheet[1]]
    assert headers[0] == "Tender"
    assert "Contract value" in headers
    assert "Awarded" in headers
    assert "GSTIN" not in headers
    values = {headers[index]: sheet.cell(2, index + 1).value for index in range(len(headers))}
    assert values["Tender"] == "LARGE"
    assert values["Email"] == "NA"

    csv_text = client.get("/scrape/export.csv?q=electrical").content.decode("utf-8-sig")
    assert csv_text.splitlines()[1].startswith("LARGE,")
    assert "NA" in csv_text


_TENDERS = ("SMALL", "LARGE", "ODD", "FAIL")


def _rich(path):
    bind = engine(path)
    ensure_schema(bind)
    with session(bind) as current:
        small = upsert_vendor(current, name_raw="Small Roads Ltd", state="Bihar")
        large = upsert_vendor(current, name_raw="Large Electricals", state="West Bengal")
        odd = upsert_vendor(current, name_raw="Odd Annex Works", state="Assam", source="manual")
        failed = upsert_vendor(current, name_raw="Failed Civic", state="Goa", source="bideasy")
        rows = (
            ("SMALL", "Small road", "NHAI", "AOC", "01-Jan-2020", "INR 10", small, "Small Roads Ltd"),
            ("LARGE", "Large rooms", "NIT Durgapur", "AOC", "21-Sep-2026", "INR 179,853.24", large, "Large Electricals"),
            ("ODD", "Annex job", "Port office", "Retender", "whenever", "see annex", odd, "Odd Annex Works"),
            ("FAIL", "Civic hall", "City office", "AOC", "01-Jun-2024", "INR 50,000", failed, "Failed Civic"),
        )
        for tender_id, title, organisation, status, contract_date, contract_value, vendor_id, name in rows:
            upsert_tender(
                current,
                tender_id=tender_id,
                title=title,
                organisation=organisation,
                status=status,
                contract_date=contract_date,
                contract_value=contract_value,
                scraped_at=utcnow(),
            )
            replace_awards(
                current,
                tender_id,
                [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": name}],
            )
        mark_enriched(current, large, email="large@example.com", phone=None)
        mark_enriched(current, odd, email=None, phone=None)
        mark_failed(current, failed)
        record_outreach(
            current,
            vendor_id=large,
            email="large@example.com",
            subject="sent",
            transport="gmail",
            status="sent",
        )
        record_outreach(
            current,
            vendor_id=failed,
            email="failed@example.com",
            subject="failed",
            transport="gmail",
            status="failed",
        )
        current.get(Tender, "SMALL").scraped_at = "2020-01-15T08:00:00+00:00"
        current.get(Tender, "LARGE").scraped_at = "2026-09-21T08:00:00+00:00"
        current.get(Tender, "ODD").scraped_at = "2026-09-22T08:00:00+00:00"
        current.get(Tender, "FAIL").scraped_at = "2024-06-01T08:00:00+00:00"


def _shown(html: str) -> set[str]:
    return {tender_id for tender_id in _TENDERS if f">{tender_id}</a>" in html}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("/scrape?q=electrical", {"LARGE"}),
        ("/scrape?q=%25", set()),
        ("/scrape?tender_status=Retender", {"ODD"}),
        ("/scrape?scraped_from=2026-09-21&scraped_to=2026-09-21", {"LARGE"}),
        ("/scrape?enrichment_status=failed", {"SMALL", "LARGE", "ODD", "FAIL"}),
        ("/enrich", {"SMALL"}),
        ("/enrich?enrichment_status=pending", {"SMALL"}),
        ("/enrich?enrichment_status=done", {"LARGE"}),
        ("/enrich?enrichment_status=not_found", {"ODD"}),
        ("/enrich?enrichment_status=failed", {"FAIL"}),
        ("/enrich?enrichment_status=not_found&source=manual", {"ODD"}),
        ("/enrich?enrichment_status=failed&source=bideasy", {"FAIL"}),
        ("/enrich?source=scrape", {"SMALL"}),
        ("/enrich?tender_status=Retender", {"SMALL"}),
        ("/mail", set()),
        ("/mail?outreach_status=sent", {"LARGE"}),
        ("/mail?outreach_status=failed", {"FAIL"}),
        ("/mail?outreach_status=none", set()),
        ("/mail?mailable=no", set()),
    ],
)
def test_each_filter_shows_the_same_awards_the_query_returns(tmp_path, url, expected):
    path = tmp_path / "pipeline.sqlite3"
    _rich(path)
    html = TestClient(create_app(path)).get(url).text

    assert _shown(html) == expected
    assert _count(html, "awards") == str(len(expected))


def test_high_value_mail_list_is_the_filtered_companies(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    bind = engine(path)
    ensure_schema(bind)
    with session(bind) as current:
        large = upsert_vendor(current, name_raw="Large Electricals", state="Goa")
        small = upsert_vendor(current, name_raw="Small Roads Ltd", state="Bihar")
        odd = upsert_vendor(current, name_raw="Odd Annex Works", state="Assam")
        for tender_id, title, organisation, contract_date, contract_value, vendor_id, name in (
            ("LARGE", "Large rooms", "NIT Durgapur", "21-Sep-2026", "INR 179,853.24", large, "Large Electricals"),
            ("SMALL", "Small road", "NHAI", "01-Jan-2020", "INR 10", small, "Small Roads Ltd"),
            ("ODD", "Annex job", "Port office", "whenever", "see annex", odd, "Odd Annex Works"),
        ):
            upsert_tender(
                current,
                tender_id=tender_id,
                title=title,
                organisation=organisation,
                status="AOC",
                contract_date=contract_date,
                contract_value=contract_value,
                scraped_at=utcnow(),
            )
            replace_awards(
                current,
                tender_id,
                [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": name}],
            )
        mark_enriched(current, large, email="large@example.com", phone=None)
        mark_enriched(current, small, email="small@example.com", phone=None)
    html = TestClient(create_app(path)).post(
        "/mail/from-table",
        data={"select_all": "1", "value_min": "100000", "mailable": "yes"},
    ).text
    picked = html[html.index('id="picked"'):html.index("</ul>", html.index('id="picked"'))]

    assert "Large Electricals" in picked
    assert "Odd Annex Works" not in picked
    assert "Small Roads Ltd" not in picked


def test_stage_filter_is_still_there_after_leaving_the_table(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    client = TestClient(create_app(path))
    client.get("/enrich?enrichment_status=failed")
    saved = 'href="/enrich?enrichment_status=failed"'

    for path_ in ("/jobs", "/mail", "/"):
        page = client.get(path_)
        assert page.status_code == 200
        assert saved in page.text

    client.get("/enrich")
    assert "enrichment_status=failed" not in client.get("/jobs").text


def test_enrich_status_is_one_list_at_a_time(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _rich(path)
    client = TestClient(create_app(path))
    pending = client.get("/enrich").text
    assert _shown(pending) == {"SMALL"}
    assert _count(pending, "pending") == "1"
    missed = client.get("/enrich?enrichment_status=not_found").text
    assert _shown(missed) == {"ODD"}
    assert _count(missed, "not_found") == "1"
    assert "Put not_found companies back on the queue" not in missed
    assert 'value="lookup">Enrich</button>' in missed
    done = client.get("/enrich?enrichment_status=done").text
    assert _shown(done) == {"LARGE"}
    assert 'value="lookup"' not in done
    assert 'name="vendor_id"' not in done
    failed = client.get("/enrich?enrichment_status=failed").text
    assert _shown(failed) == {"FAIL"}
    assert 'value="lookup">Enrich</button>' in failed
    thead = failed.split("<thead>", 1)[1].split("</thead>", 1)[0]
    assert "<a " not in thead
    assert "pin-name" not in failed
    assert "class=\"tick\"" in failed
    assert 'name="sort"' in failed
    assert 'name="dir"' in failed
    assert "company name, city, state, email, or phone" in failed


def test_from_preset_is_measured_from_the_to_date():
    assert resolve_preset("", "2026-09-21", "60") == ("2026-07-23", "2026-09-21")
    assert resolve_preset("2020-01-01", "2026-09-21", "") == ("2020-01-01", "2026-09-21")
    assert resolve_preset("", "", "7", today=date(2026, 10, 7)) == ("2026-09-30", "2026-10-07")
    assert resolve_preset("2026-01-01", "2026-09-21", "any", to_mode="any") == ("", "")
    assert resolve_preset("", "", "60", to_mode="any", today=date(2026, 10, 7)) == (
        "2026-08-08",
        "2026-10-07",
    )


def test_home_holds_the_master_export_and_scrape_can_clear(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _rich(path)
    client = TestClient(create_app(path))
    home = client.get("/")
    assert 'href="/export.xlsx"' in home.text
    assert 'href="/export.csv"' in home.text
    assert _shown(home.text) == set()
    scrape = client.get("/scrape")
    assert "Clear" in scrape.text
    assert _shown(scrape.text) == {"SMALL", "LARGE", "ODD", "FAIL"}
    narrowed = client.get("/scrape?tender_status=Retender")
    assert _shown(narrowed.text) == {"ODD"}
    cleared = client.get("/scrape")
    assert _shown(cleared.text) == {"SMALL", "LARGE", "ODD", "FAIL"}


def test_company_name_search_skips_a_title_mention(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    bind = engine(path)
    ensure_schema(bind)
    with session(bind) as current:
        company = upsert_vendor(current, name_raw="APSARA INNOVATIONS PRIVATE LIMITED", city="Pune")
        other = upsert_vendor(current, name_raw="ANZEN PROJECTS PRIVATE LIMITED", city="Mumbai")
        upsert_tender(
            current,
            tender_id="NAME",
            title="Rooms",
            organisation="IIT",
            status="AOC",
            contract_date="01-Jan-2026",
            contract_value="INR 10",
            scraped_at=utcnow(),
        )
        upsert_tender(
            current,
            tender_id="TITLE",
            title="Works in the Apsara building",
            organisation="BARC",
            status="AOC",
            contract_date="01-Jan-2026",
            contract_value="INR 10",
            scraped_at=utcnow(),
        )
        replace_awards(
            current,
            "NAME",
            [{"bid_number": "1", "vendor_id": company, "bidder_name": "APSARA INNOVATIONS PRIVATE LIMITED"}],
        )
        replace_awards(
            current,
            "TITLE",
            [{"bid_number": "1", "vendor_id": other, "bidder_name": "ANZEN PROJECTS PRIVATE LIMITED"}],
        )
    client = TestClient(create_app(path))
    broad = client.get("/scrape?q=APSARA").text
    ignored = client.get("/scrape?q=APSARA&search_in=name").text
    assert ">NAME</a>" in broad and ">TITLE</a>" in broad
    assert ">NAME</a>" in ignored and ">TITLE</a>" in ignored


def test_combination_urls_show_the_same_tenders_as_the_query(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _rich(path)
    client = TestClient(create_app(path))
    from starlette.datastructures import QueryParams

    from ops_ui.present import stage_query

    cases = (
        ("scrape", "/scrape?q=road&tender_status=AOC"),
        ("enrich", "/enrich?enrichment_status=failed&source=bideasy&outreach_status=sent"),
        ("mail", "/mail?outreach_status=sent&enrichment_status=pending"),
    )
    for stage, url in cases:
        html = client.get(url).text
        query = stage_query(stage, QueryParams(url.split("?", 1)[1]))
        with session(engine(path)) as current:
            view = award_view(current, query, page_size=None)
        shown = {tender_id for tender_id in _TENDERS if f">{tender_id}</a>" in html}
        assert shown == {row["tender_id"] for row in view.rows}
        assert _count(html, "awards") == str(view.total)


def test_scrape_organisation_and_contract_value_match_the_query(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _rich(path)
    client = TestClient(create_app(path))
    from starlette.datastructures import QueryParams

    from ops_ui.present import stage_query

    cases = (
        ("scrape", "/scrape?organisation=Durgapur", {"LARGE"}),
        ("scrape", "/scrape?value_min=100000", {"LARGE"}),
        ("enrich", "/enrich?organisation=Durgapur&value_min=100000", {"SMALL"}),
        ("mail", "/mail?organisation=Durgapur&value_min=1", set()),
    )
    for stage, url, expected in cases:
        html = client.get(url).text
        query = stage_query(stage, QueryParams(url.split("?", 1)[1]))
        with session(engine(path)) as current:
            view = award_view(current, query, page_size=None)
        shown = {tender_id for tender_id in _TENDERS if f">{tender_id}</a>" in html}
        assert shown == expected
        assert shown == {row["tender_id"] for row in view.rows}


def test_failed_chip_matches_the_award_rows_including_tender_status(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    bind = engine(path)
    ensure_schema(bind)
    with session(bind) as current:
        twice = upsert_vendor(current, name_raw="Twice Failed Ltd")
        once = upsert_vendor(current, name_raw="Once Failed Ltd")
        for tender_id, vendor_id, name, status in (
            ("A", twice, "Twice Failed Ltd", "AOC"),
            ("B", twice, "Twice Failed Ltd", "Retender"),
            ("C", once, "Once Failed Ltd", "AOC"),
        ):
            upsert_tender(
                current,
                tender_id=tender_id,
                title=name,
                organisation="Office",
                status=status,
                contract_date="01-Jan-2026",
                contract_value="INR 10",
                scraped_at=utcnow(),
            )
            replace_awards(
                current,
                tender_id,
                [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": name}],
            )
        mark_failed(current, twice)
        mark_failed(current, once)
    client = TestClient(create_app(path))
    failed = client.get("/enrich?enrichment_status=failed").text
    assert _count(failed, "failed") == _count(failed, "awards") == "3"
    ignored = client.get("/enrich?enrichment_status=failed&tender_status=AOC").text
    assert _count(ignored, "failed") == _count(ignored, "awards") == "3"
    aoc = client.get("/scrape?tender_status=AOC").text
    assert _count(aoc, "awards") == "2"
    assert ">B</a>" not in aoc
