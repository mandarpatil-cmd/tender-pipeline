from __future__ import annotations


class ScraperError(Exception):
    """Base error for the eprocure AOC scraper."""


class FormError(ScraperError):
    """Search form is missing or could not be parsed."""


class CaptchaError(ScraperError):
    """Captcha image missing, invalid length, or search rejected the code."""


class ParseError(ScraperError):
    """Expected markup was not found on a page."""


class DocumentError(ScraperError):
    """A document URL did not return PDF bytes."""


class SessionExpiredError(ScraperError):
    """Session token (`sp`) or cookie is no longer valid."""


class ExportError(ScraperError):
    """The vendor spreadsheet could not be written."""
