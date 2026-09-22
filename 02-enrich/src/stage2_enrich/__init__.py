"""Stage 2 of the tender pipeline: company name -> public email and phone.

Reads vendors the scraper left `enrichment_status = 'pending'`, asks a model for
each one's public contact details, and writes the answer straight back to the
same row. Every call is logged to `llm_runs`, success or failure.

    from stage2_enrich.runner import enrich_pending
    print(enrich_pending(limit=5).render())
"""

from __future__ import annotations

from .llm import ContactInfo, build_chain, fetch_contact
from .runner import Summary, enrich_one, enrich_pending, queue

__version__ = "0.1.0"

__all__ = [
    "ContactInfo",
    "Summary",
    "build_chain",
    "enrich_one",
    "enrich_pending",
    "fetch_contact",
    "queue",
    "__version__",
]
