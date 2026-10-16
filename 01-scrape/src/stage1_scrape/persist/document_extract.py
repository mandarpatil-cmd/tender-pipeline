"""Turn stored PDF text into contact clues. One model call per document."""

from __future__ import annotations

import json
import logging
from typing import Any

import requests
from pydantic import BaseModel, Field

from pipeline_core.emailcheck import usable_contact
from stage1_scrape.settings import openrouter_endpoint, openrouter_ocr_model

log = logging.getLogger(__name__)

_BUYER_GSTIN = {"27AAATV9885C1Z2", "27AAATV9885C1ZZ"}
_PROMPT = (
    "Read this tender work-order text and extract the winning company's details. "
    "Reply with JSON only. Use null when a field is not in the text. "
    "Do not invent an email, phone, or GSTIN. "
    "evidence_quote is a short verbatim snippet from the text, or null."
)


class DocumentExtract(BaseModel):
    """Structured clues from one work order. Every field may be absent."""

    company_name: str | None = Field(default=None)
    email: str | None = Field(default=None)
    phone: str | None = Field(default=None)
    gstin: str | None = Field(default=None)
    city: str | None = Field(default=None)
    state: str | None = Field(default=None)
    evidence_quote: str | None = Field(default=None)


def clues_from_extract(parsed: DocumentExtract) -> dict[str, list[str]]:
    """Emails and phones that pass the existing checks, and a 15-character GSTIN."""
    verdict = usable_contact(parsed.email, parsed.phone)
    gstins: list[str] = []
    gstin = (parsed.gstin or "").strip().upper()
    if len(gstin) == 15 and gstin.isalnum() and gstin not in _BUYER_GSTIN:
        gstins.append(gstin)
    return {
        "emails": list(verdict.emails),
        "phones": list(verdict.phones),
        "gstins": gstins,
    }


def fetch_document_extract(text: str, text_source: str = "") -> DocumentExtract:
    """Ask OpenRouter for a DocumentExtract. Raises when the call or the JSON fails."""
    endpoint = openrouter_endpoint()
    if endpoint is None:
        raise RuntimeError("Missing OPENROUTER_API_KEY.")
    schema = DocumentExtract.model_json_schema()
    body: dict[str, Any] = {
        "model": openrouter_ocr_model(),
        "temperature": 0,
        "max_tokens": 800,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "DocumentExtract", "strict": True, "schema": schema},
        },
        "messages": [
            {"role": "system", "content": _PROMPT},
            {
                "role": "user",
                "content": f"Text source: {text_source or 'unknown'}\n\n{text}",
            },
        ],
    }
    response = requests.post(
        endpoint.chat_url,
        headers={
            "Authorization": f"Bearer {endpoint.api_key}",
            "Content-Type": "application/json",
            **endpoint.extra_headers,
        },
        json=body,
        timeout=60,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(
            str(item.get("text") or "") if isinstance(item, dict) else str(item)
            for item in content
        )
    return DocumentExtract.model_validate(json.loads(content))


def apply_document_extracts(
    extracts: list[dict[str, Any]],
    *,
    fetch=None,
) -> None:
    """Fill clue lists on each extract that has text. A failed call leaves them empty."""
    reader = fetch or fetch_document_extract
    for item in extracts:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        try:
            parsed = reader(text, item.get("text_source") or "")
            found = clues_from_extract(parsed)
        except Exception as exc:
            log.warning("Document extract failed for %s: %s", item.get("filename"), exc)
            continue
        item["emails"] = found["emails"]
        item["phones"] = found["phones"]
        item["gstins"] = found["gstins"]
