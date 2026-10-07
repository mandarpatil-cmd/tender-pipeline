"""The award table, its filter, and the export of that same filter."""

from __future__ import annotations

from datetime import date
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.grid import AwardQuery, award_view
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
    response = TestClient(create_app(path)).get("/?q=electrical")

    assert response.status_code == 200
    html = response.text
    assert ">LARGE</a>" in html
    assert "SMALL" not in html
    assert _count(html, "awards") == "1"
    assert _count(html, "vendors") == "1"
    assert "Counts match the rows in this filter." in html
    assert 'class="evidence"' in html


def test_high_value_hides_an_unreadable_amount(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    html = TestClient(create_app(path)).get("/?value_min=100").text

    assert ">LARGE</a>" in html
    assert ">ODD</a>" not in html
    assert "SMALL" not in html


def test_evidence_comes_from_the_database(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    with session(engine(path)) as current:
        replace_tender_documents(
            current,
            "LARGE",
            [{"filename": "work-order.pdf", "gstins": ["27ABCDE1234F1Z5"]}],
        )
    html = TestClient(create_app(path)).get("/?q=electrical").text

    assert "27ABCDE1234F1Z5" in html
    assert "work-order.pdf" in html


def test_filters_offer_mailable_state_and_order(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    html = TestClient(create_app(path)).get("/").text

    assert 'name="mailable"' in html
    assert "Maharashtra" in html
    assert "Ascending" in html
    assert "Descending" in html
    assert 'name="scraped_from"' in html
    assert 'name="scraped_to"' in html
    assert "All companies in this filter" in html
    assert "Mail these" in html
    assert ">Enrich</button>" in html
    assert "Look up contacts" not in html
    assert "Organisation" in html
    assert "Tender status" in html
    assert "Contract value min" in html
    assert "60 days before to" in html
    assert 'aria-label="Select rows on this page"' in html


def test_scraped_range_is_kept_on_the_export_link(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    html = TestClient(create_app(path)).get(
        "/?scraped_from=1999-01-01&scraped_to=1999-01-02"
    ).text

    assert "No awards match." in html
    assert "scraped_from=1999-01-01" in html
    assert "scraped_to=1999-01-02" in html


def test_export_matches_the_filter_and_writes_na_for_blanks(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    client = TestClient(create_app(path))

    workbook = load_workbook(BytesIO(client.get("/export.xlsx?q=electrical").content))
    sheet = workbook.active
    assert sheet.max_row == 2
    assert sheet.max_column == 33
    headers = [cell.value for cell in sheet[1]]
    assert headers[0] == "Tender"
    assert "PDF email" in headers
    values = {headers[index]: sheet.cell(2, index + 1).value for index in range(len(headers))}
    assert values["Tender"] == "LARGE"
    assert values["Email"] == "NA"
    assert values["GSTIN"] == "NA"

    csv_text = client.get("/export.csv?q=electrical").content.decode("utf-8-sig")
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
    ("query", "expected"),
    [
        ("q=electrical", {"LARGE"}),
        ("q=%25", set()),
        ("tender_status=Retender", {"ODD"}),
        ("enrichment_status=pending", {"SMALL"}),
        ("enrichment_status=done", {"LARGE"}),
        ("enrichment_status=not_found", {"ODD"}),
        ("enrichment_status=failed", {"FAIL"}),
        ("outreach_status=sent", {"LARGE"}),
        ("outreach_status=failed", {"FAIL"}),
        ("outreach_status=none", {"SMALL", "ODD"}),
        ("source=manual", {"ODD"}),
        ("source=bideasy", {"FAIL"}),
        ("source=scrape", {"SMALL", "LARGE"}),
        ("mailable=yes", {"LARGE"}),
        ("mailable=no", {"SMALL", "ODD", "FAIL"}),
        ("state=Bihar", {"SMALL"}),
        ("state=Bih", set()),
        ("organisation=Durgapur", {"LARGE"}),
        ("date_from=2026-01-01", {"LARGE", "ODD"}),
        ("date_to=2020-12-31", {"SMALL", "ODD"}),
        ("value_min=100000", {"LARGE"}),
        ("value_max=20", {"SMALL"}),
        ("value_min=100&mailable=yes", {"LARGE"}),
        ("enrichment_status=pending&mailable=yes", set()),
        ("enrichment_status=pending&outreach_status=sent", set()),
        ("enrichment_status=failed&outreach_status=failed", {"FAIL"}),
        ("enrichment_status=pending&enrichment_status=failed", {"SMALL", "FAIL"}),
        ("enrichment_status=pending&enrichment_status=not_found&enrichment_status=failed", {"SMALL", "ODD", "FAIL"}),
        ("scraped_from=2026-09-21&scraped_to=2026-09-21", {"LARGE"}),
        ("date_to=2026-09-21&date_from_preset=60", {"LARGE", "ODD"}),
    ],
)
def test_each_filter_shows_the_same_awards_the_query_returns(tmp_path, query, expected):
    path = tmp_path / "pipeline.sqlite3"
    _rich(path)
    html = TestClient(create_app(path)).get(f"/?{query}").text

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

    assert "Large Electricals" in html
    assert "Odd Annex Works" not in html
    assert "Small Roads Ltd" not in html


def test_awards_filter_is_still_there_after_leaving_the_table(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    client = TestClient(create_app(path))
    client.get("/?enrichment_status=pending&state=Bihar")
    saved = 'href="/?enrichment_status=pending&amp;state=Bihar"'

    for path_ in ("/jobs", "/mail", "/tender/new"):
        page = client.get(path_)
        assert page.status_code == 200
        assert saved in page.text

    client.get("/")
    assert saved not in client.get("/jobs").text
    home = client.get("/")
    assert 'value="pending" selected' not in home.text


def test_combined_enrichment_filter_shows_class_counts(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _rich(path)
    html = TestClient(create_app(path)).get(
        "/?enrichment_status=pending&enrichment_status=not_found"
    ).text

    assert _shown(html) == {"SMALL", "ODD"}
    assert _count(html, "pending") == "1"
    assert _count(html, "not_found") == "1"
    assert _count(html, "failed") == "0"
    assert _count(html, "awards") == "2"


def test_from_preset_is_measured_from_the_to_date():
    assert resolve_preset("", "2026-09-21", "60") == ("2026-07-23", "2026-09-21")
    assert resolve_preset("2020-01-01", "2026-09-21", "") == ("2020-01-01", "2026-09-21")
    assert resolve_preset("", "", "7", today=date(2026, 10, 7)) == ("2026-09-30", "2026-10-07")
    assert resolve_preset("2026-01-01", "2026-09-21", "any", to_mode="any") == ("", "")
    assert resolve_preset("", "", "60", to_mode="any", today=date(2026, 10, 7)) == (
        "2026-08-08",
        "2026-10-07",
    )


def test_any_enrichment_is_the_whole_table_and_filters_can_clear(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _rich(path)
    client = TestClient(create_app(path))
    home = client.get("/")
    assert "Clear filters" in home.text
    assert 'href="/"' in home.text
    assert "Search tender, company, city, email, or phone" in home.text
    assert 'data-enrichment-any' in home.text
    assert _shown(home.text) == {"SMALL", "LARGE", "ODD", "FAIL"}
    narrowed = client.get("/?enrichment_status=pending&enrichment_status=failed")
    assert _shown(narrowed.text) == {"SMALL", "FAIL"}
    opened = client.get("/?date_from_preset=any&date_to_mode=any")
    assert 'href="/export.xlsx"' in opened.text
    assert _shown(opened.text) == {"SMALL", "LARGE", "ODD", "FAIL"}
    cleared = client.get("/")
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
    broad = client.get("/?q=APSARA").text
    named = client.get("/?q=APSARA&search_in=name").text
    assert ">NAME</a>" in broad and ">TITLE</a>" in broad
    assert ">NAME</a>" in named
    assert ">TITLE</a>" not in named
    assert 'value="name" checked' in named


def test_combination_urls_show_the_same_tenders_as_the_query(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _rich(path)
    client = TestClient(create_app(path))
    cases = (
        ("value_min=100&mailable=yes", AwardQuery(value_min="100", mailable="yes")),
        (
            "q=roads&search_in=name&enrichment_status=pending",
            AwardQuery(text="roads", search_in=("name",), enrichment_status=("pending",)),
        ),
    )
    for query_string, query in cases:
        html = client.get(f"/?{query_string}").text
        with session(engine(path)) as current:
            view = award_view(current, query, page_size=None)
        shown = {tender_id for tender_id in _TENDERS if f">{tender_id}</a>" in html}
        assert shown == {row["tender_id"] for row in view.rows}
        assert _count(html, "awards") == str(view.total)
