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
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_DELAY_SECONDS,
    DEFAULT_HEADERS,
    DEFAULT_READ_TIMEOUT_SECONDS,
    SEARCH_PAGE_URL,
)
from stage1_scrape.domain.errors import ScraperError

log = logging.getLogger(__name__)

#: A dropped DNS lookup or a stalled socket is retried. An HTTP error is not.
_TRANSIENT_ATTEMPTS = 3


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


class GePNICClient:
    """Cookie-aware HTTP client for the Tapestry GePNIC front-end."""

    def __init__(
        self,
        delay: float = DEFAULT_DELAY_SECONDS,
        timeout: tuple[float, float] = (
            DEFAULT_CONNECT_TIMEOUT_SECONDS,
            DEFAULT_READ_TIMEOUT_SECONDS,
        ),
    ) -> None:
        self.delay = delay
        # (connect, read). Read is the budget with no bytes on the socket.
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self._last_url = SEARCH_PAGE_URL

    def _pause(self) -> None:
        if self.delay <= 0:
            return
        jitter = random.uniform(0, min(0.6, self.delay * 0.3))
        time.sleep(self.delay + jitter)

    def _drop_connections(self) -> None:
        """Throw away pooled sockets. A stalled read must not be reused."""
        try:
            self.session.close()
        except Exception:
            log.debug("Could not close the HTTP connection pool", exc_info=True)

    def _send(self, verb: str, url: str, call):
        """Run one HTTP call. A connection, DNS, or read stall is tried again."""
        last: Exception | None = None
        for attempt in range(1, _TRANSIENT_ATTEMPTS + 1):
            try:
                return call()
            except requests.RequestException as exc:
                last = exc
                if attempt >= _TRANSIENT_ATTEMPTS or not _is_transient(exc):
                    raise ScraperError(f"{verb} failed for {url}: {exc}") from exc
                self._drop_connections()
                wait = 2 * attempt
                log.warning(
                    "%s failed (%s/%s). Waiting %ss, then retrying. %s",
                    verb,
                    attempt,
                    _TRANSIENT_ATTEMPTS,
                    wait,
                    exc,
                )
                time.sleep(wait)
        raise ScraperError(f"{verb} failed for {url}: {last}")

    def get(self, url: str, referer: str | None = None) -> requests.Response:
        self._pause()
        headers = {"Referer": referer or self._last_url}
        log.debug("GET %s", url)
        response = self._send(
            "GET",
            url,
            lambda: self.session.get(url, headers=headers, timeout=self.timeout),
        )
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
        response = self._send(
            "POST",
            url,
            lambda: self.session.post(
                url,
                data=payload,
                headers=headers,
                timeout=self.timeout,
            ),
        )
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
