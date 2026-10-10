from stage1_scrape.app.pipeline import search_overrides, submit_search
from stage1_scrape.scraping.captcha import extract_captcha_bytes
from stage1_scrape.scraping.forms import extract_form_fields, field_map, override_fields, select_options


def test_extract_form_skips_border_form_and_submit_buttons(html):
    fields = extract_form_fields(html("search_form.html"))
    names = [name for name, _ in fields]
    assert "domainUrl" not in names
    assert "Search" not in names
    assert "captcha" not in names
    assert names.count("tokenSecret") == 1
    values = field_map(fields)
    assert values["tenderStatus"] == ["0"]
    assert values["tokenSecret"] == ["SKuWV88nl25082026152256"]
    assert "captchaText" in values


def test_iterrows_duplicates_are_preserved(html):
    fields = extract_form_fields(html("results_table.html"))
    blobs = [value for name, value in fields if name == "iterRows_0"]
    assert blobs == ["gzipblob-row-1", "gzipblob-row-2", "gzipblob-row-3"]


def test_override_sets_aoc_and_search(html):
    fields = extract_form_fields(html("search_form.html"))
    payload = override_fields(
        fields,
        {"tenderStatus": "6", "captchaText": "AB12CD", "Search": "Search"},
    )
    mapped = field_map(payload)
    assert mapped["tenderStatus"] == ["6"]
    assert mapped["captchaText"] == ["AB12CD"]
    assert mapped["Search"] == ["Search"]
    assert mapped["KeyWord"] == [""]


def test_captcha_is_inline_png(html):
    raw, ext = extract_captcha_bytes(html("search_form.html"))
    assert ext == "png"
    assert raw.startswith(b"\x89PNG")


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeClient:
    def __init__(self) -> None:
        self.posted = None
        self._last_url = "https://eprocure.gov.in/eprocure/app"

    def post(self, fields, url, referer=None):
        self.posted = list(fields)
        return FakeResponse("<html>ok</html>")


def test_submit_search_payload_matches_confirmed_overrides(html):
    client = FakeClient()
    page = submit_search(client, html("search_form.html"), "ZX9Q2A")
    assert page == "<html>ok</html>"
    mapped = field_map(client.posted)
    assert mapped["tenderStatus"] == ["6"]
    assert mapped["captchaText"] == ["ZX9Q2A"]
    assert mapped["Search"] == ["Search"]
    assert mapped["component"] == ["frmSearchFilter"]
    assert mapped["page"] == ["WebTenderStatusLists"]


def test_select_options_skip_the_blank_choice():
    html = """
    <select name="OrganName">
      <option value="0">-Select-</option>
      <option value="534">Ministry of Road Transport and Highways</option>
      <option value=""> </option>
    </select>
    """
    assert select_options(html, "OrganName") == [
        ("534", "Ministry of Road Transport and Highways")
    ]
    assert select_options(html, "Department") == []


def test_awarded_overrides_clear_the_organisation_group():
    fields = search_overrides(
        "awarded",
        contract_from="01/08/2026",
        contract_to="25/08/2026",
    )
    assert fields["tenderStatus"] == "6"
    assert fields["fromDate"] == "01/08/2026"
    assert fields["toDate"] == "25/08/2026"
    assert fields["OrganName"] == "0"
    assert fields["publishedFromDate"] == ""
    assert fields["publishedToDate"] == ""
    assert fields["tenderId"] == ""


def test_organisation_overrides_clear_the_awarded_group():
    fields = search_overrides(
        "organisation",
        published_from="01/02/2026",
        published_to="28/02/2026",
        organ_name="534",
    )
    assert fields["tenderStatus"] == "0"
    assert fields["OrganName"] == "534"
    assert fields["publishedFromDate"] == "01/02/2026"
    assert fields["publishedToDate"] == "28/02/2026"
    assert fields["fromDate"] == ""
    assert fields["toDate"] == ""
    assert fields["tenderId"] == ""
    assert fields["KeyWord"] == ""


def test_submit_search_applies_blank_overrides(html):
    page = html("search_form.html").replace(
        'name="fromDate" value=""',
        'name="fromDate" value="01/01/2020"',
    ).replace(
        '<option value="6">AOC</option>',
        '<option value="6" selected="selected">AOC</option>',
    )
    client = FakeClient()
    submit_search(
        client,
        page,
        "ZX9Q2A",
        extra=search_overrides("organisation", organ_name="534"),
    )
    mapped = field_map(client.posted)
    assert mapped["tenderStatus"] == ["0"]
    assert mapped["OrganName"] == ["534"]
    assert mapped["fromDate"] == [""]
    assert mapped["toDate"] == [""]
    assert mapped["KeyWord"] == [""]
    assert mapped["tenderId"] == [""]
    assert mapped["publishedFromDate"] == [""]
    assert mapped["captchaText"] == ["ZX9Q2A"]


def test_submit_search_can_set_optional_dates(html):
    client = FakeClient()
    submit_search(
        client,
        html("search_form.html"),
        "ZX9Q2A",
        extra={"fromDate": "01/08/2026", "toDate": "25/08/2026"},
    )
    mapped = field_map(client.posted)
    assert mapped["fromDate"] == ["01/08/2026"]
    assert mapped["toDate"] == ["25/08/2026"]
    assert mapped["tenderStatus"] == ["6"]
