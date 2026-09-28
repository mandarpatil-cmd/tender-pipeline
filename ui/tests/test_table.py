"""The award table, its filter, and the export of that same filter."""

from __future__ import annotations

import json
from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.queries import replace_awards, upsert_tender, upsert_vendor, utcnow

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


def test_unreadable_contract_value_stays_visible(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    html = TestClient(create_app(path)).get("/?value_min=100").text

    assert ">LARGE</a>" in html
    assert ">ODD</a>" in html
    assert "SMALL" not in html


def test_evidence_comes_from_the_tender_file(tmp_path):
    folder = tmp_path / "json"
    folder.mkdir()
    document = folder / "large.json"
    document.write_text(
        json.dumps(
            {
                "pdf_extracts": [
                    {"filename": "work-order.pdf", "gstins": ["27ABCDE1234F1Z5"]}
                ]
            }
        ),
        encoding="utf-8",
    )
    path = tmp_path / "pipeline.sqlite3"
    _seed(path, json_path=str(document))
    html = TestClient(create_app(path)).get("/?q=electrical").text

    assert "27ABCDE1234F1Z5" in html
    assert "work-order.pdf" in html


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
