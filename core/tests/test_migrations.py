"""An update changes a client's rows once, after a checked copy, and never on a plain read."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pipeline_core import migrations
from pipeline_core.cli import main
from pipeline_core.db import (
    BACKUP_KEEP,
    SchemaTooNew,
    backup_dir,
    dispose_engines,
    engine,
    ensure_schema,
    rolling_backup,
    session,
)
from pipeline_core.models import (
    CONTACT_MODEL,
    STATUS_DONE,
    STATUS_NOT_FOUND,
    Vendor,
    VendorContact,
)
from pipeline_core.queries import set_pdf_contacts, upsert_vendor

#: Tables a fix records itself in, so a dump of the rest is the client's data.
_BOOKKEEPING = {"data_migrations"}


def _dump(path: Path) -> dict[str, list[tuple]]:
    """Every row of every pipeline table, in a stable order."""
    con = sqlite3.connect(path)
    try:
        names = [
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            if row[0] not in _BOOKKEEPING
        ]
        return {
            name: sorted(con.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr)
            for name in sorted(names)
        }
    finally:
        con.close()


def _as_released_before(path: Path) -> None:
    """The file the client has today: no record of data fixes, version 0."""
    dispose_engines()
    con = sqlite3.connect(path)
    con.execute("DROP TABLE data_migrations")
    con.execute("PRAGMA user_version = 0")
    con.commit()
    con.close()


def _seed(path: Path) -> int:
    bind = engine(path)
    ensure_schema(bind)
    with session(bind) as current:
        vendor_id = upsert_vendor(current, name_raw="Kept Ltd")
        vendor = current.get(Vendor, vendor_id)
        vendor.email = "hi@kept.example"
        vendor.enrichment_status = STATUS_DONE
        vendor.contact_origin = CONTACT_MODEL
        # Left by an old scan: an edge dash that cleaning removes.
        current.add(
            VendorContact(
                vendor_id=vendor_id, channel="email", value="-hi@kept.example", source="pdf", valid=1
            )
        )
    dispose_engines()
    return vendor_id


def _copies(path: Path, label: str) -> list[Path]:
    folder = backup_dir(path)
    if not folder.is_dir():
        return []
    return sorted(
        item
        for item in folder.iterdir()
        if item.name.startswith(f"{path.name}.{label}-") and not item.name.endswith(("-wal", "-shm"))
    )


def test_an_up_to_date_database_is_never_rewritten_even_when_a_rule_changes(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    ensure_schema(engine(path))
    before = _dump(path)

    # A release that judges every address differently must not touch stored rows.
    monkeypatch.setattr("pipeline_core.queries.contact_is_valid", lambda channel, value: False)
    monkeypatch.setattr("pipeline_core.emailcheck.clean_email", lambda value: "changed")
    report = ensure_schema(engine(path))

    assert not report.changed
    assert report.backup == ""
    assert _dump(path) == before


def test_the_contact_fix_runs_once_and_is_recorded(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    vendor_id = _seed(path)
    _as_released_before(path)

    first = ensure_schema(engine(path))
    second = ensure_schema(engine(path))

    assert [line.split(" ")[0] for line in first.migrated] == ["2026-10-contacts-v1"]
    assert second.migrated == []
    con = sqlite3.connect(path)
    assert [row[0] for row in con.execute("SELECT migration_id FROM data_migrations")] == [
        "2026-10-contacts-v1"
    ]
    assert con.execute("PRAGMA user_version").fetchone() == (migrations.SCHEMA_VERSION,)
    values = con.execute(
        "SELECT value, source FROM vendor_contacts WHERE vendor_id = ? ORDER BY contact_id",
        (vendor_id,),
    ).fetchall()
    con.close()
    # Cleaned once. The company column holds the same mailbox, so it is not copied again.
    assert values == [("hi@kept.example", "pdf")]


def test_pending_work_takes_one_checked_copy_first_and_none_after(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    _as_released_before(path)
    untouched = _dump(path)

    report = ensure_schema(engine(path))

    copies = _copies(path, "auto")
    assert [str(item) for item in copies] == [report.backup]
    assert _dump(copies[0]) == untouched
    con = sqlite3.connect(copies[0])
    assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    con.close()
    assert ensure_schema(engine(path)).backup == ""
    assert len(_copies(path, "auto")) == 1


def test_a_brand_new_database_needs_no_copy(tmp_path):
    path = tmp_path / "pipeline.sqlite3"

    report = ensure_schema(engine(path))

    assert report.backup == ""
    assert not backup_dir(path).exists()


def test_a_fix_that_fails_leaves_no_record_and_runs_again(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    _as_released_before(path)

    def broken(current):
        raise RuntimeError("fix failed")

    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        (migrations.Migration("2026-10-contacts-v1", "broken", broken),),
    )
    with pytest.raises(RuntimeError, match="fix failed"):
        ensure_schema(engine(path))
    assert [item.migration_id for item in migrations.pending(engine(path))] == [
        "2026-10-contacts-v1"
    ]


def test_a_database_from_newer_code_is_refused_and_left_alone(tmp_path, capsys):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    con = sqlite3.connect(path)
    con.execute(f"PRAGMA user_version = {migrations.SCHEMA_VERSION + 1}")
    con.execute("DROP TABLE data_migrations")
    con.commit()
    con.close()
    before = _dump(path)

    with pytest.raises(SchemaTooNew):
        ensure_schema(engine(path))
    dispose_engines()
    assert main(["--db", str(path), "init"]) == 1

    assert "newer version" in capsys.readouterr().err
    assert _dump(path) == before
    assert not backup_dir(path).exists()


def test_rolling_copies_keep_the_newest_of_one_label_only(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    _seed(path)
    folder = backup_dir(path)
    folder.mkdir()
    other = folder / f"{path.name}.manual-20200101-000000"
    other.write_bytes(b"keep me")
    unrelated = folder / "notes.txt"
    unrelated.write_text("keep me too")

    made = [rolling_backup(path, "update") for _ in range(BACKUP_KEEP + 3)]

    kept = _copies(path, "update")
    assert len(kept) == BACKUP_KEEP
    assert set(kept) == set(made[-BACKUP_KEEP:])
    assert other.read_bytes() == b"keep me"
    assert unrelated.read_text() == "keep me too"


def test_backup_then_restore_puts_the_rows_back(tmp_path, capsys):
    path = tmp_path / "pipeline.sqlite3"
    vendor_id = _seed(path)
    ensure_schema(engine(path))
    dispose_engines()
    before = _dump(path)

    assert main(["--db", str(path), "backup", "--label", "update"]) == 0
    copy = _copies(path, "update")[0]
    with session(engine(path)) as current:
        current.get(Vendor, vendor_id).email = None
        current.query(VendorContact).delete()
    dispose_engines()

    assert main(["--db", str(path), "restore", str(copy)]) == 0

    assert _dump(path) == before
    replaced = [item for item in tmp_path.iterdir() if ".replaced-" in item.name]
    assert len(replaced) == 1
    assert _dump(replaced[0])["vendor_contacts"] == []
    assert str(copy) in capsys.readouterr().out


def test_backup_with_no_database_yet_says_so(tmp_path, capsys):
    path = tmp_path / "pipeline.sqlite3"

    assert main(["--db", str(path), "backup"]) == 0

    assert "nothing to back up" in capsys.readouterr().out
    assert not path.exists()


def test_recheck_apply_copies_the_database_and_lists_every_change(tmp_path, capsys):
    path = tmp_path / "pipeline.sqlite3"
    bind = engine(path)
    ensure_schema(bind)
    with session(bind) as current:
        vendor_id = upsert_vendor(current, name_raw="Model Gov")
        vendor = current.get(Vendor, vendor_id)
        vendor.email = "rfi@ord.gov.in"
        vendor.enrichment_status = STATUS_DONE
        vendor.contact_origin = CONTACT_MODEL
    dispose_engines()

    assert main(["--db", str(path), "recheck-contacts", "--apply"]) == 0

    copy = _copies(path, "before-recheck")
    assert len(copy) == 1
    con = sqlite3.connect(copy[0])
    assert con.execute("SELECT email FROM vendors").fetchone() == ("rfi@ord.gov.in",)
    con.close()
    listing = list(backup_dir(path).glob("recheck-*.txt"))
    assert len(listing) == 1
    text = listing[0].read_text(encoding="utf-8")
    assert "rfi@ord.gov.in" in text and str(copy[0]) in text
    with session(engine(path)) as current:
        assert current.get(Vendor, vendor_id).enrichment_status == STATUS_NOT_FOUND


def test_recheck_apply_with_nothing_to_change_makes_no_copy(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    dispose_engines()

    assert main(["--db", str(path), "recheck-contacts", "--apply"]) == 0

    assert _copies(path, "before-recheck") == []


def test_a_pdf_clue_reaches_vendor_contacts_without_a_later_backfill(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    bind = engine(path)
    ensure_schema(bind)
    with session(bind) as current:
        vendor_id = upsert_vendor(current, name_raw="Clue Ltd")
        set_pdf_contacts(current, vendor_id, email="clue@example.com", phone="9876543210")
        rows = {
            (row.channel, row.value, row.source)
            for row in current.query(VendorContact).filter_by(vendor_id=vendor_id)
        }
    assert rows == {("email", "clue@example.com", "pdf"), ("phone", "9876543210", "pdf")}
