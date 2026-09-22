from __future__ import annotations

import base64
import logging
import os
import re
from pathlib import Path

from stage1_scrape.domain.errors import CaptchaError
from stage1_scrape.scraping.forms import parse_html

log = logging.getLogger(__name__)

DATA_URI_RE = re.compile(
    r"^data:image/(?P<kind>png|jpeg|jpg|gif);base64,(?P<data>.+)$",
    re.I | re.S,
)


def extract_captcha_bytes(html: str) -> tuple[bytes, str]:
    """Return (image_bytes, extension) from ``img#captchaImage``.

    Live page CONFIRMED: the image is an inline ``data:image/png;base64,...``
    URI. ``captchaText`` is maxlength=6 — this is not ``tokenSecret``.
    """
    soup = parse_html(html)
    image = soup.find("img", id="captchaImage")
    if image is None:
        image = soup.find("img", attrs={"name": "captchaImage"})
    if image is None:
        raise CaptchaError("Captcha image (#captchaImage) was not found on the search page.")

    src = (image.get("src") or "").strip()
    match = DATA_URI_RE.match(src)
    if not match:
        raise CaptchaError(
            "Captcha image src is not an inline data URI. "
            "The portal markup may have changed."
        )
    payload = re.sub(r"\s+", "", match.group("data"))
    try:
        raw = base64.b64decode(payload)
    except Exception as exc:
        raise CaptchaError(f"Could not decode captcha image: {exc}") from exc
    kind = match.group("kind").lower()
    extension = "jpg" if kind in {"jpeg", "jpg"} else kind
    return raw, extension


def save_captcha(html: str, dest: Path) -> Path:
    raw, extension = extract_captcha_bytes(html)
    dest = dest.with_suffix(f".{extension}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    log.info("Wrote captcha image to %s (%s bytes)", dest, len(raw))
    return dest


def prompt_captcha(image_path: Path) -> str:
    """Open the captcha image (Windows) and ask the operator to type it."""
    try:
        os.startfile(image_path)  # type: ignore[attr-defined]
    except Exception:
        log.info("Open this file and type the 6 characters: %s", image_path.resolve())
    print(f"\nCaptcha image: {image_path.resolve()}")
    value = input("Enter the 6-character captcha: ").strip()
    if len(value) != 6:
        raise CaptchaError(
            f"Captcha must be exactly 6 characters (portal validation). Got {len(value)!r}."
        )
    return value


def parse_captcha_option(
    value: str | None,
    *,
    default_solver: str = "manual",
) -> tuple[str | None, str]:
    """Return (explicit_code_or_none, solver). solver is manual or openrouter."""
    if value is None or not str(value).strip():
        return None, default_solver
    text = str(value).strip()
    lowered = text.lower()
    if lowered == "auto":
        return None, "openrouter"
    if lowered == "manual":
        return None, "manual"
    if len(text) != 6:
        raise CaptchaError(
            "Captcha must be exactly 6 characters, or 'auto' / 'manual'."
        )
    return text, default_solver


def resolve_captcha_code(
    image_path: Path,
    captcha_text: str | None,
    *,
    solver: str = "manual",
) -> str:
    """Use typed text, OpenRouter OCR, or a human prompt."""
    if captcha_text:
        if len(captcha_text) != 6:
            raise CaptchaError(
                f"Captcha must be exactly 6 characters. Got {len(captcha_text)!r}."
            )
        return captcha_text
    if solver == "openrouter":
        from stage1_scrape.scraping.ocr import read_captcha_image

        return read_captcha_image(image_path)
    return prompt_captcha(image_path)
