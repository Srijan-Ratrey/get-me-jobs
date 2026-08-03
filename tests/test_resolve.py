"""Importing a company list and resolving each one's ATS.

The stakes here are quiet ones: a CSV row that fails to parse is a company you
believe you are watching and are not, and a YAML append that loses comments
destroys the provenance of the hand-verified entries.
"""
from __future__ import annotations

import pytest
import respx

from jobhunter.config import (
    append_targets,
    domain_from_url,
    load_company_csv,
    load_targets,
)
from jobhunter.pipeline import run_resolve

# --------------------------------------------------------------------------- #
# CSV loading
# --------------------------------------------------------------------------- #

CSV = """Company,What they do,Career page,Funded,Area
Khatabook,Ledger app,https://khatabook.com/hiring/,Yes - Series C,HSR Layout
Zetwerk,Manufacturing marketplace,https://www.zetwerk.com/careers/,Yes,HSR Layout
"""


def write(tmp_path, text, name="companies.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_the_real_column_names(tmp_path):
    targets, skipped = load_company_csv(write(tmp_path, CSV))
    assert skipped == []
    assert [t.name for t in targets] == ["Khatabook", "Zetwerk"]
    assert targets[0].careers_url == "https://khatabook.com/hiring/"
    # www is stripped, because contact discovery resolves paths against this.
    assert targets[1].domain == "zetwerk.com"


@pytest.mark.parametrize(
    "header",
    [
        "Company,Career page",
        "company,careers_url",
        "Name,Careers URL",
        "Employer,Careers Page",
        "COMPANY,CAREER PAGE",
    ],
)
def test_header_aliases(tmp_path, header):
    targets, _ = load_company_csv(write(tmp_path, f"{header}\nAcme,https://acme.com/careers\n"))
    assert [t.name for t in targets] == ["Acme"]


def test_tolerates_a_utf8_bom(tmp_path):
    path = tmp_path / "bom.csv"
    path.write_text("Company,Career page\nAcme,https://acme.com/careers\n", encoding="utf-8-sig")
    targets, skipped = load_company_csv(path)
    assert [t.name for t in targets] == ["Acme"] and skipped == []


def test_skips_are_reported_not_swallowed(tmp_path):
    text = (
        "Company,Career page\n"
        "Acme,https://acme.com/careers\n"
        ",https://orphan.com/careers\n"  # no name
        "NoUrl,\n"  # no URL
        "BadUrl,acme.com/careers\n"  # not http
        "Acme,https://acme.com/jobs\n"  # duplicate
        ",\n"  # blank spacer, not worth reporting
    )
    targets, skipped = load_company_csv(write(tmp_path, text))
    assert [t.name for t in targets] == ["Acme"]
    assert len(skipped) == 4
    joined = " ".join(skipped)
    assert "no company name" in joined
    assert "NoUrl" in joined and "BadUrl" in joined and "duplicate" in joined


def test_explicit_domain_column_wins_over_the_derived_one(tmp_path):
    text = "Company,Career page,Domain\nAcme,https://careers.acme.io/,acme.com\n"
    targets, _ = load_company_csv(write(tmp_path, text))
    assert targets[0].domain == "acme.com"


def test_missing_required_columns_raise_clearly(tmp_path):
    with pytest.raises(ValueError, match="careers-URL column"):
        load_company_csv(write(tmp_path, "Company,Funded\nAcme,Yes\n"))
    with pytest.raises(ValueError, match="company-name column"):
        load_company_csv(write(tmp_path, "Career page,Funded\nhttps://x.com,Yes\n"))


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.acme.com/careers/", "acme.com"),
        ("https://acme.com:8443/careers", "acme.com"),
        ("https://careers.acme.co.in/jobs", "careers.acme.co.in"),
        (None, None),
        ("", None),
        # An ATS host is never the employer's domain. Plenty of companies list
        # their ATS URL as their careers page, so returning it would send contact
        # discovery to scrape the vendor for the employer's hiring address.
        ("https://apply.workable.com/exponent-energy/", None),
        ("https://jobs.lever.co/acme/1", None),
        ("https://boards.greenhouse.io/acme", None),
        ("https://jobs.ashbyhq.com/acme", None),
        ("https://acme.darwinbox.in/ms/candidate", None),
        ("https://careers-acme.icims.com/jobs", None),
    ],
)
def test_domain_from_url_never_returns_an_ats_host(url, expected):
    assert domain_from_url(url) == expected


def test_csv_row_pointing_at_an_ats_gets_no_domain(tmp_path):
    """Better no domain than the ATS vendor's: find_contacts handles None."""
    targets, _ = load_company_csv(
        write(tmp_path, "Company,Career page\nExponent,https://apply.workable.com/exponent/\n")
    )
    assert targets[0].domain is None
    assert targets[0].careers_url == "https://apply.workable.com/exponent/"


# --------------------------------------------------------------------------- #
# Appending to companies.yaml
# --------------------------------------------------------------------------- #

EXISTING = """# Companies to watch.
#
# Every ats_token was verified against the live board on 2026-07-31.
companies:
  - name: Sarvam AI
    domain: sarvam.ai
    ats: ashby
    ats_token: sarvam          # 63 jobs / 56 in India

# Not reachable by this pipeline - apply through their own portals:
#   Google - robots.txt disallows the careers results paths.
"""


def test_append_preserves_comments_and_existing_entries(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text(EXISTING)
    from jobhunter.config import Target

    added = append_targets(path, [Target(name="Khatabook", domain="khatabook.com", ats="lever", ats_token="khatabook")])
    assert added == 1

    text = path.read_text()
    # Every original line survives, including the provenance comments a
    # safe_dump round-trip would have discarded.
    for line in EXISTING.splitlines():
        assert line in text, f"append destroyed: {line!r}"
    assert "Google - robots.txt disallows" in text


def test_append_produces_parseable_yaml_after_a_trailing_comment_block(tmp_path):
    """Sequence items after a comment block are still part of the sequence."""
    path = tmp_path / "companies.yaml"
    path.write_text(EXISTING)
    from jobhunter.config import Target

    append_targets(path, [Target(name="Khatabook", ats="lever", ats_token="khatabook")])
    targets = load_targets(path)
    assert [t.name for t in targets] == ["Sarvam AI", "Khatabook"]
    assert targets[1].ats_token == "khatabook"


def test_append_skips_names_already_present(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text(EXISTING)
    from jobhunter.config import Target

    # Different casing, same company: must not be duplicated, and the verified
    # token already on file must not be overwritten.
    added = append_targets(path, [Target(name="sarvam ai", ats="greenhouse", ats_token="WRONG")])
    assert added == 0
    targets = load_targets(path)
    assert len(targets) == 1
    assert targets[0].ats_token == "sarvam"
    assert "WRONG" not in path.read_text()


def test_append_quotes_scalars_that_need_it(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text("companies:\n  - name: Existing\n")
    from jobhunter.config import Target

    append_targets(path, [Target(name="Foo: Bar & Co", ats="lever", ats_token="foo")])
    assert [t.name for t in load_targets(path)] == ["Existing", "Foo: Bar & Co"]


# --------------------------------------------------------------------------- #
# The resolution pass
# --------------------------------------------------------------------------- #

FILLER = "We are hiring across the company. " * 30


@respx.mock
async def test_run_resolve_sorts_into_four_buckets(tmp_path, allow_robots):
    from jobhunter.config import Target

    for host in ("gh", "workday", "spa", "gone"):
        allow_robots(f"https://{host}.com")

    respx.get("https://gh.com/careers").respond(
        200, text=f'<html><body><p>{FILLER}</p><a href="https://boards.greenhouse.io/acme">Roles</a></body></html>'
    )
    respx.get("https://workday.com/careers").respond(
        200, text=f'<html><body><p>{FILLER}</p><a href="https://acme.myworkdayjobs.com/x">Roles</a></body></html>'
    )
    respx.get("https://spa.com/careers").respond(
        200, text='<html><head><script src="/a.js"></script></head><body><div id="root"></div></body></html>'
    )
    respx.get("https://gone.com/careers").respond(404)

    targets = [
        Target(name="GH", careers_url="https://gh.com/careers", domain="gh.com"),
        Target(name="Workday", careers_url="https://workday.com/careers", domain="workday.com"),
        Target(name="Spa", careers_url="https://spa.com/careers", domain="spa.com"),
        Target(name="Gone", careers_url="https://gone.com/careers", domain="gone.com"),
    ]
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies: []\n")

    result = await run_resolve(targets, companies_path=companies, dry_run=False)

    assert [o.target.name for o in result.resolved] == ["GH"]
    assert result.resolved[0].ats == "greenhouse"
    assert result.resolved[0].token == "acme"
    assert [o.target.name for o in result.unsupported] == ["Workday"]
    assert result.unsupported[0].ats == "workday"
    assert [o.target.name for o in result.no_fingerprint] == ["Spa"]
    assert [o.target.name for o in result.unreachable] == ["Gone"]

    # Only the resolved company is written, with its discovered ATS and token.
    written = load_targets(companies)
    assert [(t.name, t.ats, t.ats_token) for t in written] == [("GH", "greenhouse", "acme")]
    assert result.added == 1


@respx.mock
async def test_run_resolve_dry_run_writes_nothing(tmp_path, allow_robots):
    from jobhunter.config import Target

    allow_robots("https://gh.com")
    respx.get("https://gh.com/careers").respond(
        200, text=f'<html><body><p>{FILLER}</p><a href="https://jobs.lever.co/acme/1">Roles</a></body></html>'
    )
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies: []\n")

    result = await run_resolve(
        [Target(name="GH", careers_url="https://gh.com/careers")],
        companies_path=companies,
        dry_run=True,
    )
    assert len(result.resolved) == 1 and result.added == 0
    assert load_targets(companies) == []


@respx.mock
async def test_robots_disallowed_is_unreachable_not_a_crash(tmp_path):
    from jobhunter.config import Target

    respx.get("https://blocked.com/robots.txt").respond(200, text="User-agent: *\nDisallow: /")
    result = await run_resolve(
        [Target(name="Blocked", careers_url="https://blocked.com/careers")],
        companies_path=tmp_path / "companies.yaml",
        dry_run=True,
    )
    assert [o.target.name for o in result.unreachable] == ["Blocked"]
    assert "robots" in result.unreachable[0].detail


@respx.mock
async def test_supported_ats_without_a_token_is_reported_not_guessed(tmp_path, allow_robots):
    """Seeing "greenhouse" in the HTML is not the same as knowing the board."""
    from jobhunter.config import Target

    allow_robots("https://vague.com")
    respx.get("https://vague.com/careers").respond(
        200, text=f"<html><body><p>{FILLER}</p><p>Powered by grnhse widgets</p></body></html>"
    )
    result = await run_resolve(
        [Target(name="Vague", careers_url="https://vague.com/careers")],
        companies_path=tmp_path / "companies.yaml",
        dry_run=True,
    )
    assert result.resolved == []
    assert [o.target.name for o in result.no_fingerprint] == ["Vague"]
    assert "no board token" in result.no_fingerprint[0].detail


def test_unsupported_tally_names_the_highest_value_adapter():
    from jobhunter.config import Target
    from jobhunter.pipeline import ResolveOutcome, ResolveResult

    result = ResolveResult(
        unsupported=[
            ResolveOutcome(Target(name=n), "unsupported", ats=a)
            for n, a in [("a", "darwinbox"), ("b", "darwinbox"), ("c", "keka"), ("d", "darwinbox")]
        ]
    )
    assert list(result.unsupported_by_ats()) == ["darwinbox", "keka"]
    assert result.unsupported_by_ats()["darwinbox"] == 3


def test_append_rewrites_a_flow_style_empty_list(tmp_path):
    """`companies: []` cannot take appended block items; it must be converted."""
    from jobhunter.config import Target

    path = tmp_path / "companies.yaml"
    path.write_text("# my list\ncompanies: []\n")
    assert append_targets(path, [Target(name="Acme", ats="lever", ats_token="acme")]) == 1
    assert [t.name for t in load_targets(path)] == ["Acme"]
    assert "# my list" in path.read_text()


def test_append_creates_the_key_when_the_file_has_none(tmp_path):
    from jobhunter.config import Target

    path = tmp_path / "companies.yaml"
    path.write_text("# just a comment\n")
    assert append_targets(path, [Target(name="Acme", ats="lever", ats_token="acme")]) == 1
    assert [t.name for t in load_targets(path)] == ["Acme"]


def test_append_refuses_an_inline_list_rather_than_corrupting_it(tmp_path):
    from jobhunter.config import Target

    path = tmp_path / "companies.yaml"
    original = "companies: [{name: Acme}]\n"
    path.write_text(original)
    with pytest.raises(ValueError, match="inline list"):
        append_targets(path, [Target(name="New", ats="lever", ats_token="new")])
    # Left exactly as it was.
    assert path.read_text() == original
