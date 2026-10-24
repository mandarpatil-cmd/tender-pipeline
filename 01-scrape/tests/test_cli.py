"""`python -m stage1_scrape`: flags reach the scrape, and a failure is an exit code."""

from __future__ import annotations

import json

import pytest

from stage1_scrape import cli


@pytest.fixture
def runs(monkeypatch):
    seen: list[dict] = []

    def run_scrape(out, **kwargs):
        seen.append({"out": out, **kwargs})
        return {"scraped": 1, "tender_ids": ["T-1"]}

    def scrape_aoc(**kwargs):
        seen.append(kwargs)
        return []

    monkeypatch.setattr("stage1_scrape.app.run.run_scrape", run_scrape)
    monkeypatch.setattr(cli, "scrape_aoc", scrape_aoc)
    return seen


def test_run_reads_the_captcha_by_ocr_and_prints_the_result(runs, tmp_path, capsys):
    assert cli.main(["run", "--out", str(tmp_path)]) == 0

    assert runs[0]["captcha_solver"] == "openrouter"
    assert runs[0]["max_tenders"] == 1
    assert json.loads(capsys.readouterr().out)["tender_ids"] == ["T-1"]


def test_flags_reach_the_scrape(runs, tmp_path):
    argv = [
        "run", "--out", str(tmp_path), "--captcha", "AB12CD", "--no-pdfs", "--refresh",
        "--max-pages", "3", "--max-tenders", "7", "--from-date", "01/07/2026", "--to-date", "31/07/2026",
    ]

    assert cli.main(argv) == 0

    seen = runs[0]
    assert seen["captcha_text"] == "AB12CD"
    assert seen["download_pdfs"] is False and seen["skip_known"] is False
    assert (seen["max_pages"], seen["max_tenders"]) == (3, 7)
    assert seen["extra_fields"]["fromDate"] == "01/07/2026"
    assert seen["extra_fields"]["toDate"] == "31/07/2026"


def test_scrape_defaults_to_typing_the_captcha(runs, tmp_path, capsys):
    assert cli.main(["scrape", "--out", str(tmp_path)]) == 0

    assert runs[0]["captcha_solver"] == "manual"
    assert "Saved 0 tender(s)" in capsys.readouterr().out


def test_a_bad_captcha_option_is_refused(runs, tmp_path):
    assert cli.main(["run", "--out", str(tmp_path), "--captcha", "toolong123"]) == 1
    assert runs == []


def test_a_failed_run_is_exit_code_one_with_the_reason(monkeypatch, tmp_path, capsys):
    def broken(out, **kwargs):
        raise RuntimeError("portal down")

    monkeypatch.setattr("stage1_scrape.app.run.run_scrape", broken)

    assert cli.main(["run", "--out", str(tmp_path)]) == 1
    assert "portal down" in capsys.readouterr().err
