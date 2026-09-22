from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_env() -> None:
    """Load `.env` from the repo root. Uses python-dotenv when installed."""
    path = project_root() / ".env"
    try:
        from dotenv import load_dotenv
    except ImportError:
        _load_env_file(path)
        return
    load_dotenv(path)


def _load_env_file(path: Path) -> None:
    """Minimal KEY=VALUE loader. Does not override variables already in the environment."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def env(name: str, default: str = "") -> str:
    load_env()
    return (os.environ.get(name) or default).strip()
OPENROUTER_DEFAULT_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_OCR_DEFAULT_MODEL = "google/gemini-2.5-flash"


def openrouter_api_key() -> str:
    return env("OPENROUTER_API_KEY")


def openrouter_base_url() -> str:
    return env("OPENROUTER_BASE_URL", OPENROUTER_DEFAULT_BASE).rstrip("/")


def openrouter_ocr_model() -> str:
    """Vision model used to read the 6-character search captcha."""
    return env("OPENROUTER_OCR_MODEL", OPENROUTER_OCR_DEFAULT_MODEL)


@dataclass(frozen=True)
class OpenRouterEndpoint:
    """Where to send a chat-completions call, and who to say we are.

    Deliberately carries no model: the only caller is captcha OCR, which picks
    its model with `openrouter_ocr_model()`. The old `LlmSource.model` field
    looked shared but was never read here.
    """

    api_key: str
    base_url: str
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def chat_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"


def openrouter_endpoint() -> OpenRouterEndpoint | None:
    """None when no key is configured, so callers can give their own message."""
    key = openrouter_api_key()
    if not key:
        return None
    return OpenRouterEndpoint(
        api_key=key,
        base_url=openrouter_base_url(),
        extra_headers={
            "HTTP-Referer": env("OPENROUTER_HTTP_REFERER", "https://localhost"),
            "X-Title": env("OPENROUTER_APP_TITLE", "stage1-scrape"),
        },
    )
