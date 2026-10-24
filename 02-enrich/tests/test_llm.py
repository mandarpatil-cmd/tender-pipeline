"""The model call: its settings, its guards, and what an odd answer does."""

from __future__ import annotations

import pytest
from pipeline_core.db import session
from pipeline_core.models import STATUS_FAILED, LlmRun, Vendor
from pipeline_core.queries import upsert_vendor

from stage2_enrich import settings
from stage2_enrich.llm import ContactInfo, build_chain, fetch_contact
from stage2_enrich.runner import enrich_pending


class Answer:
    def __init__(self, result):
        self.result = result

    def invoke(self, payload):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _client(chain):
    return next(
        getattr(step, "bound", step)
        for step in chain.steps
        if hasattr(getattr(step, "bound", step), "extra_body")
    )


@pytest.mark.parametrize(("key", "model", "named"), [("", "m", "OPENROUTER_API_KEY"), ("k", "", "MODEL")])
def test_a_missing_key_or_model_is_named_before_any_call(monkeypatch, key, model, named):
    monkeypatch.setenv("OPENROUTER_API_KEY", key)
    monkeypatch.setenv("MODEL", model)

    with pytest.raises(RuntimeError, match=named):
        build_chain()


@pytest.mark.parametrize("value", ["0", "false", "No", " OFF "])
def test_web_search_is_off_for_every_spelling_of_no(monkeypatch, value):
    monkeypatch.setenv("WEB_SEARCH", value)
    assert settings.web_search() is False


@pytest.mark.parametrize("value", ["", "1", "yes", "on"])
def test_web_search_stays_on_otherwise(monkeypatch, value):
    monkeypatch.setenv("WEB_SEARCH", value)
    assert settings.web_search() is True


def test_the_call_is_deterministic_capped_and_retried(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH", raising=False)
    client = _client(build_chain())

    assert client.temperature == 0
    assert client.max_retries == 5
    assert client.max_tokens == settings.MAX_OUTPUT_TOKENS
    assert client.request_timeout == 120
    assert client.openai_api_base == settings.OPENROUTER_BASE_URL


def test_without_web_search_the_timeout_is_shorter(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH", "0")
    assert _client(build_chain()).request_timeout == 60


def test_an_answer_that_is_not_a_contact_is_an_error():
    with pytest.raises(TypeError, match="ContactInfo"):
        fetch_contact(Answer({"email": "a@b.example"}), "Kanta Enterprises")


def test_a_blank_title_is_sent_as_not_recorded():
    seen = []

    class Recording(Answer):
        def invoke(self, payload):
            seen.append(payload)
            return ContactInfo()

    fetch_contact(Recording(None), "Kanta Enterprises")
    assert seen[0]["title"] == "(not recorded)"


def test_a_long_provider_error_is_kept_short_in_the_audit():
    with session() as current:
        vendor_id = upsert_vendor(current, name_raw="Verbose Errors Ltd")

    summary = enrich_pending(chain=Answer(RuntimeError("x" * 5000)))

    assert summary.failed == 1
    with session() as current:
        assert current.get(Vendor, vendor_id).enrichment_status == STATUS_FAILED
        run = current.query(LlmRun).one()
        assert len(run.error) <= 500
        assert run.error.startswith("RuntimeError: xxx")


def test_stop_during_the_wait_ends_the_run_before_the_next_call():
    with session() as current:
        for name in ("First Ltd", "Second Ltd"):
            upsert_vendor(current, name_raw=name)
    calls = []

    class Counting(Answer):
        def invoke(self, payload):
            calls.append(payload["company"])
            return ContactInfo(email="a@first.example")

    stops = iter([False, True])
    summary = enrich_pending(chain=Counting(None), delay=5, should_stop=lambda: next(stops, True))

    assert calls == ["First Ltd"]
    assert summary.attempted == 1
