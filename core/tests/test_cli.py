"""`pipeline-db`: what each command changes, what it refuses, and its exit code."""

from __future__ import annotations

import pytest

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


def _seed_company(path, name="Scraped Traders", source=SOURCE_SCRAPE) -> int:
    ensure_schema(engine(path))
    with session(engine(path)) as current:
        return upsert_vendor(current, name_raw=name, source=source)


def test_init_creates_the_database_and_running_it_again_keeps_the_rows(tmp_path, capsys):
    path = tmp_path / "pipeline.sqlite3"

    assert main(["--db", str(path), "init"]) == 0
    assert path.is_file()
    _seed_company(path)
    assert main(["--db", str(path), "init"]) == 0

    assert "already up to date" in capsys.readouterr().out
    assert _companies(path) == ["Scraped Traders"]


def test_status_reports_and_changes_nothing(tmp_path, capsys):
    from pipeline_core.db import table_counts

    path = tmp_path / "pipeline.sqlite3"
    _seed_company(path)
    before = table_counts(engine(path))

    assert main(["--db", str(path), "status"]) == 0

    assert table_counts(engine(path)) == before
    assert "pending" in capsys.readouterr().out


def test_status_without_a_database_says_to_init(tmp_path, capsys):
    path = tmp_path / "missing.sqlite3"

    assert main(["--db", str(path), "status"]) == 1

    assert "pipeline-db init" in capsys.readouterr().err
    assert not path.exists()


@pytest.mark.parametrize("command", [["reset"], ["prune", "--source", SOURCE_SCRAPE]])
def test_a_delete_refuses_without_a_terminal(tmp_path, monkeypatch, command):
    path = tmp_path / "pipeline.sqlite3"
    _seed_company(path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert main(["--db", str(path), *command]) == 1
    assert _companies(path) == ["Scraped Traders"]


@pytest.mark.parametrize("command", [["reset"], ["prune", "--source", SOURCE_SCRAPE]])
def test_a_delete_is_cancelled_unless_its_word_is_typed(tmp_path, monkeypatch, command):
    path = tmp_path / "pipeline.sqlite3"
    _seed_company(path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")

    assert main(["--db", str(path), *command]) == 1
    assert _companies(path) == ["Scraped Traders"]


def test_reset_empties_every_table_once_confirmed(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.sqlite3"
    _seed_company(path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "reset")

    assert main(["--db", str(path), "reset"]) == 0
    assert _companies(path) == []


def test_prune_refuses_companies_that_won_a_tender(tmp_path, capsys):
    from pipeline_core.queries import replace_awards, upsert_tender, utcnow

    path = tmp_path / "pipeline.sqlite3"
    vendor_id = _seed_company(path, "Imported Winner", SOURCE_BIDEASY)
    with session(engine(path)) as current:
        upsert_tender(current, tender_id="T-1", title="Work", scraped_at=utcnow())
        replace_awards(current, "T-1", [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": "x"}])

    assert main(["--db", str(path), "prune", "--source", SOURCE_BIDEASY, "--yes"]) == 1

    assert "awards" in capsys.readouterr().err
    assert _companies(path) == ["Imported Winner"]


def test_import_xlsx_dry_run_writes_nothing_and_a_real_run_adds_the_companies(tmp_path):
    pd = pytest.importorskip("pandas")
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    sheet = tmp_path / "bidders.xlsx"
    pd.DataFrame({"Probable Bidder": ["Acme Infra Pvt Ltd", "Bravo Traders"]}).to_excel(sheet, index=False)

    assert main(["--db", str(path), "import-xlsx", str(sheet), "--dry-run"]) == 0
    assert _companies(path) == []
    assert main(["--db", str(path), "import-xlsx", str(sheet)]) == 0
    assert _companies(path) == ["Acme Infra Pvt Ltd", "Bravo Traders"]


def test_import_xlsx_reports_a_file_it_cannot_read(tmp_path, capsys):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    broken = tmp_path / "broken.xlsx"
    broken.write_bytes(b"not a workbook")

    assert main(["--db", str(path), "import-xlsx", str(broken)]) == 1
    assert "FAILED" in capsys.readouterr().err


def test_import_xlsx_needs_a_database_first(tmp_path):
    assert main(["--db", str(tmp_path / "missing.sqlite3"), "import-xlsx", "x.xlsx"]) == 1
