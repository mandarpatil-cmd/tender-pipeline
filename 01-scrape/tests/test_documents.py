from pathlib import Path
from unittest.mock import MagicMock

import pytest

from stage1_scrape.domain.errors import SessionExpiredError
from stage1_scrape.domain.models import DocumentLink
from stage1_scrape.persist.documents import download_document, safe_filename


def test_safe_filename_strips_path_chars():
    assert safe_filename("aoc:file/name?.pdf") == "aoc_file_name_.pdf"


def test_download_document_writes_pdf(tmp_path: Path):
    client = MagicMock()
    response = MagicMock()
    response.content = b"%PDF-1.4 fake"
    response.headers = {"Content-Type": "application/pdf"}
    client.get.return_value = response
    doc = DocumentLink(
        label="AOC document",
        filename="award.pdf",
        url="https://eprocure.gov.in/eprocure/app?component=$DirectLink_14",
    )
    path = download_document(client, doc, tmp_path)
    assert path.read_bytes().startswith(b"%PDF")
    assert path.name == "award.pdf"


def test_download_document_rejects_html_error_page(tmp_path: Path):
    client = MagicMock()
    response = MagicMock()
    response.content = b"<html>session expired</html>"
    response.headers = {"Content-Type": "text/html"}
    client.get.return_value = response
    doc = DocumentLink(label="AOC document", filename="award.pdf", url="https://example")
    with pytest.raises(SessionExpiredError):
        download_document(client, doc, tmp_path)
