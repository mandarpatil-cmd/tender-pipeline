from __future__ import annotations

import base64
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

import requests

from stage1_scrape.domain.errors import CaptchaError
from stage1_scrape.settings import (
    OpenRouterEndpoint,
    openrouter_endpoint,
    openrouter_ocr_model,
)

log = logging.getLogger(__name__)

_ALNUM = re.compile(r"[A-Za-z0-9]")
_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
# Survive NFKD; treat as the ASCII captcha glyph they were meant to be.
_LOOKALIKES = str.maketrans(
    {
        "ø": "o",
        "Ø": "O",
        "ł": "l",
        "Ł": "L",
        "đ": "d",
        "Ð": "D",
        "×": "x",
        "χ": "x",
        "ı": "i",
        "İ": "I",
    }
)

CAPTCHA_PROMPT = (
    "This image is a 6-character website captcha. "
    "Characters are only ASCII letters A-Z a-z and digits 0-9. "
    "Accents, dots, commas, and decoration are noise: fold them "
    "(ç→c, ä→a, ü→u, è→e) or ignore them. "
    "Preserve uppercase vs lowercase of the base letter. "
    "Reply with exactly 6 ASCII characters and nothing else."
)
REPAIR_PROMPT = (
    "That answer was not valid. Look at the image again. "
    "Reply with exactly 6 ASCII letters or digits [A-Za-z0-9], "
    "no accents, spaces, or punctuation."
)


def _fold_char(ch: str) -> str:
    folded = unicodedata.normalize("NFKD", ch).translate(_LOOKALIKES)
    return "".join(
        part for part in folded if not unicodedata.combining(part)
    )


def _fold_text(raw: str) -> str:
    return "".join(_fold_char(ch) for ch in (raw or ""))


def normalize_captcha_text(raw: str) -> str:
    """Fold accents/noise to ASCII and require exactly 6 A–Z / a–z / 0–9."""
    folded = _fold_text(raw)
    compact = "".join(_ALNUM.findall(folded))
    if len(compact) == 6:
        return compact
    spaced = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in folded)
    tokens = re.findall(r"(?<![A-Za-z0-9])[A-Za-z0-9]{6}(?![A-Za-z0-9])", spaced)
    if len(tokens) == 1:
        return tokens[0]
    raise CaptchaError(
        f"OCR did not return 6 characters (got {compact!r} from {raw!r})."
    )


def read_captcha_image(
    path: Path,
    *,
    model: str | None = None,
    timeout: float = 45.0,
) -> str:
    """Send a local captcha PNG to OpenRouter vision and return 6 characters."""
    endpoint = openrouter_endpoint()
    if endpoint is None:
        raise CaptchaError(
            "Missing OPENROUTER_API_KEY. Copy .env.example to .env and set the key."
        )
    path = Path(path)
    if not path.is_file():
        raise CaptchaError(f"Captcha image not found: {path}")
    chosen = model or openrouter_ocr_model()
    mime = _MIME.get(path.suffix.lower(), "image/png")
    data_url = f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
    text = _vision(endpoint, chosen, data_url, CAPTCHA_PROMPT, timeout)
    try:
        code = normalize_captcha_text(text)
    except CaptchaError:
        log.info("OCR answer %r was not 6 ASCII chars; asking the same image again", text)
        text = _vision(endpoint, chosen, data_url, REPAIR_PROMPT, timeout)
        code = normalize_captcha_text(text)
    log.info("OpenRouter OCR (%s) read 6-character captcha", chosen)
    return code


def _vision(
    endpoint: OpenRouterEndpoint,
    model: str,
    data_url: str,
    prompt: str,
    timeout: float,
) -> str:
    headers = {
        "Authorization": f"Bearer {endpoint.api_key}",
        "Content-Type": "application/json",
        **endpoint.extra_headers,
    }
    body: dict[str, Any] = {
        "model": model,
        "temperature": 0,
        "max_tokens": 256,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }
    try:
        response = requests.post(
            endpoint.chat_url,
            headers=headers,
            json=body,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise CaptchaError(f"OpenRouter OCR request failed: {exc}") from exc
    if response.status_code == 401:
        raise CaptchaError("OpenRouter rejected the API key (401).")
    if not response.ok:
        raise CaptchaError(
            f"OpenRouter OCR -> {response.status_code}: {response.text[:400]}"
        )
    try:
        message = response.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise CaptchaError(f"OpenRouter OCR returned no message content: {exc}") from exc
    return _message_text(message)


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in {None, "text"}:
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return str(content or "")
