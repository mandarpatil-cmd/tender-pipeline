"""Excel and CSV of the rows the screen is showing. A blank cell is NA."""

from __future__ import annotations

import csv
import io
from typing import Any

from openpyxl import Workbook
from pipeline_core.grid import COLUMNS

HEADERS = [label for _key, label, _group, _evidence in COLUMNS]
KEYS = [key for key, _label, _group, _evidence in COLUMNS]


def as_cell(value: Any) -> str:
    if value is None:
        return "NA"
    text = str(value).strip()
    return text if text else "NA"


def table(rows: list[dict]) -> list[list[str]]:
    return [[as_cell(row.get(key)) for key in KEYS] for row in rows]


def csv_bytes(rows: list[dict]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(HEADERS)
    writer.writerows(table(rows))
    return buffer.getvalue().encode("utf-8-sig")


def xlsx_bytes(rows: list[dict]) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "awards"
    sheet.append(HEADERS)
    for row in table(rows):
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()
