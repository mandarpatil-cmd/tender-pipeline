"""Every stage finds the same database without being told where it is."""

from __future__ import annotations

from pathlib import Path

from pipeline_core import settings

REPO = Path(__file__).resolve().parents[2]


def test_the_root_is_the_folder_that_holds_the_stages(monkeypatch):
    monkeypatch.delenv("PIPELINE_ROOT", raising=False)
    monkeypatch.delenv("PIPELINE_DB", raising=False)

    assert settings.project_root() == REPO
    assert settings.data_dir() == REPO / "data"
    assert settings.db_path() == REPO / "data" / "pipeline.sqlite3"


def test_pipeline_root_moves_the_data_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_ROOT", f"  {tmp_path}  ")
    monkeypatch.delenv("PIPELINE_DB", raising=False)

    assert settings.project_root() == tmp_path.resolve()
    assert settings.db_path() == tmp_path.resolve() / "data" / "pipeline.sqlite3"


def test_pipeline_db_names_the_file_whatever_the_root(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_ROOT", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("PIPELINE_DB", str(tmp_path / "scratch.sqlite3"))

    assert settings.db_path() == (tmp_path / "scratch.sqlite3").resolve()
