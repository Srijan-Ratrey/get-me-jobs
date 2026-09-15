"""Importing hand-researched HR contacts from a spreadsheet.

The rule this file mostly exists to defend is provenance: every contact row
records where its address came from, because "a spreadsheet" is not an answer
to a subject access request. A row without it is rejected and reported, never
quietly stored with a blank.
"""
from __future__ import annotations

import pytest

from jobhunter import db
from jobhunter.config import Target
from jobhunter.contacts.importer import (
    company_key,
    import_contacts,
    load_contact_csv,
)
from jobhunter.models import Company, Contact, Suppression, hash_email


@pytest.fixture
def session(tmp_path):
    db.init_db(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    with db.session_scope() as s:
        yield s


def write_csv(tmp_path, text: str):
    path = tmp_path / "hr.csv"
    path.write_text(text)
    return path


# --------------------------------------------------------------------------- #
# Provenance is mandatory
# --------------------------------------------------------------------------- #


def test_a_row_without_a_source_url_is_rejected(tmp_path):
    """Hard rule 4. A contact you cannot account for is a liability, not an asset."""
    path = write_csv(
        tmp_path,
        "company,email,source_url\n"
        "Acme,careers@acme.com,https://acme.com/careers\n"
        "Beta,hr@beta.com,\n",
    )
    rows, skipped = load_contact_csv(path)

    assert [r.company for r in rows] == ["Acme"]
    assert len(skipped) == 1
    assert "source_url" in skipped[0] and "hr@beta.com" in skipped[0]


def test_imported_contacts_record_how_they_were_found(session, tmp_path):
    path = write_csv(
        tmp_path, "company,email,source_url\nAcme,careers@acme.com,https://acme.com/careers\n"
    )
    rows, _ = load_contact_csv(path)
    import_contacts(session, rows)

    contact = session.query(Contact).one()
    assert contact.discovery_method == "manual"
    assert contact.source_url == "https://acme.com/careers"


# --------------------------------------------------------------------------- #
# Reading real spreadsheets
# --------------------------------------------------------------------------- #


def test_headers_are_matched_by_meaning_not_exact_text(tmp_path):
    """Real sheets say "HR Email" and "Career Page" and mean the obvious thing."""
    path = write_csv(
        tmp_path,
        "Employer,HR Email,Contact Name,Designation,Source URL\n"
        "Acme Corp,priya@acme.com,Priya Sharma,TA Lead,https://acme.com/about\n",
    )
    rows, skipped = load_contact_csv(path)

    assert skipped == []
    assert len(rows) == 1
    assert rows[0].company == "Acme Corp"
    assert rows[0].email == "priya@acme.com"
    assert rows[0].first_name == "Priya"
    assert rows[0].last_name == "Sharma"
    assert rows[0].role_title == "TA Lead"


def test_a_bom_and_blank_rows_do_not_break_the_import(tmp_path):
    path = write_csv(
        tmp_path,
        "﻿company,email,source_url\n"
        "Acme,careers@acme.com,https://acme.com/careers\n"
        ",,\n",
    )
    rows, skipped = load_contact_csv(path)
    assert len(rows) == 1
    assert skipped == []


def test_a_malformed_address_is_reported_not_stored(tmp_path):
    path = write_csv(
        tmp_path, "company,email,source_url\nAcme,not-an-email,https://acme.com/careers\n"
    )
    rows, skipped = load_contact_csv(path)
    assert rows == []
    assert "not an email" in skipped[0]


def test_a_sheet_with_no_email_column_fails_loudly(tmp_path):
    path = write_csv(tmp_path, "company,notes\nAcme,called them\n")
    rows, skipped = load_contact_csv(path)
    assert rows == []
    assert "no email column" in skipped[0]


# --------------------------------------------------------------------------- #
# Joining a spreadsheet's company names to the database
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "a,b",
    [
        ("Zensar", "Zensar Technologies"),
        ("Citi", "Citigroup"),
        ("Aera Technology", "Aeratechnology"),
        ("Wipro", "Wipro Limited"),
        ("Honeywell", "Honeywell International Inc."),
    ],
)
def test_spelling_variants_of_one_employer_share_a_key(a, b):
    assert company_key(a) == company_key(b)


@pytest.mark.parametrize(
    "a,b",
    [
        # Char-level suffix stripping would turn these into "cis", "tele",
        # "cost" and "tes", and a wrong merge attaches an HR contact to an
        # employer they do not work for and then mails them about it.
        ("Cisco", "Cis"),
        ("Telecom", "Tele"),
        ("Costco", "Cost"),
        ("Tesco", "Tes"),
        # And a company genuinely called "Group" must survive as itself.
        ("Group", "Grouper"),
    ],
)
def test_unrelated_names_are_never_merged(a, b):
    assert company_key(a) != company_key(b)


def test_an_imported_contact_attaches_to_the_existing_company_row(session, tmp_path):
    db.upsert_company(session, Target(name="Zensar"))
    path = write_csv(
        tmp_path,
        "company,email,source_url\nZensar Technologies,hr@zensar.com,https://zensar.com/careers\n",
    )
    rows, _ = load_contact_csv(path)
    report = import_contacts(session, rows)

    assert report.new_companies == 0, "created a duplicate company for a spelling variant"
    assert session.query(Company).count() == 1
    assert session.query(Contact).one().company_id == session.query(Company).one().id


# --------------------------------------------------------------------------- #
# Suppression and re-import
# --------------------------------------------------------------------------- #


def test_an_erased_address_is_not_resurrected_by_a_re_import(session, tmp_path):
    """Honouring erasure has to survive the user re-importing their sheet."""
    session.add(Suppression(email_hash=hash_email("careers@acme.com")))
    session.flush()
    path = write_csv(
        tmp_path, "company,email,source_url\nAcme,careers@acme.com,https://acme.com/careers\n"
    )
    rows, _ = load_contact_csv(path)
    report = import_contacts(session, rows)

    assert session.query(Contact).count() == 0
    assert report.skipped == 1
    assert "suppression" in report.reasons[0]


def test_re_importing_updates_rather_than_duplicating(session, tmp_path):
    path = write_csv(
        tmp_path, "company,email,source_url\nAcme,careers@acme.com,https://acme.com/careers\n"
    )
    rows, _ = load_contact_csv(path)
    import_contacts(session, rows)

    better = write_csv(
        tmp_path,
        "company,email,name,role,source_url\n"
        "Acme,careers@acme.com,Priya Sharma,TA Lead,https://acme.com/team\n",
    )
    rows2, _ = load_contact_csv(better)
    report = import_contacts(session, rows2)

    contact = session.query(Contact).one()
    assert report.updated == 1
    assert contact.first_name == "Priya"
    assert contact.source_url == "https://acme.com/team"


def test_a_role_address_is_classified_as_one(session, tmp_path):
    """Imported rows go through the same ranking as discovered ones."""
    path = write_csv(
        tmp_path,
        "company,email,source_url\n"
        "Acme,careers@acme.com,https://acme.com/careers\n"
        "Beta,anna.schmidt@beta.com,https://beta.com/team\n",
    )
    rows, _ = load_contact_csv(path)
    import_contacts(session, rows)

    kinds = {c.email: c.kind for c in session.query(Contact).all()}
    assert kinds["careers@acme.com"] == "role"
    assert kinds["anna.schmidt@beta.com"] == "person"


def test_dry_run_writes_nothing(session, tmp_path):
    path = write_csv(
        tmp_path, "company,email,source_url\nAcme,careers@acme.com,https://acme.com/careers\n"
    )
    rows, _ = load_contact_csv(path)
    report = import_contacts(session, rows, dry_run=True)

    assert report.added == 1
    assert session.query(Contact).count() == 0
