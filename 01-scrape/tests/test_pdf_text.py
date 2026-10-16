from pathlib import Path

from stage1_scrape.persist.pdf_text import extract_pdf


class _Result:
    def __init__(self, text: str, pages: int) -> None:
        self.text = text
        self.total_pages = pages


class _Parser:
    options: dict = {}

    def __init__(self, **options) -> None:
        _Parser.options = options

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def parse(self, path: str) -> _Result:
        return _Result("page one\n" + ("word " * 3000), 4)


def test_liteparse_reads_every_page_at_300_dpi(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("liteparse.LiteParse", _Parser)
    path = tmp_path / "work-order.pdf"
    path.write_bytes(b"%PDF-1.4")

    extracted = extract_pdf(path)

    assert _Parser.options["ocr_enabled"] is True
    assert _Parser.options["dpi"] == 300
    assert "target_pages" not in _Parser.options
    assert extracted["error"] is None
    assert extracted["text_source"] == "liteparse"
    assert extracted["pages"] == 4
    assert extracted["char_count"] == len(extracted["text"])
    assert extracted["char_count"] > 8000
    assert extracted["emails"] == []


def test_a_failed_read_sets_an_error(tmp_path: Path, monkeypatch):
    class Broken(_Parser):
        def parse(self, path: str):
            raise RuntimeError("liteparse down")

    monkeypatch.setattr("liteparse.LiteParse", Broken)
    path = tmp_path / "work-order.pdf"
    path.write_bytes(b"%PDF-1.4")

    extracted = extract_pdf(path)

    assert "liteparse down" in extracted["error"]
    assert extracted["text"] == ""
    assert extracted["text_source"] == ""


def test_a_blank_read_is_an_error(tmp_path: Path, monkeypatch):
    class Empty(_Parser):
        def parse(self, path: str) -> _Result:
            return _Result("   ", 2)

    monkeypatch.setattr("liteparse.LiteParse", Empty)
    path = tmp_path / "blank.pdf"
    path.write_bytes(b"%PDF-1.4")

    extracted = extract_pdf(path)

    assert extracted["error"] == "No text in the PDF"
    assert extracted["pages"] == 2
    assert extracted["text"] == ""


def test_a_missing_file_is_an_error(tmp_path: Path):
    extracted = extract_pdf(tmp_path / "missing.pdf")
    assert extracted["error"]
    assert extracted["emails"] == []
