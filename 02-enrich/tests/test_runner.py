"""What stage 2 promises: one commit per vendor, and a failure that can be retried."""

from __future__ import annotations

from pipeline_core.db import session
from pipeline_core.models import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_NOT_FOUND,
    STATUS_PENDING,
    LlmRun,
    Vendor,
)
from pipeline_core.queries import reset_failed, upsert_vendor

from stage2_enrich.llm import ContactInfo
from stage2_enrich.runner import enrich_pending, queue


class FakeChain:
    """Stands in for prompt | ChatOpenAI. Records what it was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def invoke(self, payload):
        self.calls.append(payload)
        answer = self.answers.pop(0) if self.answers else ContactInfo()
        # BaseException, not Exception: KeyboardInterrupt must be raisable here,
        # and `enrich_one` deliberately does not catch it.
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _seed(*names: str) -> list[int]:
    with session() as current:
        return [upsert_vendor(current, name_raw=name) for name in names]


def test_a_found_contact_is_written_back_to_the_vendor():
    (vendor_id,) = _seed("Kanta Enterprises")
    chain = FakeChain(ContactInfo(email="hi@kanta.example", phone="+91 712 111"))

    summary = enrich_pending(chain=chain)

    assert summary.done == 1
    with session() as current:
        vendor = current.get(Vendor, vendor_id)
        assert vendor.email == "hi@kanta.example"
        assert vendor.phone == "+91 712 111"
        assert vendor.enrichment_status == STATUS_DONE
        assert vendor.enriched_at


def test_no_public_contact_is_not_found_and_is_never_retried():
    _seed("Obscure Traders")
    chain = FakeChain(ContactInfo(email=None, phone=None))

    summary = enrich_pending(chain=chain)

    assert summary.not_found == 1
    with session() as current:
        assert current.query(Vendor).one().enrichment_status == STATUS_NOT_FOUND
    assert queue() == []  # gone from the work queue for good


def test_a_provider_error_marks_failed_and_does_not_stop_the_run():
    ids = _seed("First Ltd", "Broken Ltd", "Third Ltd")
    chain = FakeChain(
        ContactInfo(email="a@first.example"),
        RuntimeError("429 rate limited"),
        ContactInfo(email="c@third.example"),
    )

    summary = enrich_pending(chain=chain)

    assert (summary.done, summary.failed) == (2, 1)
    with session() as current:
        rows = {v.vendor_id: v for v in current.query(Vendor).all()}
    assert rows[ids[1]].enrichment_status == STATUS_FAILED
    assert rows[ids[1]].email is None  # left blank so a retry can fill it
    assert rows[ids[2]].email == "c@third.example"  # the run carried on


def test_every_call_is_audited_win_or_lose():
    _seed("Good Ltd", "Bad Ltd")
    chain = FakeChain(ContactInfo(email="a@good.example"), RuntimeError("boom"))

    enrich_pending(chain=chain)

    with session() as current:
        runs = current.query(LlmRun).order_by(LlmRun.run_id).all()
        assert [r.status for r in runs] == ["ok", "error"]
        assert runs[0].model == "google/gemini-2.5-flash"
        assert runs[0].prompt_version == "contact-v1"
        assert "a@good.example" in runs[0].output_json
        assert "boom" in runs[1].error
        assert runs[1].output_json is None


def test_a_second_run_does_no_work(monkeypatch):
    """Resumability: the queue is the state, so re-running is safe and free."""
    _seed("Kanta Enterprises")
    enrich_pending(chain=FakeChain(ContactInfo(email="hi@kanta.example")))

    def explode():
        raise AssertionError("build_chain must not be called with an empty queue")

    monkeypatch.setattr("stage2_enrich.runner.build_chain", explode)
    second = enrich_pending()

    assert second.attempted == 0


def test_an_interrupted_run_keeps_what_it_already_answered():
    ids = _seed("First Ltd", "Second Ltd", "Third Ltd")
    chain = FakeChain(
        ContactInfo(email="a@first.example"),
        KeyboardInterrupt(),
        ContactInfo(email="c@third.example"),
    )

    summary = enrich_pending(chain=chain)

    assert summary.done == 1
    with session() as current:
        rows = {v.vendor_id: v for v in current.query(Vendor).all()}
    assert rows[ids[0]].email == "a@first.example"  # committed before the stop
    assert rows[ids[1]].enrichment_status == STATUS_PENDING  # untouched
    assert rows[ids[2]].enrichment_status == STATUS_PENDING
    assert [v.vendor_id for v in queue()] == [ids[1], ids[2]]  # resumes here


def test_failed_vendors_can_be_requeued_but_not_found_ones_cannot():
    ids = _seed("Broken Ltd", "Obscure Ltd")
    enrich_pending(chain=FakeChain(RuntimeError("boom"), ContactInfo()))

    with session() as current:
        assert reset_failed(current) == 1

    assert [v.vendor_id for v in queue()] == [ids[0]]


def test_the_prompt_gets_the_award_title_as_context():
    from pipeline_core.queries import replace_awards, upsert_tender, utcnow

    with session() as current:
        vendor_id = upsert_vendor(current, name_raw="Kanta Enterprises")
        upsert_tender(current, tender_id="2026_A_1", scraped_at=utcnow())
        replace_awards(
            current,
            "2026_A_1",
            [
                {
                    "bid_number": "1",
                    "vendor_id": vendor_id,
                    "bidder_name": "Kanta",
                    "work_title": "Supply of ceiling fans",
                }
            ],
        )

    chain = FakeChain(ContactInfo(email="hi@kanta.example"))
    enrich_pending(chain=chain)

    assert chain.calls[0]["company"] == "Kanta Enterprises"
    assert chain.calls[0]["title"] == "Supply of ceiling fans"


def test_limit_bounds_the_spend():
    _seed("A Ltd", "B Ltd", "C Ltd")
    chain = FakeChain(*[ContactInfo(email=f"{n}@x.example") for n in "abc"])

    summary = enrich_pending(limit=2, chain=chain)

    assert summary.attempted == 2
    assert len(chain.calls) == 2
    assert len(queue()) == 1


# --------------------------------------------------------------------------
# PDF evidence from stage 1
# --------------------------------------------------------------------------


def test_a_pdf_contact_is_offered_to_the_model_as_a_lead():
    from pipeline_core.queries import set_pdf_contacts

    with session() as current:
        vendor_id = upsert_vendor(current, name_raw="Sunrise Builders")
        set_pdf_contacts(
            current, vendor_id, email="sunrisebuilders@example.com", phone="9123456780"
        )

    chain = FakeChain(ContactInfo(email="sunrisebuilders@example.com"))
    enrich_pending(chain=chain)

    prompt = chain.calls[0]["evidence"]
    assert "sunrisebuilders@example.com" in prompt
    assert "9123456780" in prompt
    assert "OCR" in prompt  # told it may be garbled, not to be trusted blindly


def test_no_pdf_contact_means_no_evidence_block():
    _seed("Acme Ltd")
    chain = FakeChain(ContactInfo(email="hi@acme.example"))
    enrich_pending(chain=chain)

    assert chain.calls[0]["evidence"] == ""


def test_the_pdf_contact_is_never_written_through_as_the_answer():
    """Evidence is a lead. If the model cannot corroborate it, nothing is stored."""
    from pipeline_core.queries import set_pdf_contacts

    with session() as current:
        vendor_id = upsert_vendor(current, name_raw="Acme Techworks Private Limited")
        set_pdf_contacts(current, vendor_id, email="acmctechworks0l@example.com", phone=None)

    # the OCR text was garbled and the model could not confirm it
    enrich_pending(chain=FakeChain(ContactInfo(email=None, phone=None)))

    with session() as current:
        vendor = current.get(Vendor, vendor_id)
        assert vendor.email is None
        assert vendor.enrichment_status == STATUS_NOT_FOUND
        assert vendor.pdf_email == "acmctechworks0l@example.com"  # kept as evidence


def test_the_model_may_correct_a_garbled_pdf_address():
    from pipeline_core.queries import set_pdf_contacts

    with session() as current:
        vendor_id = upsert_vendor(current, name_raw="Acme Techworks Private Limited")
        set_pdf_contacts(current, vendor_id, email="acmctechworks0l@example.com", phone=None)

    enrich_pending(chain=FakeChain(ContactInfo(email="acmetechworks01@example.com")))

    with session() as current:
        assert current.get(Vendor, vendor_id).email == "acmetechworks01@example.com"


# --------------------------------------------------------------------------
# Web search
# --------------------------------------------------------------------------


def test_web_search_is_on_by_default_and_reaches_openrouter(monkeypatch):
    """Without this the model answers from memory, and returns null for every
    small regional contractor -- which is what the first live run did."""
    from stage2_enrich import settings
    from stage2_enrich.llm import build_chain

    monkeypatch.delenv("WEB_SEARCH", raising=False)
    assert settings.web_search() is True

    chain = build_chain()
    client = next(
        getattr(step, "bound", step)
        for step in chain.steps
        if hasattr(getattr(step, "bound", step), "extra_body")
    )
    assert client.extra_body == {
        "plugins": [{"id": "web", "max_results": settings.WEB_SEARCH_RESULTS}]
    }


def test_web_search_can_be_turned_off(monkeypatch):
    from stage2_enrich import settings
    from stage2_enrich.llm import build_chain

    monkeypatch.setenv("WEB_SEARCH", "0")
    assert settings.web_search() is False

    chain = build_chain()
    client = next(
        getattr(step, "bound", step)
        for step in chain.steps
        if hasattr(getattr(step, "bound", step), "extra_body")
    )
    assert not client.extra_body


def test_the_source_url_is_recorded_for_every_answer():
    """Provenance: before mailing an address you want to know where it came from."""
    from pipeline_core.models import LlmRun

    _seed("Kanta Enterprises")
    chain = FakeChain(
        ContactInfo(
            email="hi@kanta.example",
            phone=None,
            source="https://www.indiamart.com/kanta-enterprises/",
        )
    )
    enrich_pending(chain=chain)

    with session() as current:
        run = current.query(LlmRun).one()
        assert "indiamart.com" in run.output_json


def test_only_source_narrows_the_queue_without_touching_the_rest():
    """Work through what the scraper just found, without paying for the imports."""
    from pipeline_core.models import SOURCE_BIDEASY, SOURCE_SCRAPE, Vendor
    from stage2_enrich.runner import queue

    with session() as current:
        scraped = upsert_vendor(current, name_raw="Local Contractor", source=SOURCE_SCRAPE)
        upsert_vendor(current, name_raw="Imported Ltd", source=SOURCE_BIDEASY)

    assert [v.vendor_id for v in queue(source=SOURCE_SCRAPE)] == [scraped]
    assert len(queue()) == 2

    enrich_pending(source=SOURCE_SCRAPE, chain=FakeChain(ContactInfo(email="a@b.example")))

    with session() as current:
        rows = {v.name_raw: v.enrichment_status for v in current.query(Vendor).all()}
    assert rows["Local Contractor"] == "done"
    assert rows["Imported Ltd"] == "pending"  # never touched, never paid for
