"""Excel and CSV of the rows the screen is showing. A blank cell is NA."""

from __future__ import annotations

import csv
import io
from typing import Any

from openpyxl import Workbook
from pipeline_core.grid import COLUMNS

_BY_KEY = {key: label for key, label, _group, _evidence in COLUMNS}
HEADERS = [label for _key, label, _group, _evidence in COLUMNS]
KEYS = [key for key, _label, _group, _evidence in COLUMNS]


def as_cell(value: Any) -> str:
    if value is None:
        return "NA"
    text = str(value).strip()
    return text if text else "NA"


def layout(columns: tuple[str, ...] | None = None) -> tuple[list[str], list[str]]:
    """Headers and keys. ``None`` is every column, in the master order."""
    keys = list(KEYS if columns is None else columns)
    keys = [key for key in keys if key in _BY_KEY]
    return [_BY_KEY[key] for key in keys], keys


def table(rows: list[dict], columns: tuple[str, ...] | None = None) -> list[list[str]]:
    _headers, keys = layout(columns)
    return [[as_cell(row.get(key)) for key in keys] for row in rows]


def csv_bytes(rows: list[dict], columns: tuple[str, ...] | None = None) -> bytes:
    headers, _keys = layout(columns)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    writer.writerows(table(rows, columns))
    return buffer.getvalue().encode("utf-8-sig")


def xlsx_bytes(
    rows: list[dict],
    columns: tuple[str, ...] | None = None,
    sheet_title: str = "awards",
) -> bytes:
    headers, _keys = layout(columns)
    book = Workbook()
    sheet = book.active
    sheet.title = sheet_title
    sheet.append(headers)
    for row in table(rows, columns):
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()
