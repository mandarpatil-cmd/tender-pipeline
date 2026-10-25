"""docs/intent.md section 6: what an operator can do with only a browser.

The portal, the model and the mailbox are replaced in-process. Everything
else (the window, its runs, the captcha wait, the log, the letter rules and
the send queue) is the real code.
"""

from __future__ import annotations

import base64
import logging
import re
import threading
from io import BytesIO

import pytest
from openpyxl import load_workbook
from pipeline_core.db import engine, session
from pipeline_core.models import Outreach, Vendor
from pipeline_core.queries import replace_awards, upsert_tender, upsert_vendor, utcnow
from playwright.sync_api import expect

from ops_ui.runs import load_campaign

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
_SLOW = 20_000


def _rows(page) -> list[str]:
    return page.locator("tr.open-row td[data-col=tender_id]").all_inner_texts()


# --------------------------------------------------------------------------
# 1 and 5: filter, search by part of an id, a status and a date range; export
# --------------------------------------------------------------------------


def _exported(page) -> list[str]:
    with page.expect_download() as download:
        page.get_by_role("link", name="Excel").first.click()
    sheet = load_workbook(BytesIO(open(download.value.path(), "rb").read())).active
    header = [cell.value for cell in sheet[1]]
    return [row[header.index("Tender")] for row in sheet.iter_rows(min_row=2, values_only=True)]


def test_part_of_an_id_a_status_and_a_scraped_range_export_what_is_on_screen(browser_page, window):
    page = browser_page
    page.goto(window.url("/"))
    page.fill("input[name=q]", "_90")
    page.select_option("select[name=tender_status]", "AOC")
    page.get_by_text("Advanced filters").click()
    page.select_option("select[name=scraped_from_preset]", "7")
    page.get_by_role("button", name="Apply").click()

    expect(page.locator("tr.open-row")).to_have_count(2)
    on_screen = _rows(page)
    assert sorted(on_screen) == ["2026_ALPHA_901001_1", "2026_BETA_902002_1"]
    assert _exported(page) == on_screen


def test_a_contract_date_range_on_the_mail_page_exports_what_is_on_screen(browser_page, window):
    page = browser_page
    page.goto(window.url("/mail"))
    page.fill("input[name=q]", "_90")
    page.get_by_text("Advanced filters").click()
    page.select_option("select[name=date_from_preset]", "")
    page.fill("input[name=date_from]", "2026-08-01")
    page.get_by_role("button", name="Apply").click()

    expect(page.locator("tr.open-row")).to_have_count(1)
    assert _rows(page) == ["2026_GAMMA_903003_1"]
    assert _exported(page) == ["2026_GAMMA_903003_1"]


# --------------------------------------------------------------------------
# 2 and 3: start a scrape from the form, type the captcha, watch the log
# --------------------------------------------------------------------------


@pytest.fixture
def portal(window, tmp_path, monkeypatch):
    """Stage 1 replaced: it asks for one captcha, then saves one tender."""
    image = tmp_path / "captcha.png"
    image.write_bytes(_PNG)

    def scrape(out, *, ask, job_id, searched, **_kwargs):
        log = logging.getLogger("stage1_scrape.app.pipeline")
        log.info("Opened the portal search form.")
        code = ask(image)
        log.info("Portal accepted captcha %s", code)
        searched()
        with session(engine(window.path)) as current:
            vendor_id = upsert_vendor(current, name_raw="Delta Electricals")
            upsert_tender(current, tender_id="2026_DELTA_904004_1", title="Lights", scraped_at=utcnow(),
                          scrape_job_id=job_id)
            replace_awards(current, "2026_DELTA_904004_1",
                           [{"bid_number": "1", "vendor_id": vendor_id, "bidder_name": "Delta Electricals"}])
        log.info("Saved 2026_DELTA_904004_1")
        return {"scraped": 1, "vendors": 4, "tender_ids": ["2026_DELTA_904004_1"], "csv": "", "xlsx": ""}

    monkeypatch.setattr("stage1_scrape.app.run.run_scrape", scrape)


def test_a_scrape_asks_for_the_captcha_in_the_browser_and_carries_on(browser_page, window, portal):
    page = browser_page
    page.goto(window.url("/scrape"))
    page.get_by_role("button", name="Scrape").click()
    expect(page).to_have_url(re.compile(r"/scrape\?job=\d+"))

    code = page.locator("#captcha-code")
    expect(code).to_be_visible(timeout=_SLOW)
    assert page.locator("img.captcha").evaluate("img => img.complete && img.naturalWidth > 0")
    assert page.evaluate("document.activeElement.id") == "captcha-code"

    code.press_sequentially("AB1")
    page.wait_for_timeout(2500)  # the panel polls every second; the box and the typing stay
    expect(code).to_have_value("AB1")
    code.press_sequentially("2CD")
    page.get_by_role("button", name="Continue").click()

    log = page.locator("#job-log")
    expect(log).to_contain_text("Portal accepted captcha AB12CD", timeout=_SLOW)
    expect(log).to_contain_text("Scraped 1 new tender(s).", timeout=_SLOW)
    expect(page.locator("#live")).to_contain_text("done", timeout=_SLOW)

    polls: list[str] = []
    page.on("request", lambda request: "/log?" in request.url and polls.append(request.url))
    page.wait_for_timeout(2500)
    assert polls == [], "the log stops polling once the run is over"

    page.goto(window.url("/jobs"))
    run = page.locator("tr", has_text="scrape").first
    expect(run).to_contain_text("done")
    run.get_by_role("link").first.click()
    expect(page.locator("tr.open-row")).to_contain_text(["2026_DELTA_904004_1"])


# --------------------------------------------------------------------------
# 4: a tender by hand, the same company recognised, a typed email, a lookup
# --------------------------------------------------------------------------


def test_a_hand_entered_tender_finds_the_known_company_and_takes_a_typed_email(browser_page, window):
    page = browser_page
    page.goto(window.url("/scrape"))
    page.fill("form[action='/tender/new'] input[name=tender_id]", "HAND-0001")
    page.fill("form[action='/tender/new'] input[name=title]", "Fence repair")
    page.fill("form[action='/tender/new'] input[name=winner_name]", "ALPHA BUILDERS")
    page.get_by_role("button", name="Save tender").click()

    expect(page.locator("main")).to_contain_text("Alpha Builders")
    page.get_by_role("button", name="Attach the award").click()
    expect(page).to_have_url(re.compile(r"/\?q=HAND-0001"))
    expect(page.locator("tr.open-row")).to_have_count(1)
    with session(engine(window.path)) as current:
        assert current.query(Vendor).filter(Vendor.name_raw.ilike("alpha builders")).count() == 1

    page.locator("tr.open-row a.open").click()
    page.fill("form[action$='/contact'] input[name=email]", "typed@alpha.example")
    page.get_by_role("button", name="Save contact").click()
    expect(page.locator("#addresses")).to_contain_text("typed@alpha.example")


def test_a_lookup_runs_for_the_ticked_company_only_after_the_cost_is_shown(browser_page, window):
    page = browser_page
    page.goto(window.url("/enrich"))
    page.locator("tr.open-row", has_text="Beta Works").locator("input[name=vendor_id]").check()
    page.get_by_role("button", name="Enrich").click()

    expect(page.locator("#lookup")).to_have_text("Beta Works")
    expect(page.locator("p.cost").first).to_contain_text("Nothing will be spent.")
    expect(page.locator("input[name=dry_run]")).to_be_checked()
    page.get_by_role("button", name="Start").click()

    expect(page).to_have_url(re.compile(r"/enrich\?job=\d+"))
    expect(page.locator("#job-log")).to_contain_text("Nothing was looked up.", timeout=_SLOW)
    with session(engine(window.path)) as current:
        assert current.query(Vendor).filter_by(name_raw="Beta Works").one().enrichment_status == "pending"


# --------------------------------------------------------------------------
# 6: preview, a test to myself, one company, then the filtered view
# --------------------------------------------------------------------------


class Mailbox:
    """Takes every message instead of sending it. One address bounces."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def build(self, name):
        mailbox = self

        class Transport:
            def send(self, to, subject, body, attachments=None):
                if to == "sales@gamma.example":
                    from transport import TransportError

                    raise TransportError("550 mailbox unavailable")
                with mailbox.lock:
                    mailbox.sent.append((to, subject))

            def close(self):
                pass

        return Transport()


@pytest.fixture
def mailbox(monkeypatch):
    monkeypatch.setenv("OUTLOOK_USER", "me@company.example")
    monkeypatch.setenv("OUTLOOK_PASSWORD", "app-password")
    box = Mailbox()
    monkeypatch.setattr(load_campaign(), "build_transport", box.build)
    return box


def _choose(page, window, company: str | None = None):
    page.goto(window.url("/mail"))
    if company:
        page.locator("tr.open-row", has_text=company).locator("input[name=vendor_id]").check()
    else:
        page.locator("#select-all").check()
    page.get_by_role("button", name="Mail these").click()


def _run(page, button: str, expect_text: str):
    page.get_by_role("button", name=button).click()
    expect(page).to_have_url(re.compile(r"/mail\?job=\d+"))
    expect(page.locator("#job-log")).to_contain_text(expect_text, timeout=_SLOW)
    expect(page.locator("#live")).to_contain_text(re.compile("done|failed"), timeout=_SLOW)


def _outreach(window) -> list[tuple[str, str]]:
    with session(engine(window.path)) as current:
        return sorted((row.email, row.status) for row in current.query(Outreach).all())


def test_preview_test_one_company_then_the_view_never_mails_twice(browser_page, window, mailbox):
    page = browser_page

    # A live send is refused while the signature the letter uses is empty.
    _choose(page, window, "Alpha Builders")
    page.get_by_role("button", name="Send for real").click()
    expect(page.locator("p.warn")).to_contain_text("Refusing to send: fill in")
    assert mailbox.sent == []

    page.get_by_text("Edit the default").click()
    for name, value in {
        "sender_name": "Ada Lovelace",
        "sender_designation": "Director",
        "sender_org": "Policy Pact",
        "sender_mobile": "9000000000",
        "sender_email": "ada@policypact.example",
    }.items():
        page.fill(f"input[name={name}]", value)
    page.get_by_role("button", name="Save the default").click()
    expect(page.locator("main")).to_contain_text("Saved. Nothing was sent.")

    # Preview writes files and sends nothing.
    _choose(page, window, "Alpha Builders")
    _run(page, "Preview", "DRY RUN")
    assert mailbox.sent == [] and _outreach(window) == []

    # A test to myself is not outreach.
    _choose(page, window, "Alpha Builders")
    _run(page, "Send a test to me", "PASS")
    assert mailbox.sent[-1][0] == "me@company.example"
    assert _outreach(window) == []

    # One company.
    _choose(page, window, "Alpha Builders")
    _run(page, "Send for real", "sent to Alpha Builders")
    assert _outreach(window) == [("office@alpha.example", "sent")]

    # The whole "to send" view: Alpha is not mailed again; a bounce is logged and kept.
    _choose(page, window)
    expect(page.locator("main")).not_to_contain_text("office@alpha.example")
    _run(page, "Send for real", "FAILED Gamma Traders")
    assert [to for to, _subject in mailbox.sent].count("office@alpha.example") == 1
    assert _outreach(window) == [("office@alpha.example", "sent"), ("sales@gamma.example", "failed")]

    page.goto(window.url("/mail?outreach_status=failed"))
    expect(page.locator("tr.open-row")).to_contain_text(["2026_GAMMA_903003_1"])
