"""One address rule for every stage, and the SQL twin the mail queue uses."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from pipeline_core.db import engine, ensure_schema
from pipeline_core.emailcheck import clean_email, is_buyer_email, is_email, usable_contact


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("-sksalescompany@gmail.com", "sksalescompany@gmail.com"),
        ("mailto:a@b.com", "a@b.com"),
        ("(a@b.com)", "a@b.com"),
        ("a@b.com.", "a@b.com"),
        ("a@b.com;", "a@b.com"),
        ("•a@b.com", "a@b.com"),
        ("E-mail:-abc@x.com", "abc@x.com"),
        ("Email : abc@x.com", "abc@x.com"),
        ("  <office@roads.example>  ", "office@roads.example"),
        ("first.last@roads.example", "first.last@roads.example"),
        ("", ""),
        (None, ""),
    ],
)
def test_clean_email_drops_what_a_scan_left_at_the_edges(raw, clean):
    assert clean_email(raw) == clean


@pytest.mark.parametrize(
    "value",
    ["-a@b.com", "a@b", "a..b@c.com", "a@b.c", "a.@b.com", "a b@c.com", "a@-b.com", "@b.com"],
)
def test_is_email_rejects_a_damaged_address(value):
    assert not is_email(value)


@pytest.mark.parametrize(
    "value",
    ["a@b.com", "x.y+z@sub.mail.co.in", "O'Neil@x.com", "info_1@roads-ltd.example"],
)
def test_is_email_keeps_a_real_address(value):
    assert is_email(value)


def test_a_model_answer_keeps_the_clean_address():
    verdict = usable_contact("-sksalescompany@gmail.com", None)
    assert verdict.email == "sksalescompany@gmail.com"
    assert verdict.emails == ("sksalescompany@gmail.com",)
    assert verdict.dropped == ()


def test_two_spellings_of_one_mailbox_are_one_address():
    verdict = usable_contact("-sales@roads.example / SALES@roads.example", None)
    assert verdict.emails == ("sales@roads.example",)


@pytest.mark.parametrize(
    "value",
    ["info@thsti.res.in", "stores@iitb.ac.in", "office@cdac.ernet.in", "rfi@ord.gov.in", "eo@x.nic.in"],
)
def test_an_institute_or_government_address_is_the_buyer(value):
    assert is_buyer_email(value)
    assert usable_contact(value, None).email is None


def test_the_sql_twin_judges_like_python(tmp_path):
    bind = engine(tmp_path / "pipeline.sqlite3")
    ensure_schema(bind)
    values = ["a@b.com", "-a@b.com", "a@b", "info@thsti.res.in", None, ""]
    with bind.connect() as conn:
        for value in values:
            row = conn.execute(
                text("SELECT pc_is_email(:v), pc_is_buyer_email(:v)"), {"v": value}
            ).one()
            assert (bool(row[0]), bool(row[1])) == (is_email(value), is_buyer_email(value))
