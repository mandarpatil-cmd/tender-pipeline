"""`python main.py`: the preflight only stops a run that needs the key."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MAIN = Path(__file__).resolve().parents[1] / "main.py"


@pytest.fixture
def stage_main():
    spec = importlib.util.spec_from_file_location("scrape_main", MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_without_a_key_ocr_stops_the_run(stage_main, monkeypatch, capsys):
    monkeypatch.setattr(stage_main, "OCR_ATTEMPTS", 3)

    with pytest.raises(SystemExit) as stopped:
        stage_main.preflight()

    assert stopped.value.code == 1
    assert "OCR_ATTEMPTS = 0" in capsys.readouterr().out


def test_without_a_key_a_typed_captcha_run_goes_ahead(stage_main, monkeypatch, capsys):
    monkeypatch.setattr(stage_main, "OCR_ATTEMPTS", 0)

    stage_main.preflight()

    out = capsys.readouterr().out
    assert "type the captcha" in out
    assert "contact clues" in out


def test_with_a_key_the_preflight_names_the_model(stage_main, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setattr(stage_main, "OCR_ATTEMPTS", 3)

    stage_main.preflight()

    assert "OPENROUTER_API_KEY  ok" in capsys.readouterr().out


def test_a_portal_failure_ends_the_run_with_a_message_not_a_traceback(stage_main, monkeypatch, capsys):
    from stage1_scrape.domain.errors import ScraperError

    monkeypatch.setattr(stage_main, "preflight", lambda: None)

    def broken(out):
        raise ScraperError("GET failed for https://eprocure.gov.in: 503 Server Error")

    monkeypatch.setattr(stage_main, "scrape", broken)

    assert stage_main.main() == 1
    assert "Failed: GET failed" in capsys.readouterr().err
