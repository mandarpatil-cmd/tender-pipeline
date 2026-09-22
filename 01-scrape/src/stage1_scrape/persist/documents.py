from __future__ import annotations

import logging
import re
from pathlib import Path

from stage1_scrape.config import PDF_MAGIC
from stage1_scrape.domain.errors import DocumentError, SessionExpiredError
from stage1_scrape.domain.models import DocumentLink
from stage1_scrape.scraping.client import GePNICClient

log = logging.getLogger(__name__)

UNSAFE_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str, fallback: str = "document.pdf") -> str:
    cleaned = UNSAFE_FS.sub("_", name).strip(" .")
    return cleaned or fallback


def download_document(
    client: GePNICClient,
    doc: DocumentLink,
    dest_dir: Path,
) -> Path:
    """GET the DirectLink. Confirm the body is a PDF before writing it.

    An expired ``sp=`` token often returns an HTML error page instead of bytes.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    response = client.get(doc.url)
    content_type = (response.headers.get("Content-Type") or "").lower()
    body = response.content
    is_pdf = body.startswith(PDF_MAGIC) or "pdf" in content_type
    if not is_pdf:
        snippet = body[:200].decode("utf-8", errors="replace")
        if "html" in content_type or snippet.lstrip().lower().startswith("<"):
            raise SessionExpiredError(
                f"Document URL for {doc.filename!r} returned HTML, not a PDF. "
                "The sp= token has likely expired."
            )
        raise DocumentError(
            f"Document URL for {doc.filename!r} did not look like a PDF "
            f"(content-type={content_type!r})."
        )
    path = dest_dir / safe_filename(doc.filename)
    path.write_bytes(body)
    log.info("Saved %s (%s bytes)", path, len(body))
    return path
