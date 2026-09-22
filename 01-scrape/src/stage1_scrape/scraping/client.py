from __future__ import annotations

import logging
import pickle
import random
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import requests

from stage1_scrape.config import (
    APP_URL,
    DEFAULT_DELAY_SECONDS,
    DEFAULT_HEADERS,
    DEFAULT_TIMEOUT_SECONDS,
    SEARCH_PAGE_URL,
)
from stage1_scrape.domain.errors import ScraperError

log = logging.getLogger(__name__)


class GePNICClient:
    """Cookie-aware HTTP client for the Tapestry GePNIC front-end."""

    def __init__(
        self,
        delay: float = DEFAULT_DELAY_SECONDS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.delay = delay
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self._last_url = SEARCH_PAGE_URL

    def _pause(self) -> None:
        if self.delay <= 0:
            return
        jitter = random.uniform(0, min(0.6, self.delay * 0.3))
        time.sleep(self.delay + jitter)

    def get(self, url: str, referer: str | None = None) -> requests.Response:
        self._pause()
        headers = {"Referer": referer or self._last_url}
        log.debug("GET %s", url)
        try:
            response = self.session.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ScraperError(f"GET failed for {url}: {exc}") from exc
        response.raise_for_status()
        self._last_url = response.url
        return response

    def post(
        self,
        fields: Iterable[tuple[str, str]],
        url: str = APP_URL,
        referer: str | None = None,
    ) -> requests.Response:
        self._pause()
        payload = list(fields)
        headers = {
            "Referer": referer or self._last_url,
            "Origin": "https://eprocure.gov.in",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        log.debug("POST %s (%s fields)", url, len(payload))
        try:
            response = self.session.post(
                url,
                data=payload,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ScraperError(f"POST failed for {url}: {exc}") from exc
        response.raise_for_status()
        self._last_url = response.url
        return response

    def fetch_search_form(self) -> str:
        return self.get(SEARCH_PAGE_URL).text

    def absolute(self, href: str) -> str:
        return urljoin(APP_URL, href)

    def save_state(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            pickle.dumps(
                {
                    "cookies": requests.utils.dict_from_cookiejar(self.session.cookies),
                    "last_url": self._last_url,
                }
            )
        )
        log.debug("Saved session cookies to %s", path)

    def load_state(self, path: Path) -> None:
        state = pickle.loads(path.read_bytes())
        self.session.cookies.update(state.get("cookies") or {})
        self._last_url = state.get("last_url") or SEARCH_PAGE_URL
        log.debug("Loaded session cookies from %s", path)
