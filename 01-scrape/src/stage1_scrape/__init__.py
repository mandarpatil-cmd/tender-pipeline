"""Scrape Award of Contract (AOC) records from eprocure.gov.in."""

from typing import TYPE_CHECKING

__all__ = ["scrape_aoc"]
__version__ = "0.1.0"

if TYPE_CHECKING:  # pragma: no cover - for type checkers only
    from .app.pipeline import scrape_aoc


def __getattr__(name: str):
    """Resolve `scrape_aoc` on first use (PEP 562).

    Keeping it lazy means `import stage1_scrape` does not pull in the whole
    scraping stack, and a missing optional dependency only bites the caller who
    actually scrapes.
    """
    if name == "scrape_aoc":
        from .app.pipeline import scrape_aoc

        return scrape_aoc
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
