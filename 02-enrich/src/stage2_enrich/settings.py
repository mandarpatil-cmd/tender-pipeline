"""Model configuration, read from this stage's own `.env`."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

#: Stamped on every `llm_runs` row. Bump it when the prompt changes, so old runs
#: stay attributable to the prompt that produced them.
PROMPT_VERSION = "contact-v1"

#: Cap on output tokens per call.
#:
#: Left unset, providers reserve the model's full ceiling (65536 on
#: gemini-3.1-pro-preview) against your balance, and OpenRouter rejects the
#: request with a 402 if you cannot afford that *reservation* -- even when the
#: real reply would cost a fraction of a cent. That is what the first live run
#: hit, with 64025 tokens of credit available.
#:
#: The answer itself, `{email, phone}`, is a hundred tokens at most. The headroom
#: is for reasoning models, which spend output tokens thinking before they
#: answer: too low a cap truncates them mid-thought and yields nothing. 2048 is
#: comfortable for that and still reserves 32x less than the default.
MAX_OUTPUT_TOKENS = 2048


#: Number of search results the web plugin may pull per call. OpenRouter bills
#: per result, so this is the main cost dial: it multiplies across every vendor.
WEB_SEARCH_RESULTS = 3


def api_key() -> str:
    return (os.getenv("OPENROUTER_API_KEY") or "").strip()


def model() -> str:
    return (os.getenv("MODEL") or "").strip()


def web_search() -> bool:
    """Let the model search the web before answering. On unless WEB_SEARCH=0.

    Without this the model answers from its weights alone, and a small regional
    contractor is simply not in them -- the first live run returned null for
    every vendor that had no work-order PDF to go on. Searching is what makes
    this stage able to answer at all, and it is billed per result on top of the
    model's own tokens.
    """
    raw = (os.getenv("WEB_SEARCH") or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def require_env() -> None:
    missing = [
        name
        for name, value in (("OPENROUTER_API_KEY", api_key()), ("MODEL", model()))
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"Missing {' and '.join(missing)} in {PROJECT_ROOT / '.env'}. "
            "Copy .env.example to .env and fill it in."
        )
