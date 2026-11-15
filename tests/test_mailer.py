"""SmtpMailer against a real (local, in-process) SMTP server."""

import re
import socket
from collections.abc import Iterator

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.handlers import Message

from secret_santa.config import Settings
from secret_santa.mailer import SmtpMailer, assignment_message


class Inbox(Message):
    def __init__(self) -> None:
        super().__init__()
        self.messages = []

    def handle_message(self, message) -> None:  # noqa: ANN001
        self.messages.append(message)

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):  # noqa: ANN001, N802
        if address.endswith("@refused.example"):
            return "550 no such user"
        envelope.rcpt_tos.append(address)
        return "250 OK"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def smtp() -> Iterator[tuple[Inbox, int]]:
    inbox, port = Inbox(), _free_port()
    controller = Controller(inbox, hostname="127.0.0.1", port=port)
    controller.start()
    yield inbox, port
    controller.stop()


def _settings(port: int) -> Settings:
    return Settings(
        admin_password="x" * 8,
        secret_key="y" * 16,
        smtp_host="127.0.0.1",
        smtp_port=port,
        smtp_security="none",
        smtp_username="santa@yahoo.com",
        smtp_password="unused",
        smtp_from_name="Święty Mikołaj",
    )


def test_sends_utf8_and_reports_per_recipient(smtp: tuple[Inbox, int]) -> None:
    inbox, port = smtp
    settings = _settings(port)
    msgs = [
        assignment_message(
            settings,
            group_name="Wigilia 2026",
            group_message="Budżet: 100 zł",
            giver=giver,
            giver_email=email,
            receiver="Łucja Żółć",
        )
        for giver, email in [
            ("Anna", "anna@ok.example"),
            ("Bob", "bob@refused.example"),
            ("Celina", "celina@ok.example"),
        ]
    ]
    results = SmtpMailer(settings).send_many(msgs)

    assert results[0] is None and results[2] is None
    assert results[1] is not None and "550" in results[1]
    assert [m["X-RcptTo"] for m in inbox.messages] == ["anna@ok.example", "celina@ok.example"]
    body = inbox.messages[0].get_payload()[0].get_payload(decode=True).decode()
    assert "Łucja Żółć" in body and "Budżet: 100 zł" in body


def test_unconfigured_smtp_fails_every_message() -> None:
    settings = _settings(1).model_copy(update={"smtp_username": "", "smtp_password": ""})
    msg = assignment_message(
        settings, group_name="g", group_message="", giver="a", giver_email="a@x.pl", receiver="b"
    )
    results = SmtpMailer(settings).send_many([msg, msg])
    assert len(results) == 2 and all("nie jest skonfigurowane" in r for r in results)


def test_unreachable_server_is_tried_once_not_per_message() -> None:
    # Each attempt can take SMTP_TIMEOUT; retrying per message would multiply it by the group size.
    settings = _settings(_free_port()).model_copy(update={"smtp_timeout": 2})
    attempts = 0

    class Counting(SmtpMailer):
        def _connect(self):  # noqa: ANN202
            nonlocal attempts
            attempts += 1
            return super()._connect()

    msg = assignment_message(
        settings, group_name="g", group_message="", giver="a", giver_email="a@x.pl", receiver="b"
    )
    results = Counting(settings).send_many([msg] * 5)
    assert attempts == 1
    assert len(results) == 5 and all(r and "Connection" in r for r in results)


def test_drawn_name_stays_out_of_previews() -> None:
    """Inbox lists, phone notifications and the sender's Sent folder show the start of the body."""
    settings = Settings(admin_password="x" * 8, secret_key="y" * 16, smtp_username="s@yahoo.com")
    msg = assignment_message(
        settings,
        group_name="Rodzina",
        group_message="Budżet: 100 zł",
        giver="Anna",
        giver_email="anna@example.com",
        receiver="Bartek",
    )
    assert "Bartek" not in msg["Subject"]
    plain = msg.get_body(("plain",)).get_content()
    assert plain.index("Bartek") > 200
    html = msg.get_body(("html",)).get_content()
    preheader, body = html.split("</div>", 1)
    assert preheader.lstrip().startswith('<div style="display:none') and "Bartek" not in preheader
    visible = " ".join(re.sub(r"<[^>]+>", " ", body).split())
    assert visible.index("Bartek") > 200
