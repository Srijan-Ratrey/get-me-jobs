"""Contact discovery: the three tiers.

The bar these tests hold the module to is the one from docs/contact-discovery.md:
one high-confidence contact beats fifty guesses, and a guess presented as a real
address is worse than admitting you found nothing.
"""
from __future__ import annotations

import pytest
import respx

from jobhunter.config import Target
from jobhunter.contacts import patterns, verify
from jobhunter.contacts.finder import GOOD_ENOUGH, SURFACE_THRESHOLD, find_contacts
from jobhunter.contacts.scraper import (
    decode_cfemail,
    harvest,
    is_acceptable,
    looks_like_person,
    rank,
    scrape_company,
)

TEAM_PAGE = """
<html><body>
  <h1>Our team</h1>
  <p>General enquiries: <a href="mailto:careers@acme.com?subject=Hi">careers@acme.com</a></p>
  <div class="person">
    <h3>Anna Schmidt</h3>
    <p>Talent Acquisition Partner</p>
    <a href="mailto:anna.schmidt@acme.com">Email Anna</a>
  </div>
  <p>Press: press@acme.com. Support: support@acme.com. noreply@acme.com</p>
  <p>Our agency partner: recruiter@headhunters.example</p>
  <script>var analytics = {"id":"\\u003ePress\\u003c","mail":"tracking@vendor.example"};</script>
</body></html>
"""


# --------------------------------------------------------------------------- #
# Tier 1: extraction and ranking
# --------------------------------------------------------------------------- #


def test_harvest_prefers_role_address_and_finds_the_person():
    """The acceptance criterion: careers@ first at 0.95, above a named person."""
    found = harvest(TEAM_PAGE, "https://acme.com/team", "acme.com", is_html=True)
    by_email = {c.email: c for c in found}

    assert found[0].email == "careers@acme.com"
    assert found[0].confidence == 0.95
    assert found[0].kind == "role"

    anna = by_email["anna.schmidt@acme.com"]
    assert anna.kind == "person"
    assert (anna.first_name, anna.last_name) == ("Anna", "Schmidt")
    # A recruiting-ish title nearby lifts a named person above the unknown case.
    assert anna.confidence == 0.80
    assert anna.confidence < found[0].confidence


def test_harvest_rejects_the_reject_list_and_offdomain():
    emails = {c.email for c in harvest(TEAM_PAGE, "https://acme.com/team", "acme.com", is_html=True)}
    assert "press@acme.com" not in emails
    assert "support@acme.com" not in emails
    assert "noreply@acme.com" not in emails
    # Vendor/agency addresses are not this company's contacts.
    assert "recruiter@headhunters.example" not in emails
    assert "tracking@vendor.example" not in emails


def test_harvest_ignores_script_bodies():
    """Inline JSON is not prose; its escaped punctuation becomes fake local parts."""
    emails = {c.email for c in harvest(TEAM_PAGE, "https://acme.com/t", "acme.com", is_html=True)}
    assert not any(e.startswith("u003e") for e in emails)


def test_json_escapes_are_decoded_before_matching():
    html = '<html><body>Contact \\u003ecareers@acme.com\\u003c today</body></html>'
    emails = {c.email for c in harvest(html, "https://acme.com/", "acme.com", is_html=True)}
    assert emails == {"careers@acme.com"}


def test_every_candidate_carries_provenance():
    for candidate in harvest(TEAM_PAGE, "https://acme.com/team", "acme.com", is_html=True):
        assert candidate.source_url == "https://acme.com/team"
        assert candidate.discovery_method.startswith("scraped:")


def test_mailto_subject_is_stripped_and_percent_decoding_applied():
    html = '<a href="mailto:careers%40acme.com?subject=Application">Apply</a>'
    found = harvest(html, "https://acme.com/", "acme.com", is_html=True)
    assert [c.email for c in found] == ["careers@acme.com"]
    assert found[0].discovery_method == "scraped:mailto"


def test_obfuscated_addresses_are_normalized():
    html = "<p>Write to jobs [at] acme [dot] com or hr (at) acme (dot) com</p>"
    emails = {c.email for c in harvest(html, "https://acme.com/", "acme.com", is_html=True)}
    assert emails == {"jobs@acme.com", "hr@acme.com"}


def test_prose_containing_at_is_not_mistaken_for_an_address():
    html = "<p>Look at that. Come and work at acme with us.</p>"
    assert harvest(html, "https://acme.com/", "acme.com", is_html=True) == []


def test_decode_cfemail():
    # "careers@acme.com" XORed against key 0x7a.
    plain = "careers@acme.com"
    key = 0x7A
    encoded = f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in plain)
    assert decode_cfemail(encoded) == plain
    assert decode_cfemail("zz") is None
    assert decode_cfemail("") is None


def test_cfemail_is_extracted_from_markup():
    plain = "careers@acme.com"
    key = 0x21
    encoded = f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in plain)
    html = f'<p>Email <a href="#" data-cfemail="{encoded}">protected</a></p>'
    found = harvest(html, "https://acme.com/", "acme.com", is_html=True)
    assert [(c.email, c.discovery_method) for c in found] == [(plain, "scraped:cfemail")]


def test_subdomains_of_the_company_domain_are_accepted():
    assert is_acceptable("careers@jobs.acme.com", "acme.com") is True
    assert is_acceptable("careers@acme.com", "www.acme.com") is True
    assert is_acceptable("careers@notacme.com", "acme.com") is False


def test_asset_extensions_are_not_addresses():
    assert is_acceptable("sprite@2x.png", "acme.com") is False


@pytest.mark.parametrize(
    "local,expected",
    [
        ("anna.schmidt", True),
        ("a_schmidt", True),
        ("anna-schmidt", True),
        ("careers", False),
        ("info-jp", False),  # a regional role inbox, not a person
        ("careers-india", False),
        ("pno", False),  # opaque single word
        ("x.y", False),  # initials only
    ],
)
def test_looks_like_person(local, expected):
    assert looks_like_person(local) is expected


def test_unrecognised_local_stays_below_the_surfacing_threshold():
    """"future@" and friends must not be presented as hiring contacts."""
    confidence, kind, _ = rank("future@acme.com")
    assert confidence < SURFACE_THRESHOLD


def test_regional_role_inbox_keeps_its_ranking():
    assert rank("careers-india@acme.com")[0] == 0.95
    assert rank("info-jp@acme.com")[0] == 0.50


def test_job_description_addresses_are_harvested():
    text = "Apply now. Questions? Email jobs@acme.com and we'll respond."
    found = harvest(text, "https://boards.greenhouse.io/acme/jobs/1", "acme.com", is_html=False)
    assert [c.email for c in found] == ["jobs@acme.com"]
    assert found[0].source_url == "https://boards.greenhouse.io/acme/jobs/1"


# --------------------------------------------------------------------------- #
# Tier 2: pattern generation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name,first,last",
    [
        ("Anna Schmidt", "anna", "schmidt"),
        ("Änna Schmïdt", "anna", "schmidt"),  # accents stripped
        ("Søren Ørsted", "soren", "orsted"),  # no NFKD decomposition
        ("Seán O'Brien", "sean", "obrien"),  # apostrophe dropped
        ("Jan van der Berg", "jan", "vanderberg"),  # multi-part surname
        ("Maria Jose Garcia Lopez", "maria", "josegarcialopez"),  # first given name only
        ("Dr. Anna Schmidt Jr", "anna", "schmidt"),  # honorific and suffix dropped
        ("Schmidt, Anna", "anna", "schmidt"),  # inverted
        ("François Müller", "francois", "muller"),
    ],
)
def test_normalize_name(name, first, last):
    parts = patterns.normalize_name(name)
    assert parts is not None
    assert (parts.first, parts.last) == (first, last)


def test_normalize_name_rejects_a_single_token():
    assert patterns.normalize_name("Anna") is None
    assert patterns.normalize_name("") is None
    assert patterns.normalize_name("   ") is None


def test_generate_is_capped_and_ordered_by_prevalence():
    out = patterns.generate("Anna Schmidt", "acme.com", max_guesses=6)
    assert len(out) == 6
    assert out[0][0] == "anna.schmidt@acme.com"
    # Confidences are deliberately below the surfacing threshold.
    assert all(confidence < SURFACE_THRESHOLD for _, confidence, _ in out)
    assert [c for _, c, _ in out] == sorted((c for _, c, _ in out), reverse=True)


def test_infer_pattern_from_a_known_address():
    parts = patterns.normalize_name("Anna Schmidt")
    assert patterns.infer_pattern("anna.schmidt@acme.com", parts) == "first.last"
    assert patterns.infer_pattern("aschmidt@acme.com", parts) == "flast"
    assert patterns.infer_pattern("a.schmidt@acme.com", parts) == "f.last"
    assert patterns.infer_pattern("nothing-like-it@acme.com", parts) is None


def test_inference_produces_one_confident_candidate_not_six_weak_ones():
    """The acceptance criterion from TASKS.md 1.7."""
    guesses = patterns.generate("Bob Jones", "acme.com", max_guesses=6)
    assert len(guesses) == 6
    assert all(c < 0.5 for _, c, _ in guesses)

    pattern = patterns.infer_pattern_from_directory([("Anna Schmidt", "anna.schmidt@acme.com")])
    assert pattern == "first.last"
    inferred = patterns.generate("Bob Jones", "acme.com", known_pattern=pattern)
    assert inferred == [("bob.jones@acme.com", patterns.INFERRED_CONFIDENCE, "first.last")]
    assert inferred[0][1] >= GOOD_ENOUGH


def test_generate_returns_nothing_for_an_unusable_name():
    assert patterns.generate("Anna", "acme.com") == []


# --------------------------------------------------------------------------- #
# Tier 3: the verification state machine
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_smtp(monkeypatch):
    """Drive the state machine without a resolver or a mail server."""
    state: dict = {"mx": ["mx.acme.com"], "codes": {}, "default": 550, "probed": []}

    def resolve_mx(domain: str) -> list[str]:
        return state["mx"]

    def probe(mx_host: str, recipients: list[str]) -> dict[str, int]:
        state["probed"].extend(recipients)
        return {r: state["codes"].get(r, state["default"]) for r in recipients}

    monkeypatch.setattr(verify, "resolve_mx", resolve_mx)
    monkeypatch.setattr(verify, "probe_recipients", probe)
    # No real sleeping between probes in tests.
    monkeypatch.setattr(verify.time, "sleep", lambda _: None)
    return state


def test_no_mx_is_invalid(fake_smtp):
    fake_smtp["mx"] = []
    result = verify.verify_domain("acme.com")
    assert result.status == verify.INVALID


def test_catch_all_domain_is_risky_never_valid(fake_smtp):
    """Reporting false confidence is worse than reporting nothing."""
    fake_smtp["default"] = 250  # accepts everything, including the random probe
    domain_result = verify.verify_domain("acme.com")
    assert domain_result.status == verify.RISKY
    assert domain_result.catch_all is True

    results, catch_all = verify.verify_candidates("acme.com", ["anna@acme.com"])
    assert catch_all is True
    assert results["anna@acme.com"].status == verify.RISKY
    assert all(r.status != verify.VALID for r in results.values())


def test_cached_catch_all_avoids_reprobing(fake_smtp):
    result = verify.verify_domain("acme.com", known_catch_all=True)
    assert result.status == verify.RISKY
    assert fake_smtp["probed"] == []


def test_opaque_mx_is_unknown_by_design(fake_smtp):
    """Google and Microsoft do not leak recipient validity; that is not a negative."""
    fake_smtp["mx"] = ["aspmx.l.google.com"]
    result = verify.verify_domain("acme.com")
    assert result.status == verify.UNKNOWN
    assert fake_smtp["probed"] == []

    fake_smtp["mx"] = ["acme-com.mail.protection.outlook.com"]
    assert verify.verify_domain("acme.com").status == verify.UNKNOWN


@pytest.mark.parametrize(
    "code,expected",
    [
        (250, verify.VALID),
        (251, verify.VALID),
        (550, verify.INVALID),
        (551, verify.INVALID),
        (553, verify.INVALID),
        (450, verify.UNKNOWN),
        (451, verify.UNKNOWN),
        (452, verify.UNKNOWN),
        (421, verify.UNKNOWN),
        (0, verify.UNKNOWN),
    ],
)
def test_rcpt_codes_map_to_statuses(fake_smtp, code, expected):
    fake_smtp["codes"] = {"anna@acme.com": code}
    fake_smtp["default"] = 550  # so the catch-all probe is rejected
    result = verify.verify_email("anna@acme.com", mx_hosts=["mx.acme.com"], delay=0)
    assert result.status == expected


def test_verification_stops_at_the_first_valid(fake_smtp):
    fake_smtp["codes"] = {"b@acme.com": 250}
    results, _ = verify.verify_candidates(
        "acme.com", ["a@acme.com", "b@acme.com", "c@acme.com"], max_probes=6
    )
    assert results["b@acme.com"].status == verify.VALID
    assert "c@acme.com" not in results


def test_probe_count_is_capped(fake_smtp):
    emails = [f"guess{i}@acme.com" for i in range(20)]
    results, _ = verify.verify_candidates("acme.com", emails, max_probes=3)
    assert len(results) == 3


def test_smtp_probe_never_issues_data():
    """A structural check: the probe must be incapable of sending a message.

    Looks for the actual SMTP verbs rather than the word, so the docstring
    explaining the rule does not trip the test enforcing it.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(verify.probe_recipients).strip())
    literals = {
        node.value.upper()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not any(text.startswith("DATA") for text in literals)

    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "sendmail" not in calls
    assert "send_message" not in calls
    # And the verbs it *does* use are the harmless ones.
    assert "docmd" in calls and "rset" in calls


# --------------------------------------------------------------------------- #
# The finder
# --------------------------------------------------------------------------- #


@respx.mock
async def test_finder_stops_at_a_role_address(polite_client, allow_robots):
    """Tier 1 found a 0.95 role address, so no guessing should happen."""
    allow_robots("https://acme.com")
    respx.get("https://acme.com/careers").respond(200, text=TEAM_PAGE)
    respx.route(host="acme.com").respond(404)

    async with polite_client() as client:
        result = await find_contacts(
            client,
            Target(name="Acme", domain="acme.com"),
            person_names=[("Bob Jones", "https://acme.com/team")],
        )

    assert result.candidates[0].email == "careers@acme.com"
    assert result.candidates[0].confidence >= GOOD_ENOUGH
    # No pattern guesses were generated.
    assert not any(c.discovery_method.startswith("pattern:") for c in result.candidates)


@respx.mock
async def test_finder_returns_nothing_rather_than_a_guess(polite_client, allow_robots):
    """"No contact found - apply through the posting" is an honest answer."""
    allow_robots("https://acme.com")
    respx.route(host="acme.com").respond(404)

    async with polite_client() as client:
        result = await find_contacts(client, Target(name="Acme", domain="acme.com"))

    assert result.surfaced() == []


@respx.mock
async def test_finder_does_not_guess_without_a_real_name(polite_client, allow_robots):
    """Never generate patterns from a name you invented."""
    allow_robots("https://acme.com")
    respx.route(host="acme.com").respond(404)

    async with polite_client() as client:
        result = await find_contacts(client, Target(name="Acme", domain="acme.com"))

    assert result.candidates == []
    assert any("no real person name" in note for note in result.notes)


async def test_finder_needs_a_domain(polite_client):
    async with polite_client() as client:
        result = await find_contacts(client, Target(name="Acme"))
    assert result.candidates == []
    assert any("no domain" in note for note in result.notes)


@respx.mock
async def test_finder_never_returns_a_suppressed_address(polite_client, allow_robots):
    """Erasure must apply at discovery, not only at the DB write.

    Refusing the insert while still displaying, exporting, or SMTP-probing the
    address is not honouring the request.
    """
    allow_robots("https://acme.com")
    respx.get("https://acme.com/careers").respond(200, text=TEAM_PAGE)
    respx.route(host="acme.com").respond(404)

    async with polite_client() as client:
        result = await find_contacts(
            client,
            Target(name="Acme", domain="acme.com"),
            is_suppressed=lambda email: email == "careers@acme.com",
        )

    emails = {c.email for c in result.candidates}
    assert "careers@acme.com" not in emails
    assert "anna.schmidt@acme.com" in emails


@respx.mock
async def test_suppressed_pattern_guesses_are_never_probed(polite_client, allow_robots, monkeypatch):
    allow_robots("https://acme.com")
    respx.route(host="acme.com").respond(404)
    probed: list[str] = []
    monkeypatch.setattr(verify, "resolve_mx", lambda d: ["mx.acme.com"])
    monkeypatch.setattr(
        verify,
        "probe_recipients",
        lambda host, rcpts: (probed.extend(rcpts), {r: 550 for r in rcpts})[1],
    )
    monkeypatch.setattr(verify.time, "sleep", lambda _: None)

    async with polite_client() as client:
        await find_contacts(
            client,
            Target(name="Acme", domain="acme.com"),
            person_names=[("Anna Schmidt", "https://acme.com/team")],
            verify_emails=True,
            is_suppressed=lambda email: email.startswith("anna"),
        )

    assert not any(p.startswith("anna") for p in probed), f"probed a suppressed address: {probed}"
