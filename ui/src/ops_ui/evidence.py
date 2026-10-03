"""GSTIN and PDF file names live on tender_documents, not in a table column of the grid."""

from __future__ import annotations

from sqlalchemy.orm import Session

from pipeline_core.queries import documents_for_tenders, split_clues


def fill_evidence(session: Session, rows: list[dict]) -> None:
    """Add ``gstin`` and ``pdf_files`` for the rows about to be shown or exported."""
    grouped = documents_for_tenders(
        session, [str(row.get("tender_id") or "") for row in rows]
    )
    for row in rows:
        docs = grouped.get(str(row.get("tender_id") or ""), [])
        row["gstin"], row["pdf_files"] = summarise(docs)


def read_documents(session: Session, tender_id: str) -> list[dict[str, str]]:
    """One entry per stored PDF: file name and the clues pulled from it."""
    return documents_for_tenders(session, [tender_id]).get(tender_id, [])


def summarise(documents: list[dict[str, str]]) -> tuple[str, str]:
    gstins: list[str] = []
    names: list[str] = []
    for item in documents:
        for gstin in split_clues(item.get("gstins")):
            if gstin not in gstins:
                gstins.append(gstin)
        filename = item.get("filename") or ""
        if filename and filename not in names:
            names.append(filename)
    return "; ".join(gstins), "; ".join(names)
