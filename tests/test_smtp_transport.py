"""SmtpTransport, with the suite's socket block left armed.

The connection is injected, so none of this opens a socket. What is worth
pinning down is the part that is easy to get subtly wrong and only discover
against a live server: which account it authenticates as, that a pasted
app password works with its spaces in, and that a refusal is loud.
"""

from __future__ import annotations

from email.message import EmailMessage

import pytest
from pydantic import SecretStr

from jobhunter.outreach import sender


class FakeServer:
    def __init__(self, refused: dict | None = None) -> None:
        self.refused = refused or {}
        self.logins: list[tuple[str, str]] = []
        self.sent: list[EmailMessage] = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def login(self, user, password):
        self.logins.append((user, password))

    def send_message(self, message):
        self.sent.append(message)
        return self.refused


def message(to: str = "careers@example.com") -> EmailMessage:
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = "Ada Lovelace <ada@example.com>"
    msg["Subject"] = "Application"
    msg.set_content("body")
    return msg


def transport(server: FakeServer, password: str = "abcdefghijklmnop") -> sender.SmtpTransport:
    return sender.SmtpTransport(password=SecretStr(password), connect=lambda: server)


def test_authenticates_as_the_from_address():
    """The account is taken from the message, not a separate setting to drift."""
    server = FakeServer()
    transport(server).send(message())
    assert server.logins == [("ada@example.com", "abcdefghijklmnop")]


def test_a_pasted_app_password_keeps_its_spaces_out_of_the_login():
    """Google shows the code in four groups; that is how it gets pasted."""
    server = FakeServer()
    transport(server, password="abcd efgh ijkl mnop").send(message())
    assert server.logins[0][1] == "abcdefghijklmnop"


def test_returns_a_message_id_it_actually_set():
    """SMTP hands back no id, so the outreach row would otherwise have no handle."""
    server = FakeServer()
    returned = transport(server).send(message())
    assert returned
    assert server.sent[0]["Message-ID"] == returned


def test_an_existing_message_id_is_left_alone():
    server = FakeServer()
    msg = message()
    msg["Message-ID"] = "<already-set@example.com>"
    assert transport(server).send(msg) == "<already-set@example.com>"


def test_a_refused_recipient_raises_rather_than_reporting_success():
    server = FakeServer(refused={"careers@example.com": (550, b"no such user")})
    with pytest.raises(RuntimeError, match="refused"):
        transport(server).send(message())


def test_the_connection_is_closed_even_on_refusal():
    server = FakeServer(refused={"careers@example.com": (550, b"no")})
    with pytest.raises(RuntimeError):
        transport(server).send(message())
    assert server.closed


def test_no_password_anywhere_is_a_readable_error(monkeypatch):
    monkeypatch.setattr(sender.settings, "gmail_app_password", None)
    with pytest.raises(RuntimeError, match="APP_PASSWORD"):
        sender.SmtpTransport()


def test_an_app_password_selects_smtp(monkeypatch):
    monkeypatch.setattr(sender.settings, "gmail_app_password", SecretStr("abcdefghijklmnop"))
    assert isinstance(sender.build_transport(), sender.SmtpTransport)


def test_without_one_it_falls_back_to_oauth(monkeypatch):
    """No app password means the send-only Gmail client, not a silent no-op."""
    monkeypatch.setattr(sender.settings, "gmail_app_password", None)
    built: list[str] = []
    monkeypatch.setattr(
        sender.GmailTransport, "_build_service", staticmethod(lambda: built.append("oauth"))
    )
    assert isinstance(sender.build_transport(), sender.GmailTransport)
    assert built == ["oauth"]
