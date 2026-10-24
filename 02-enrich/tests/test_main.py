"""`python main.py`: a dry run spends nothing and changes nothing."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from pipeline_core.db import session
from pipeline_core.models import STATUS_FAILED, STATUS_NOT_FOUND, STATUS_PENDING, Vendor
from pipeline_core.queries import mark_enriched, mark_failed, upsert_vendor
from stage2_enrich.runner import Summary

MAIN = Path(__file__).resolve().parents[1] / "main.py"


@pytest.fixture
def stage_main(monkeypatch):
    spec = importlib.util.spec_from_file_location("enrich_main", MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ONLY_SOURCE", None)
    return module


def _statuses() -> dict[str, str]:
    with session() as current:
        return {v.name_raw: v.enrichment_status for v in current.query(Vendor).all()}


def _seed() -> None:
    with session() as current:
        upsert_vendor(current, name_raw="Waiting Works")
        mark_failed(current, upsert_vendor(current, name_raw="Failed Works"))
        mark_enriched(current, upsert_vendor(current, name_raw="Nothing Public"), email=None, phone=None)


def test_a_dry_run_with_retries_on_requeues_nothing(stage_main, monkeypatch, capsys):
    _seed()
    before = _statuses()
    assert before["Failed Works"] == STATUS_FAILED
    assert before["Nothing Public"] == STATUS_NOT_FOUND
    monkeypatch.setattr(stage_main, "DRY_RUN", True)
    monkeypatch.setattr(stage_main, "RETRY_FAILED", True)
    monkeypatch.setattr(stage_main, "RETRY_NOT_FOUND", True)

    assert stage_main.main() == 0

    assert _statuses() == before
    out = capsys.readouterr().out
    assert "1 failed vendor(s) would be requeued" in out
    assert "1 not_found vendor(s) would be requeued" in out
    assert "nothing sent, nothing spent" in out


def test_a_real_run_requeues_the_failed_before_it_starts(stage_main, monkeypatch):
    _seed()
    monkeypatch.setattr(stage_main, "DRY_RUN", False)
    monkeypatch.setattr(stage_main, "RETRY_FAILED", True)
    monkeypatch.setattr(stage_main, "RETRY_NOT_FOUND", False)
    monkeypatch.setattr(stage_main, "LIMIT", 0)
    looked_up = []
    monkeypatch.setattr(
        stage_main, "enrich_pending", lambda **kwargs: looked_up.append(kwargs) or Summary()
    )

    assert stage_main.main() == 0

    assert len(looked_up) == 1
    statuses = _statuses()
    assert statuses["Failed Works"] == STATUS_PENDING
    assert statuses["Nothing Public"] == STATUS_NOT_FOUND


def test_the_limit_is_only_called_reached_when_the_queue_fills_it(stage_main, monkeypatch, capsys):
    _seed()
    monkeypatch.setattr(stage_main, "DRY_RUN", True)
    monkeypatch.setattr(stage_main, "RETRY_FAILED", False)
    monkeypatch.setattr(stage_main, "LIMIT", 5)

    stage_main.main()

    out = capsys.readouterr().out
    assert "1 vendor(s) this run" in out
    assert "LIMIT reached" not in out
