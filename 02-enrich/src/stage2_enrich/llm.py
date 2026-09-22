"""The one model call: company name (plus tender context) -> email and phone.

Unchanged in substance from the original scripts — OpenRouter through LangChain,
with a Pydantic schema so the model must answer in a shape we can store. What
changed is where the question comes from and where the answer goes: the database,
not a spreadsheet.
"""

from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, SecretStr

from . import settings


class ContactInfo(BaseModel):
    """Public contact details for a company.

    Both fields are optional on purpose: "not known" is a real answer, and a
    guessed address is worse than none — stage 3 would mail a stranger.
    """

    email: str | None = Field(
        default=None,
        description="Public contact email, or null if not known with confidence",
    )
    phone: str | None = Field(
        default=None,
        description="Public contact phone with country code, or null if not known",
    )
    source: str | None = Field(
        default=None,
        description=(
            "URL of the page the details came from, or null if not from a source "
            "you can name. Do not invent a URL."
        ),
    )


PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You find publicly listed business contact details for Indian companies. "
            "Search the web before answering -- most of these are small regional "
            "contractors you will not know from memory, and their details live on "
            "directory sites (IndiaMART, JustDial, TradeIndia), GST and tender "
            "portals, or their own site. Report only what you actually find. "
            "A null is a fine answer; an invented address is not.",
        ),
        (
            "human",
            "Company: {company}\n"
            "Context - they bid on this tender: {title}\n"
            "{evidence}\n"
            "Find their public contact email and phone number, and give the URL you "
            "took them from.",
        ),
    ]
)

#: Shown when stage 1 pulled contact details out of the tender's work order.
#: These come off a scan, so they are a lead to verify rather than an answer --
#: the garbled example is real, from vendor 7 in this database.
EVIDENCE_TEMPLATE = (
    "\nA scanned work order for that tender lists:\n"
    "{lines}"
    "That text came from OCR, so characters may be wrong -- one real example read\n"
    "'acmctechworks0l@example.com' where the true address was 'acmetechworks01@example.com'.\n"
    "Treat it as a strong lead: confirm it against public sources and fix any mangled\n"
    "characters. If you cannot corroborate it at all, return null rather than repeat it.\n"
)


def build_evidence(pdf_email: str | None = None, pdf_phone: str | None = None) -> str:
    """The evidence block, or an empty string when stage 1 found nothing."""
    lines = ""
    if pdf_email:
        lines += f"  email: {pdf_email}\n"
    if pdf_phone:
        lines += f"  phone: {pdf_phone}\n"
    return EVIDENCE_TEMPLATE.format(lines=lines) if lines else ""


def build_chain() -> Runnable:
    """Prompt -> OpenRouter -> ContactInfo. Built once per run, not per vendor."""
    settings.require_env()

    extra_body: dict = {}
    if settings.web_search():
        # OpenRouter's web plugin. `max_results` is the cost dial: billing is per
        # result, per call, on top of the model's tokens.
        extra_body["plugins"] = [
            {"id": "web", "max_results": settings.WEB_SEARCH_RESULTS}
        ]

    llm = ChatOpenAI(
        model=settings.model(),
        api_key=SecretStr(settings.api_key()),
        base_url=settings.OPENROUTER_BASE_URL,
        temperature=0,
        max_retries=5,
        timeout=120 if settings.web_search() else 60,
        # Without this the provider reserves the model's full output ceiling
        # against your credit balance -- see settings.MAX_OUTPUT_TOKENS.
        max_tokens=settings.MAX_OUTPUT_TOKENS,
        extra_body=extra_body or None,
    )
    return PROMPT | llm.with_structured_output(ContactInfo)


def fetch_contact(
    chain: Runnable,
    company: str,
    title: str = "",
    evidence: str = "",
) -> ContactInfo:
    result = chain.invoke(
        {
            "company": company,
            "title": title or "(not recorded)",
            "evidence": evidence,
        }
    )
    if not isinstance(result, ContactInfo):
        raise TypeError(f"Expected ContactInfo, got {type(result)}")
    return result
