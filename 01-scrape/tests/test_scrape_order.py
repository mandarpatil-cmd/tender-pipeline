"""The order tenders are opened in, and the search each saved tender records.

The portal decides the row order. The scraper walks page 1, 2, 3 and reads
each page top to bottom. Per row it checks, in this order: stop, AOC only,
the new-tender cap, already saved. Only then is the tender opened.
"""

from pathlib import Path
from unittest.mock import patch

from stage1_scrape.app.pipeline import (
    fetch_tender_pdfs,
    scrape_aoc,
    search_fields,
    search_overrides,
)
from stage1_scrape.domain.models import TenderRecord

_NEXT = '<a id="loadNext" title="Load Next" href="/eprocure/app?component=loadNext">Next &gt;</a>'


def _row(serial: int, tender_id: str, stage: str = "AOC") -> str:
    return (
        f"<tr><td>{serial}.</td><td>{tender_id}</td><td>Title</td><td>Org</td>"
        f"<td>{stage}</td><td></td>"
        f'<td><a title="View Tender Status" href="/eprocure/app?sp={tender_id}"></a></td></tr>'
    )


def _page(*rows: str, more: bool = False) -> str:
    return f"<table>{''.join(rows)}</table>{_NEXT if more else ''}"


class _Store:
    known: set[str] = set()

    def __init__(self, root, db_path=None) -> None:
        self.root = Path(root)

    def write_debug(self, name, html):
        return self.root / name

    def known_ids(self):
        return set(self.known)


def _client(later: list[str], asked: list[str]):
    class Client:
        def __init__(self, delay=0) -> None:
            pass

        def get(self, url):
            asked.append(url)

            class Response:
                text = later.pop(0)

            return Response()

    return Client


def _run(tmp_path, monkeypatch, first, later=(), known=(), **options):
    """Run scrape_aoc on canned listing pages. Returns what was opened and fetched."""
    opened: list = []
    asked: list[str] = []

    def open_one(client, listing, store, download_pdfs, source=None, **_kwargs):
        opened.append((listing, source))
        return TenderRecord(listing=listing)

    store = type("Store", (_Store,), {"known": set(known)})
    monkeypatch.setattr("stage1_scrape.app.pipeline.GePNICClient", _client(list(later), asked))
    monkeypatch.setattr("stage1_scrape.app.pipeline.Store", store)
    monkeypatch.setattr("stage1_scrape.app.pipeline.scrape_one_tender", open_one)
    with patch("stage1_scrape.app.pipeline._search_with_captcha", return_value=first):
        records = scrape_aoc(tmp_path, **options)
    return opened, asked, records


def test_rows_open_top_to_bottom_then_page_by_page(tmp_path, monkeypatch):
    first = _page(_row(1, "2026_B_9"), _row(2, "2026_B_8"), more=True)
    second = _page(_row(11, "2026_A_7"))

    opened, asked, records = _run(
        tmp_path, monkeypatch, first, [second], max_pages=None, max_tenders=None
    )

    assert [listing.tender_id for listing, _ in opened] == ["2026_B_9", "2026_B_8", "2026_A_7"]
    assert [listing.page for listing, _ in opened] == [1, 1, 2]
    assert [listing.serial for listing, _ in opened] == ["1.", "2.", "11."]
    assert len(asked) == 1
    assert [record.listing.tender_id for record in records] == [
        "2026_B_9",
        "2026_B_8",
        "2026_A_7",
    ]


def test_known_rows_are_skipped_and_do_not_use_up_the_cap(tmp_path, monkeypatch):
    first = _page(_row(1, "T-3"), _row(2, "T-2"), _row(3, "T-1"))

    opened, _, _ = _run(
        tmp_path, monkeypatch, first, known={"T-3"}, max_pages=1, max_tenders=1
    )

    assert [listing.tender_id for listing, _ in opened] == ["T-2"]


def test_refresh_reopens_a_known_tender(tmp_path, monkeypatch):
    first = _page(_row(1, "T-3"), _row(2, "T-2"))

    opened, _, _ = _run(
        tmp_path,
        monkeypatch,
        first,
        known={"T-3"},
        max_pages=1,
        max_tenders=1,
        skip_known=False,
    )

    assert [listing.tender_id for listing, _ in opened] == ["T-3"]


def test_max_pages_stops_before_asking_for_the_next_page(tmp_path, monkeypatch):
    first = _page(_row(1, "T-2"), _row(2, "T-1"), more=True)

    opened, asked, _ = _run(tmp_path, monkeypatch, first, max_pages=1, max_tenders=None)

    assert asked == []
    assert [listing.tender_id for listing, _ in opened] == ["T-2", "T-1"]


def test_a_page_of_known_rows_still_costs_a_listing_request(tmp_path, monkeypatch):
    """The skip-list runs after the page is fetched, so known rows are not free."""
    first = _page(_row(1, "T-2"), _row(2, "T-1"), more=True)
    second = _page(_row(11, "T-0"))

    opened, asked, _ = _run(
        tmp_path,
        monkeypatch,
        first,
        [second],
        known={"T-2", "T-1"},
        max_pages=2,
        max_tenders=None,
    )

    assert len(asked) == 1
    assert [listing.tender_id for listing, _ in opened] == ["T-0"]


def test_stop_is_checked_before_each_row(tmp_path, monkeypatch):
    first = _page(_row(1, "T-2"), _row(2, "T-1"))
    seen: list[str] = []

    def stop() -> bool:
        return bool(seen)

    def open_one(client, listing, store, download_pdfs, **_kwargs):
        seen.append(listing.tender_id)
        return TenderRecord(listing=listing)

    monkeypatch.setattr("stage1_scrape.app.pipeline.GePNICClient", _client([], []))
    monkeypatch.setattr("stage1_scrape.app.pipeline.Store", _Store)
    monkeypatch.setattr("stage1_scrape.app.pipeline.scrape_one_tender", open_one)
    with patch("stage1_scrape.app.pipeline._search_with_captcha", return_value=first):
        records = scrape_aoc(tmp_path, max_pages=1, max_tenders=None, should_stop=stop)

    assert seen == ["T-2"]
    assert [record.listing.tender_id for record in records] == ["T-2"]


def test_every_opened_tender_carries_the_search_and_the_run(tmp_path, monkeypatch):
    first = _page(_row(1, "T-1"))
    extra = search_overrides(
        "awarded", contract_from="01/01/2026", contract_to="31/01/2026"
    )

    opened, _, _ = _run(
        tmp_path,
        monkeypatch,
        first,
        max_pages=1,
        max_tenders=1,
        extra_fields=extra,
        search_mode="awarded",
        job_id=7,
    )

    (_listing, source), = opened
    assert source.search_mode == "awarded"
    assert source.job_id == 7
    assert source.search_fields == {
        "tenderStatus": "6",
        "fromDate": "01/01/2026",
        "toDate": "31/01/2026",
    }


def test_a_terminal_run_records_the_search_without_a_run_id(tmp_path, monkeypatch):
    opened, _, _ = _run(tmp_path, monkeypatch, _page(_row(1, "T-1")), max_pages=1)

    (_listing, source), = opened
    assert source.search_mode == "awarded"
    assert source.job_id is None
    assert source.search_fields == {"tenderStatus": "6"}


def test_search_fields_name_only_what_narrowed_the_search():
    assert search_fields(None) == {"tenderStatus": "6"}
    assert search_fields(search_overrides("awarded")) == {"tenderStatus": "6"}
    assert search_fields(
        search_overrides(
            "organisation",
            organ_name="534",
            published_from="01/02/2026",
            published_to="28/02/2026",
        )
    ) == {
        "tenderStatus": "0",
        "OrganName": "534",
        "publishedFromDate": "01/02/2026",
        "publishedToDate": "28/02/2026",
    }
    assert search_fields({"tenderId": " 2026_X_1 "}) == {
        "tenderStatus": "6",
        "tenderId": "2026_X_1",
    }


def test_download_pdfs_records_a_tender_id_search_on_page_one(tmp_path, monkeypatch):
    opened: list = []

    def open_one(client, listing, store, download_pdfs, source=None, **_kwargs):
        opened.append((listing, source))
        return TenderRecord(listing=listing)

    monkeypatch.setattr("stage1_scrape.app.pipeline.GePNICClient", _client([], []))
    monkeypatch.setattr("stage1_scrape.app.pipeline.Store", _Store)
    monkeypatch.setattr("stage1_scrape.app.pipeline.scrape_one_tender", open_one)
    with patch(
        "stage1_scrape.app.pipeline._search_with_captcha",
        return_value=_page(_row(1, "2026_X_1")),
    ):
        info = fetch_tender_pdfs(
            tmp_path, "2026_X_1", zip_path=tmp_path / "out.zip", job_id=4
        )

    (listing, source), = opened
    assert info["ok"] is True
    assert listing.page == 1
    assert source.search_mode == "tender_id"
    assert source.job_id == 4
    assert source.search_fields == {"tenderStatus": "6", "tenderId": "2026_X_1"}
