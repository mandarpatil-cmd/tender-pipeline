"""Shared schema and database access for the tender pipeline.

The three stages are separate projects with their own virtualenvs. This package
is the one thing they all install, and the only place the schema is defined.

    from pipeline_core.db import session
    from pipeline_core.queries import pending_vendors, mark_enriched

    with session() as s:
        for vendor in pending_vendors(s, limit=5):
            ...
"""

from __future__ import annotations

from .db import create_all, drop_all, engine, ensure_schema, session, table_counts
from .models import (
    ENRICHMENT_STATUSES,
    OUTREACH_FAILED,
    OUTREACH_SENT,
    SOURCE_BIDEASY,
    SOURCE_SCRAPE,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_NOT_FOUND,
    STATUS_PENDING,
    Award,
    Base,
    LlmRun,
    Outreach,
    Tender,
    Vendor,
)
from .naming import buyer_tail, infer_city_state, infer_legal_form, normalize_name

__version__ = "0.1.0"

__all__ = [
    "ENRICHMENT_STATUSES",
    "OUTREACH_FAILED",
    "OUTREACH_SENT",
    "SOURCE_BIDEASY",
    "SOURCE_SCRAPE",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_NOT_FOUND",
    "STATUS_PENDING",
    "Award",
    "Base",
    "LlmRun",
    "Outreach",
    "Tender",
    "Vendor",
    "buyer_tail",
    "create_all",
    "drop_all",
    "engine",
    "ensure_schema",
    "infer_city_state",
    "infer_legal_form",
    "normalize_name",
    "session",
    "table_counts",
    "__version__",
]
