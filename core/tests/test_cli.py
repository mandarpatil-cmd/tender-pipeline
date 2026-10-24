"""`pipeline-db`: what each command changes, what it refuses, and its exit code."""

from __future__ import annotations

from pipeline_core.cli import main
from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.models import SOURCE_BIDEASY, SOURCE_SCRAPE, Vendor
from pipeline_core.queries import mark_enriched, upsert_vendor


def _companies(path) -> list[str]:
    with session(engine(path)) as current:
        return sorted(v.name_raw for v in current.query(Vendor).all())


def test_prune_deletes_imported_companies_that_have_an_email(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    with session(engine(path)) as current:
        upsert_vendor(current, name_raw="Scraped Traders", source=SOURCE_SCRAPE)
        imported = upsert_vendor(current, name_raw="Imported Works", source=SOURCE_BIDEASY)
        mark_enriched(current, imported, email="hi@imported.example", phone=None)

    assert main(["--db", str(path), "prune", "--source", SOURCE_BIDEASY, "--yes"]) == 0
    assert _companies(path) == ["Scraped Traders"]
