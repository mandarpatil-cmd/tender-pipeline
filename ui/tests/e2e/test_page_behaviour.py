"""What the page scripts do in a real browser."""

from __future__ import annotations

import re
from datetime import date, timedelta

from playwright.sync_api import expect


def _errors(page) -> list[str]:
    found: list[str] = []
    page.on("pageerror", lambda error: found.append(str(error)))
    return found


def test_every_page_loads_without_a_script_error(browser_page, window):
    errors = _errors(browser_page)
    for path in ["/", "/scrape", "/enrich", "/mail", "/jobs", "/tender/new"]:
        browser_page.goto(window.url(path))
        expect(browser_page.locator("main")).to_be_visible()

    assert errors == []


def test_the_counts_on_home_match_the_rows(browser_page, window):
    browser_page.goto(window.url("/"))

    expect(browser_page.locator("tr.open-row")).to_have_count(3)
    expect(browser_page.locator("[data-count=awards]").first).to_have_text("3")


def test_clicking_a_row_opens_that_award_and_back_returns(browser_page, window):
    browser_page.goto(window.url("/"))
    browser_page.locator("tr.open-row", has_text="Beta Works").locator("td[data-col=title]").click()

    expect(browser_page).to_have_url(re.compile(r"/award\?tender_id=2026_BETA_902002_1"))
    expect(browser_page.locator("input[name=title]")).to_have_value("Bridge works")
    back = browser_page.locator("#back")
    expect(back).to_be_visible()
    back.click()
    expect(browser_page).to_have_url(window.url("/"))


def test_page_ticks_and_every_page_tick_the_rows(browser_page, window):
    browser_page.goto(window.url("/mail"))
    rows = browser_page.locator("input[name=vendor_id]")
    page_ticks = browser_page.locator("#page-ticks")
    every = browser_page.locator("#select-all")
    expect(rows).to_have_count(2)

    page_ticks.check()
    assert all(box.is_checked() for box in rows.all())
    rows.first.uncheck()
    expect(page_ticks).not_to_be_checked()

    every.check()
    assert all(box.is_checked() for box in rows.all())
    expect(page_ticks).to_be_checked()
    rows.first.uncheck()
    expect(every).not_to_be_checked()


def test_a_date_preset_fills_the_range_and_hides_the_from_box(browser_page, window):
    browser_page.goto(window.url("/"))
    browser_page.get_by_text("Advanced filters").click()
    browser_page.select_option("select[name=scraped_from_preset]", "7")

    group = browser_page.locator("[data-date-range]").first
    today = group.get_attribute("data-today")
    expected = (date.fromisoformat(today) - timedelta(days=7)).isoformat()
    assert browser_page.locator("input[name=scraped_from]").input_value() == expected
    assert browser_page.locator("input[name=scraped_to]").input_value() == today
    expect(browser_page.locator("input[name=scraped_from]")).to_be_hidden()


def test_a_filter_narrows_the_table_in_place_and_is_remembered(browser_page, window):
    browser_page.goto(window.url("/"))
    browser_page.fill("input[name=q]", "gamma")
    browser_page.get_by_role("button", name="Apply").click()

    expect(browser_page.locator("tr.open-row")).to_have_count(1)
    expect(browser_page).to_have_url(re.compile(r"q=gamma"))
    browser_page.goto(window.url("/jobs"))
    browser_page.get_by_role("link", name="All tenders").click()
    expect(browser_page.locator("tr.open-row")).to_have_count(1)


def test_the_theme_switch_is_kept_across_pages(browser_page, window):
    browser_page.goto(window.url("/"))
    browser_page.click("#theme")
    expect(browser_page.locator("html")).to_have_class(re.compile("dark"))

    browser_page.goto(window.url("/jobs"))
    expect(browser_page.locator("html")).to_have_class(re.compile("dark"))
    expect(browser_page.locator("#theme")).to_have_attribute("aria-label", "Switch to light mode")
