"""GSTIN and PDF file names live in the tender JSON, not in a table column."""

from __future__ import annotations

import json
from pathlib import Path


def fill_evidence(rows: list[dict], root: Path) -> None:
    """Add ``gstin`` and ``pdf_files`` for the rows about to be shown or exported."""
    cache: dict[str, tuple[str, str]] = {}
    for row in rows:
        key = row.get("json_path") or ""
        if key not in cache:
            cache[key] = read_evidence(key, root)
        row["gstin"], row["pdf_files"] = cache[key]


def read_documents(json_path: str, root: Path) -> list[dict[str, str]]:
    """One entry per saved PDF extract: file name and the clues pulled from it."""
    path = _resolve(json_path, root)
    if path is None:
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    documents = []
    for item in data.get("pdf_extracts") or []:
        documents.append(
            {
                "filename": str(item.get("filename") or ""),
                "emails": "; ".join(str(value) for value in item.get("emails") or []),
                "phones": "; ".join(str(value) for value in item.get("phones") or []),
                "gstins": "; ".join(str(value) for value in item.get("gstins") or []),
            }
        )
    if documents:
        return documents
    return [
        {"filename": Path(str(raw)).name, "emails": "", "phones": "", "gstins": ""}
        for raw in data.get("downloaded_files") or []
        if Path(str(raw)).name
    ]


def read_evidence(json_path: str, root: Path) -> tuple[str, str]:
    path = _resolve(json_path, root)
    if path is None:
        return "", ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""

    gstins: list[str] = []
    names: list[str] = []
    for item in data.get("pdf_extracts") or []:
        for gstin in item.get("gstins") or []:
            if gstin and gstin not in gstins:
                gstins.append(str(gstin))
        filename = item.get("filename")
        if filename and filename not in names:
            names.append(str(filename))
    if not names:
        for raw in data.get("downloaded_files") or []:
            name = Path(str(raw)).name
            if name and name not in names:
                names.append(name)
    return "; ".join(gstins), "; ".join(names)


def _resolve(json_path: str, root: Path) -> Path | None:
    if not str(json_path).strip():
        return None
    raw = Path(json_path)
    options = [raw] if raw.is_absolute() else [root / "01-scrape" / raw, root / raw]
    for option in options:
        if option.is_file():
            return option
    return None
