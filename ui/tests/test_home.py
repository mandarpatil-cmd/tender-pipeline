"""The home page reads a throwaway database and never the real one."""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient
import pipeline_core.db as core_db
from pipeline_core.db import dispose_engines, engine, ensure_schema, session
from pipeline_core.queries import (
    mark_enriched,
    record_outreach,
    replace_awards,
    upsert_tender,
    upsert_vendor,
    utcnow,
)
from pipeline_core.models import OUTREACH_SENT

from ops_ui.app import HOST, create_app, main


def _seed(path):
    bind = engine(path)
    ensure_schema(bind)
    with session(bind) as current:
        emailed = upsert_vendor(current, name_raw="Has Email Ltd")
        phoned = upsert_vendor(current, name_raw="Phone Only Traders")
        upsert_vendor(current, name_raw="Still Waiting Works")
        upsert_tender(
            current,
            tender_id="T-1",
            title="Road works",
            organisation="NHAI",
            status="AOC",
            scraped_at=utcnow(),
        )
        replace_awards(
            current,
            "T-1",
            [
                {
                    "bid_number": "1",
                    "vendor_id": emailed,
                    "bidder_name": "Has Email Ltd",
                }
            ],
        )
        mark_enriched(current, emailed, email="a@b.example", phone=None)
        mark_enriched(current, phoned, email=None, phone="+919876543210")
        record_outreach(
            current,
            vendor_id=emailed,
            email="a@b.example",
            subject="hello",
            transport="gmail",
            status=OUTREACH_SENT,
        )


def _count(html: str, key: str) -> str:
    marker = f'data-count="{key}">'
    start = html.index(marker) + len(marker)
    return html[start : html.index("<", start)]


def test_home_counts_match_the_database(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    client = TestClient(create_app(path))

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert _count(html, "tenders") == "1"
    assert _count(html, "companies") == "3"
    assert _count(html, "waiting") == "1"
    assert _count(html, "to-mail") == "0"
    assert "award winners stored" in html
    assert "phone-only" not in html
    assert "can never be mailed" not in html
    assert 'data-count="enriched"' not in html
    assert 'data-count="awards"' not in html
    assert str(path) in html


def test_empty_database_shows_zeros(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    response = TestClient(create_app(path)).get("/")

    assert response.status_code == 200
    html = response.text
    for key in ("tenders", "companies", "waiting", "to-mail"):
        assert _count(html, key) == "0"
    assert 'data-count="phone-only"' not in html
    assert 'data-count="enriched"' not in html


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.sqlite3"
    response = TestClient(create_app(path)).get("/")

    assert response.status_code == 200
    assert "pipeline-db init" in response.text
    assert not path.exists()


def test_empty_file_is_left_empty(tmp_path):
    path = tmp_path / "empty.sqlite3"
    path.write_bytes(b"")

    response = TestClient(create_app(path)).get("/")

    assert response.status_code == 200
    assert "This file is empty" in response.text
    assert "pipeline-db init" in response.text
    assert 'data-count="tenders"' not in response.text
    assert path.read_bytes() == b""
    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-shm").exists()


def test_non_database_file_is_left_unchanged(tmp_path):
    path = tmp_path / "notes.sqlite3"
    original = b"not a database"
    path.write_bytes(original)

    response = TestClient(create_app(path)).get("/")

    assert response.status_code == 200
    assert "This file is not a database" in response.text
    assert "Remove it" in response.text
    assert path.read_bytes() == original
    assert not path.with_name(path.name + "-wal").exists()


def test_truncated_sqlite_is_left_unchanged(tmp_path):
    path = tmp_path / "broken.sqlite3"
    original = b"SQLite format 3\x00" + b"garbage"
    path.write_bytes(original)

    response = TestClient(create_app(path)).get("/")

    assert response.status_code == 200
    assert "could not be read as a database" in response.text
    assert "Nothing was changed" in response.text
    assert path.read_bytes() == original
    assert 'data-count="tenders"' not in response.text


def test_sqlite_without_pipeline_tables_asks_for_init(tmp_path):
    path = tmp_path / "bare.sqlite3"
    bare = sqlite3.connect(path)
    bare.execute("CREATE TABLE unrelated (id INTEGER)")
    bare.commit()
    bare.close()

    response = TestClient(create_app(path)).get("/")

    assert response.status_code == 200
    assert "no pipeline tables" in response.text
    assert "pipeline-db init" in response.text
    assert 'data-count="tenders"' not in response.text


def test_locked_database_says_it_is_busy(tmp_path, monkeypatch):
    real_create_engine = core_db.create_engine

    def create_engine_quickly(url, **kwargs):
        connect_args = dict(kwargs.pop("connect_args", {}))
        connect_args["timeout"] = 0.05
        return real_create_engine(url, connect_args=connect_args, **kwargs)

    monkeypatch.setattr(core_db, "create_engine", create_engine_quickly)
    monkeypatch.setattr(core_db, "BUSY_TIMEOUT_MS", 50)
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    dispose_engines()

    # WAL still lets a reader through a normal write lock. Exclusive mode does not.
    held = sqlite3.connect(path, timeout=1)
    held.execute("PRAGMA locking_mode=EXCLUSIVE")
    held.execute("BEGIN EXCLUSIVE")
    try:
        response = TestClient(create_app(path)).get("/")
    finally:
        held.rollback()
        held.close()

    assert response.status_code == 200
    assert "The database is busy" in response.text
    assert "pipeline-db init" not in response.text
    assert 'data-count="tenders"' not in response.text


def test_server_listens_on_this_machine_only(monkeypatch):
    seen = {}

    def fake_run(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs

    monkeypatch.setattr("ops_ui.app.uvicorn.run", fake_run)
    main()

    assert seen["kwargs"]["host"] == HOST == "127.0.0.1"
    assert seen["kwargs"]["port"] == 8000
