"""Another site cannot post to this window, and no form sends the browser elsewhere."""

from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pipeline_core.db import engine, ensure_schema, session
from pipeline_core.queries import mark_enriched, set_typed_contact, upsert_vendor

from ops_ui.app import create_app

def _routes(app):
    """FastAPI keeps an included router whole, so its routes are one level down."""
    for route in app.routes:
        router = getattr(route, "original_router", None)
        yield from router.routes if router is not None else [route]


_POSTS = sorted(
    route.path
    for route in _routes(create_app())
    if isinstance(route, APIRoute) and "POST" in route.methods
)


def _seeded(tmp_path) -> tuple[TestClient, int]:
    path = tmp_path / "pipeline.sqlite3"
    ensure_schema(engine(path))
    with session(engine(path)) as current:
        vendor_id = upsert_vendor(current, name_raw="Has Email Ltd")
        mark_enriched(current, vendor_id, email="a@b.example", phone=None)
    return TestClient(create_app(path)), vendor_id


def test_every_form_route_is_checked():
    assert "/selection/lookup" in _POSTS
    assert "/vendor/{vendor_id}/contact" in _POSTS
    assert len(_POSTS) >= 20


@pytest.mark.parametrize("route", _POSTS)
def test_a_post_from_another_site_is_refused(tmp_path, route):
    client, vendor_id = _seeded(tmp_path)
    url = re.sub(r"\{[^}]+\}", "1", route).replace("/vendor/1", f"/vendor/{vendor_id}")

    answer = client.post(url, headers={"Origin": "https://evil.example"}, follow_redirects=False)

    assert answer.status_code == 403
    assert answer.text == "This form can only be sent from this computer."


@pytest.mark.parametrize("origin", [None, "http://127.0.0.1:8000", "http://localhost:8000"])
def test_a_post_from_this_computer_goes_through(tmp_path, origin):
    client, vendor_id = _seeded(tmp_path)
    headers = {"Origin": origin} if origin else {}

    answer = client.post(
        f"/vendor/{vendor_id}/do-not-mail",
        data={"do_not_mail": "1", "back": "/mail"},
        headers=headers,
        follow_redirects=False,
    )

    assert answer.status_code == 303


@pytest.mark.parametrize(
    "back",
    [
        "https://evil.example/",
        "//evil.example/path",
        "javascript:alert(1)",
        "/\\evil.example",
        "/\t/evil.example",
    ],
)
@pytest.mark.parametrize("form", ["contact", "remove", "do-not-mail"])
def test_back_never_leaves_this_window(tmp_path, form, back):
    client, vendor_id = _seeded(tmp_path)
    with session(engine(tmp_path / "pipeline.sqlite3")) as current:
        set_typed_contact(current, vendor_id, email="typed@b.example", phone="")
        from pipeline_core.models import VendorContact

        contact_id = current.query(VendorContact.contact_id).filter_by(source="typed").scalar()
    url, data = {
        "contact": (f"/vendor/{vendor_id}/contact", {"email": "new@b.example"}),
        "remove": (f"/vendor/{vendor_id}/contact/{contact_id}/remove", {}),
        "do-not-mail": (f"/vendor/{vendor_id}/do-not-mail", {"do_not_mail": "1"}),
    }[form]

    answer = client.post(url, data={**data, "back": back}, follow_redirects=False)

    assert answer.status_code == 303
    location = answer.headers["location"]
    assert location.startswith("/") and not location.startswith("//")
    assert "evil" not in location and "javascript" not in location


def test_a_local_back_link_is_kept(tmp_path):
    client, vendor_id = _seeded(tmp_path)
    back = "/award?tender_id=T-1&bid=1&back=%2Fmail"

    answer = client.post(
        f"/vendor/{vendor_id}/do-not-mail",
        data={"do_not_mail": "1", "back": back},
        follow_redirects=False,
    )

    assert answer.headers["location"] == back


def test_no_page_shows_a_secret(tmp_path, monkeypatch):
    """NFR-3: the window may say a setting is missing, never what it holds."""
    secrets = {
        "OUTLOOK_USER": "me@company.example",
        "OUTLOOK_PASSWORD": "Pw-6c1f-NEVER-SHOWN",
        "GMAIL_APP_PASSWORD": "gm-4d2e-NEVER-SHOWN",
        "GRAPH_CLIENT_ID": "id-9a7b-NEVER-SHOWN",
        "OPENROUTER_API_KEY": "sk-or-8e3a-NEVER-SHOWN",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    client, vendor_id = _seeded(tmp_path)

    pages = [client.get(url).text for url in ["/", "/scrape", "/enrich", "/mail", "/jobs"]]
    pages.append(client.post("/mail", data={"mode": "preflight", "transport": "gmail"}).text)
    pages.append(client.post("/mail", data={"mode": "send", "award": f"{vendor_id}:T-1"}).text)

    for page in pages:
        for name, value in secrets.items():
            if name != "OUTLOOK_USER":
                assert value not in page, name
