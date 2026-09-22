from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from stage1_scrape.domain.errors import CaptchaError
from stage1_scrape.scraping.captcha import parse_captcha_option
from stage1_scrape.scraping.ocr import normalize_captcha_text, read_captcha_image


def test_normalize_keeps_case_and_strips_noise():
    assert normalize_captcha_text("Ab12Xy") == "Ab12Xy"
    assert normalize_captcha_text("The code is: Ab12Xy.") == "Ab12Xy"
    assert normalize_captcha_text("  A b 1 2 X y  ") == "Ab12Xy"


def test_normalize_folds_accents_from_live_ocr():
    assert normalize_captcha_text("ç.nkx2.v") == "cnkx2v"
    assert normalize_captcha_text("hjqsä5") == "hjqsa5"
    assert normalize_captcha_text("17xüFè") == "17xuFe"


def test_normalize_rejects_wrong_length():
    with pytest.raises(CaptchaError):
        normalize_captcha_text("ABC")
    with pytest.raises(CaptchaError):
        normalize_captcha_text("ABCDEFG")
    with pytest.raises(CaptchaError):
        normalize_captcha_text("1yF.9R")


def test_parse_captcha_option_auto_manual_and_text():
    assert parse_captcha_option(None, default_solver="manual") == (None, "manual")
    assert parse_captcha_option("auto") == (None, "openrouter")
    assert parse_captcha_option("manual", default_solver="openrouter") == (None, "manual")
    assert parse_captcha_option("Ab12Xy", default_solver="openrouter") == (
        "Ab12Xy",
        "openrouter",
    )
    with pytest.raises(CaptchaError):
        parse_captcha_option("too-long")


def test_read_captcha_image_posts_data_url(tmp_path: Path):
    image = tmp_path / "captcha.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    source = MagicMock()
    source.api_key = "test-key"
    source.chat_url = "https://openrouter.ai/api/v1/chat/completions"
    source.extra_headers = {"HTTP-Referer": "https://localhost", "X-Title": "stage1-scrape"}
    response = MagicMock()
    response.ok = True
    response.status_code = 200
    response.json.return_value = {
        "choices": [{"message": {"content": "Ab12Xy"}}]
    }
    with (
        patch("stage1_scrape.scraping.ocr.openrouter_endpoint", return_value=source),
        patch("stage1_scrape.scraping.ocr.openrouter_ocr_model", return_value="google/gemini-2.5-flash"),
        patch("stage1_scrape.scraping.ocr.requests.post", return_value=response) as post,
    ):
        assert read_captcha_image(image) == "Ab12Xy"
    payload = post.call_args.kwargs["json"]
    assert payload["model"] == "google/gemini-2.5-flash"
    image_part = payload["messages"][0]["content"][1]["image_url"]["url"]
    assert image_part.startswith("data:image/png;base64,")
    assert "test-key" not in image_part
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer test-key"
    assert post.call_count == 1


def test_read_captcha_image_repairs_short_first_answer(tmp_path: Path):
    image = tmp_path / "captcha.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    source = MagicMock()
    source.api_key = "test-key"
    source.chat_url = "https://openrouter.ai/api/v1/chat/completions"
    source.extra_headers = {}
    first = MagicMock(ok=True, status_code=200)
    first.json.return_value = {"choices": [{"message": {"content": "1yF.9R"}}]}
    second = MagicMock(ok=True, status_code=200)
    second.json.return_value = {"choices": [{"message": {"content": "1yFi9R"}}]}
    with (
        patch("stage1_scrape.scraping.ocr.openrouter_endpoint", return_value=source),
        patch("stage1_scrape.scraping.ocr.openrouter_ocr_model", return_value="google/gemini-2.5-flash"),
        patch("stage1_scrape.scraping.ocr.requests.post", side_effect=[first, second]) as post,
    ):
        assert read_captcha_image(image) == "1yFi9R"
    assert post.call_count == 2
    assert "exactly 6 ASCII" in post.call_args_list[1].kwargs["json"]["messages"][0]["content"][0]["text"]
