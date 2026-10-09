from pathlib import Path

from stage1_scrape.persist.pdf_text import extract_pdf, harvest_contacts


def test_harvest_keeps_vendor_email_drops_vnit():
    text = (
        "To Sunrise Builders, Email : sunrisebuilders@example.com "
        "Mob.: 9123456780 also storesoffice@vnit.ac.in GSTIN 27ABCDE1234F1Z5"
    )
    found = harvest_contacts(text)
    assert found["emails"] == ["sunrisebuilders@example.com"]
    assert found["phones"] == ["9123456780"]
    assert found["gstins"] == ["27ABCDE1234F1Z5"]


def test_harvest_drops_ocr_buyer_email_keeps_spaced_vendor_email():
    text = (
        "Emqil: sioresofficer@vnii.oc.in INSTITUTE GST No. 27AAATV9885C1Z2 "
        "Mob No-8123456709 Lnrail id-acmctechworks0l @example.com"
    )
    found = harvest_contacts(text)
    assert found["emails"] == ["acmctechworks0l@example.com"]
    assert found["phones"] == ["8123456709"]
    assert found["gstins"] == []


def test_extract_pdf_reads_text(tmp_path: Path, monkeypatch):
    from pypdf import PdfWriter

    monkeypatch.setattr(
        "stage1_scrape.persist.pdf_text._ocr_pdf",
        lambda path: ("", 1),
    )
    path = tmp_path / "wo.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(path)
    extracted = extract_pdf(path)
    assert extracted["filename"] == "wo.pdf"
    assert extracted["pages"] == 1
    assert extracted["error"] is None
    assert extracted["emails"] == []


def test_ocr_text_is_harvested_and_skips_the_text_layer(tmp_path: Path, monkeypatch):
    def ocr(path):
        return (
            "To Sunrise Builders Email sunrisebuilders@example.com "
            "Mob.: 9123456780 storesoffice@vnit.ac.in "
            "GSTIN 27ABCDEI234F1Z5 GST 27AAATV9885C1Z2",
            4,
        )

    def embedded(path):
        raise AssertionError("pypdf should not run when OCR already found a contact")

    monkeypatch.setattr("stage1_scrape.persist.pdf_text._ocr_pdf", ocr)
    monkeypatch.setattr("stage1_scrape.persist.pdf_text._embedded_text", embedded)
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.4")

    extracted = extract_pdf(path)

    assert extracted["error"] is None
    assert extracted["pages"] == 4
    assert extracted["emails"] == ["sunrisebuilders@example.com"]
    assert extracted["phones"] == ["9123456780"]
    assert extracted["gstins"] == ["27ABCDE1234F1Z5"]


def test_a_failed_ocr_falls_back_to_the_text_layer(tmp_path: Path, monkeypatch):
    def ocr(path):
        raise RuntimeError("ocr down")

    def embedded(path):
        return ("Email sunrisebuilders@example.com Mob.: 9123456780", 2)

    monkeypatch.setattr("stage1_scrape.persist.pdf_text._ocr_pdf", ocr)
    monkeypatch.setattr("stage1_scrape.persist.pdf_text._embedded_text", embedded)
    path = tmp_path / "digital.pdf"
    path.write_bytes(b"%PDF-1.4")

    extracted = extract_pdf(path)

    assert extracted["error"] is None
    assert extracted["pages"] == 2
    assert extracted["emails"] == ["sunrisebuilders@example.com"]
    assert extracted["phones"] == ["9123456780"]


def test_both_readers_can_find_nothing_without_an_error(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "stage1_scrape.persist.pdf_text._ocr_pdf",
        lambda path: ("scanned page with no contact", 3),
    )
    monkeypatch.setattr(
        "stage1_scrape.persist.pdf_text._embedded_text",
        lambda path: ("", 3),
    )
    path = tmp_path / "empty.pdf"
    path.write_bytes(b"%PDF-1.4")

    extracted = extract_pdf(path)

    assert extracted["error"] is None
    assert extracted["emails"] == []
    assert extracted["phones"] == []
    assert extracted["gstins"] == []


def test_ocr_targets_cover_the_head_and_the_last_page():
    from stage1_scrape.persist.pdf_text import _ocr_targets

    assert _ocr_targets(0) is None
    assert _ocr_targets(6) is None
    assert _ocr_targets(12) == "1-5,12"


def test_a_missing_file_is_an_error(tmp_path: Path):
    extracted = extract_pdf(tmp_path / "missing.pdf")
    assert extracted["error"]
    assert extracted["emails"] == []


def test_extract_real_work_order_if_present():
    import pytest

    path = Path("data/pdfs/2026_WBNPI_895142_1/034WOTWOENERGYMETERFORNEWCRC.PDF")
    if not path.is_file():
        pytest.skip("scraped work-order PDF not in this workspace")
    extracted = extract_pdf(path)
    assert extracted["char_count"] > 100
    # This work order prints the winner's email and mobile.
    assert extracted["emails"]
    assert extracted["phones"]
