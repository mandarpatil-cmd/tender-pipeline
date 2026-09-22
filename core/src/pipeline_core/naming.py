"""Company-name rules.

`name_norm` is the unique key on `vendors`, so every stage that inserts a vendor
has to normalise identically — the scraper, the BidEasy import, anything later.
That makes these rules shared infrastructure rather than scraper internals, which
is why they live here and not in a stage.

`winner_rows` is deliberately *not* here: it reads a scraped TenderRecord and
belongs to the scraper.
"""

from __future__ import annotations

import re
import unicodedata

_PVT_LTD = re.compile(r"\bP(?:RIVATE)?\s*LTD\.?\b|\bPRIVATE LIMITED\b")
_LTD = re.compile(r"\bLTD\.?\b|\bLIMITED\b")
_LLP = re.compile(r"\bLLP\b")
_TRADE = (
    "ENTERPRISE",
    "CONSTRUCT",
    "COSTRUCT",
    "CONTRACT",
    "INFRA",
    "ENGINEER",
    "SERVICE",
    "INDUSTR",
    "TRADER",
    "TRADING",
    "FURNITURE",
    "HOSPITALITY",
    "CATER",
    "ASSOCIATE",
    "SOLUTION",
    "SECURITY",
    "ELECTR",
    "AGENCY",
    "COMPANY",
    "CORP",
    "WORKS",
    "PROJECT",
    "SYSTEMS",
    "TECH",
    "LABS",
    "CREATION",
    "FURNITECH",
    "SALON",
    "PARLOUR",
)
_CITY_STATE = {
    "NAGPUR": "Maharashtra",
    "MUMBAI": "Maharashtra",
    "PUNE": "Maharashtra",
    "NASHIK": "Maharashtra",
    "THANE": "Maharashtra",
    "NAGAR": "",
    "DELHI": "Delhi",
    "NEW DELHI": "Delhi",
    "BENGALURU": "Karnataka",
    "BANGALORE": "Karnataka",
    "HYDERABAD": "Telangana",
    "CHENNAI": "Tamil Nadu",
    "KOLKATA": "West Bengal",
    "AHMEDABAD": "Gujarat",
    "JAIPUR": "Rajasthan",
    "LUCKNOW": "Uttar Pradesh",
    "BHOPAL": "Madhya Pradesh",
    "INDORE": "Madhya Pradesh",
    "CHANDIGARH": "Chandigarh",
    "BHUBANESWAR": "Odisha",
    "PATNA": "Bihar",
    "RANCHI": "Jharkhand",
    "RAIPUR": "Chhattisgarh",
    "VISAKHAPATNAM": "Andhra Pradesh",
    "KOCHI": "Kerala",
    "THIRUVANANTHAPURAM": "Kerala",
}


def normalize_name(name: str) -> str:
    text = unicodedata.normalize("NFKC", name or "")
    text = text.replace("&", " AND ")
    # Strip the "M/s" trade prefix *before* punctuation, not after. The scraper
    # did it the other way round, which turned "M/s Acme" into "M S Acme" and
    # left the prefix in the key — so "M/s Acme" and "Acme" were two vendors.
    # Harmless for the 18 scraped rows (none carry it), but 119 of the BidEasy
    # rows do.
    text = re.sub(r"\bM/?S\b\.?", " ", text, flags=re.I)
    text = re.sub(r"[.,'’/\\()]", " ", text)
    text = _PVT_LTD.sub(" PVT LTD ", text.upper())
    text = re.sub(r"\s+", " ", text).strip()
    return text


def infer_legal_form(name: str) -> str:
    """Best-effort label from the portal trade name. Not a legal classification."""
    raw = name or ""
    upper = normalize_name(raw)
    if _PVT_LTD.search(raw.upper()) or "PVT LTD" in upper:
        return "pvt_ltd"
    if _LLP.search(upper):
        return "llp"
    if _LTD.search(upper):
        return "ltd"
    if any(token in upper for token in _TRADE):
        return "trade"
    if " AND " in f" {upper} ":
        return "trade"
    tokens = upper.split()
    if 2 <= len(tokens) <= 4 and all(t.isalpha() for t in tokens):
        return "person"
    return "trade"


def infer_city_state(organisation_chain: str) -> tuple[str, str]:
    """Return (city, state) from the buyer chain when a known city appears."""
    tail = (organisation_chain or "").split("||")[-1].strip().upper()
    for city in sorted(_CITY_STATE, key=len, reverse=True):
        if city == "NAGAR":
            continue
        if re.search(rf"\b{re.escape(city)}\b", tail):
            state = _CITY_STATE[city]
            label = "Bengaluru" if city == "BANGALORE" else city.title()
            if city == "NEW DELHI":
                label = "New Delhi"
            return label, state
    return "", ""


def buyer_tail(organisation_chain: str) -> str:
    return (organisation_chain or "").split("||")[-1].strip()
