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


def test_extract_pdf_reads_text(tmp_path: Path):
    from pypdf import PdfWriter

    path = tmp_path / "wo.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(path)
    extracted = extract_pdf(path)
    assert extracted["filename"] == "wo.pdf"
    assert extracted["pages"] == 1
    assert extracted["error"] is None
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
