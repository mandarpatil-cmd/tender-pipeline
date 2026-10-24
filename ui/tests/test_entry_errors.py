"""Hand entry says what is wrong, keeps what was typed, and never stores half a tender."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.models import SOURCE_MANUAL, Award, Tender, Vendor, VendorContact
from pipeline_core.queries import replace_awards, upsert_tender, upsert_vendor, utcnow

from ops_ui.app import create_app


@pytest.fixture
def window(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    with session(engine(path)) as current:
        vendor_id = upsert_vendor(current, name_raw="Stored Works")
        upsert_tender(current, tender_id="T-1", title="Stored", scraped_at=utcnow())
        replace_awards(
            current, "T-1", [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": "Stored Works"}]
        )
    return TestClient(create_app(path)), path, vendor_id


def _count(path, model) -> int:
    with session(engine(path)) as current:
        return current.query(model).count()


def test_the_new_tender_page_opens_with_a_winner_row(window):
    client, _path, _vendor = window
    page = client.get("/tender/new")

    assert page.status_code == 200
    assert 'action="/tender/new"' in page.text
    assert 'name="winner_name"' in page.text


def test_no_company_keeps_what_was_typed_and_says_why(window):
    client, path, _vendor = window

    page = client.post("/tender/new", data={"tender_id": "NEW-1", "title": "Bridge repair", "winner_name": ""})

    assert page.status_code == 400
    assert "Type at least one company." in page.text
    assert 'value="Bridge repair"' in page.text
    assert _count(path, Tender) == 1


def test_two_winners_with_one_bid_number_are_refused(window):
    client, path, _vendor = window
    data = {"tender_id": "NEW-2", "winner_name": ["First Works", "Second Works"], "bid_number": ["7", "7"]}

    page = client.post("/tender/new", data=data)

    assert page.status_code == 400
    assert "Each winner needs its own bid number." in page.text
    assert _count(path, Tender) == 1


def test_several_winners_are_saved_as_several_awards_from_a_manual_source(window):
    client, path, _vendor = window
    # The page has one winner row; the route takes several, as a hand-built form can send.
    data = {
        "tender_id": "NEW-3",
        "title": "Two firms",
        "contract_value": "INR 2,50,000",
        "winner_name": ["First Works", "Second Works"],
        "bid_number": ["1", "2"],
        "awarded_value": ["125000", "125000"],
    }

    answer = client.post("/tender/new", data=data, follow_redirects=False)

    assert answer.status_code == 303
    assert answer.headers["location"] == "/?q=NEW-3"
    with session(engine(path)) as current:
        awards = current.query(Award).filter_by(tender_id="NEW-3").order_by(Award.bid_number).all()
        assert [a.bidder_name for a in awards] == ["First Works", "Second Works"]
        sources = {current.get(Vendor, a.vendor_id).source for a in awards}
        assert sources == {SOURCE_MANUAL}
    assert "NEW-3" in client.get("/?q=NEW-3").text


def test_a_tender_already_stored_is_a_409_that_links_to_it(window):
    client, path, _vendor = window

    page = client.post("/tender/new", data={"tender_id": "T-1", "winner_name": "Brand New Works"})

    assert page.status_code == 409
    assert "/award?tender_id=T-1" in page.text
    assert _count(path, Vendor) == 1


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("contract_date", "someday", "is not a date"),
        ("contract_value", "a lot", "is not an amount"),
        ("awarded_value", "plenty", "is not an amount"),
    ],
)
def test_an_award_edit_that_cannot_be_read_is_refused(window, field, value, message):
    client, path, _vendor = window

    answer = client.post("/award", data={"tender_id": "T-1", "bid": "1", field: value})

    assert answer.status_code == 400
    assert message in answer.text
    with session(engine(path)) as current:
        assert current.get(Tender, "T-1").title == "Stored"


def test_editing_an_award_that_is_not_stored_is_a_404(window):
    client, _path, _vendor = window

    answer = client.post("/award", data={"tender_id": "NOPE", "bid": "1", "title": "x"})

    assert answer.status_code == 404


def test_a_phone_alone_can_be_typed(window):
    client, path, vendor_id = window

    answer = client.post(f"/vendor/{vendor_id}/contact", data={"phone": "98765 43210", "back": "/"},
                         follow_redirects=False)

    assert answer.status_code == 303
    with session(engine(path)) as current:
        assert [c.channel for c in current.query(VendorContact).filter_by(source="typed")] == ["phone"]


def test_an_empty_contact_form_says_what_to_type(window):
    client, path, vendor_id = window

    answer = client.post(f"/vendor/{vendor_id}/contact", data={"email": " ", "phone": ""})

    assert answer.status_code == 400
    assert answer.text == "Type an email or a phone."
    assert _count(path, VendorContact) == 0


@pytest.mark.parametrize(
    ("url", "data"),
    [
        ("/vendor/999/contact", {"email": "a@b.example"}),
        ("/vendor/999/do-not-mail", {"do_not_mail": "1"}),
        ("/vendor/999/contact/1/remove", {}),
    ],
)
def test_a_company_that_is_not_stored_is_refused_not_an_error(window, url, data):
    client, _path, _vendor = window

    answer = client.post(url, data=data)

    assert answer.status_code == 400
