"""Sending email over SMTP (Yahoo Mail by default)."""

import contextlib
import logging
import smtplib
import ssl
from collections.abc import Sequence
from email.headerregistry import Address
from email.message import EmailMessage
from html import escape
from typing import Protocol

from secret_santa.config import Settings

log = logging.getLogger(__name__)


class Mailer(Protocol):
    def send_many(self, messages: Sequence[EmailMessage]) -> list[str | None]:
        """Send each message. Returns one entry per message: None if sent, else an error text."""
        ...


class SmtpMailer:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def _connect(self) -> smtplib.SMTP:
        s = self.s
        if not s.smtp_username or not s.smtp_password:
            raise RuntimeError("SMTP nie jest skonfigurowane (brak SMTP_USERNAME / SMTP_PASSWORD)")
        context = ssl.create_default_context()
        if s.smtp_security == "ssl":
            conn: smtplib.SMTP = smtplib.SMTP_SSL(
                s.smtp_host, s.smtp_port, timeout=s.smtp_timeout, context=context
            )
        else:
            conn = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout)
            if s.smtp_security == "starttls":
                conn.starttls(context=context)
        if s.smtp_security != "none":
            conn.login(s.smtp_username, s.smtp_password)
        return conn

    def send_many(self, messages: Sequence[EmailMessage]) -> list[str | None]:
        results: list[str | None] = []
        conn: smtplib.SMTP | None = None
        try:
            for i, msg in enumerate(messages):
                if conn is None:
                    try:
                        conn = self._connect()
                    except (smtplib.SMTPException, OSError, RuntimeError) as exc:
                        # Unreachable server or bad credentials: retrying per message would only
                        # multiply the timeout, so fail the rest at once.
                        error = f"{type(exc).__name__}: {exc}"
                        log.warning("cannot connect to the SMTP server: %s", error)
                        results.extend([error] * (len(messages) - i))
                        break
                try:
                    try:
                        conn.send_message(msg)
                    except smtplib.SMTPServerDisconnected:
                        # Servers drop idle or long-lived sessions; reconnect once and retry.
                        conn = self._connect()
                        conn.send_message(msg)
                    results.append(None)
                except (smtplib.SMTPException, OSError) as exc:
                    log.warning("sending to %s failed: %s", msg["To"], exc)
                    results.append(f"{type(exc).__name__}: {exc}")
                    if not isinstance(exc, smtplib.SMTPRecipientsRefused):
                        # Connection state unknown: drop it and start fresh for the next one.
                        conn.close()
                        conn = None
        finally:
            if conn is not None:
                with contextlib.suppress(smtplib.SMTPException, OSError):
                    conn.quit()
        return results


def build_message(
    settings: Settings, *, to_name: str, to_email: str, subject: str, text: str, html: str
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    if settings.sender_address:  # if unset, SmtpMailer reports "not configured" per message
        msg["From"] = Address(
            display_name=settings.smtp_from_name, addr_spec=settings.sender_address
        )
    msg["To"] = Address(display_name=to_name, addr_spec=to_email)
    # quoted-printable keeps the whole message 7-bit clean, whatever the server advertises.
    msg.set_content(text, cte="quoted-printable")
    msg.add_alternative(html, subtype="html", cte="quoted-printable")
    return msg


# Hidden "preheader": mail clients show it as the preview line instead of the start of the body.
# The padding (invisible characters) fills the rest of the preview so the drawn name never shows up
# in an inbox list, a phone notification, or the sender's "Sent" folder.
_PREHEADER = (
    '<div style="display:none;max-height:0;overflow:hidden;opacity:0;mso-hide:all">'
    "Wynik losowania jest w środku. Otwórz, kiedy nikt nie patrzy &#129323;"
    + "&#847;&zwnj;&nbsp;" * 90
    + "</div>"
)


def assignment_message(
    settings: Settings,
    *,
    group_name: str,
    group_message: str,
    giver: str,
    giver_email: str,
    receiver: str,
) -> EmailMessage:
    # Both parts open with a spoiler-free paragraph long enough to fill any preview, in case a
    # client builds it from the plain text or ignores the hidden preheader.
    intro = (
        f"Losowanie w grupie „{group_name}” zakończone. Imię osoby, której kupujesz prezent, "
        "jest niżej, żeby nie mignęło nikomu w podglądzie wiadomości ani w powiadomieniu "
        "na telefonie."
    )
    extra_text = f"\n{group_message.strip()}\n" if group_message.strip() else ""
    text = (
        f"Cześć {giver}!\n\n"
        f"{intro}\n\n\n"
        "Kupujesz prezent dla tej osoby:\n\n"
        f"    {receiver}\n"
        f"{extra_text}\n"
        "Ciii… nie mów nikomu! 🤫\n"
    )
    extra_html = (
        f'<p style="white-space:pre-line;color:#444">{escape(group_message.strip())}</p>'
        if group_message.strip()
        else ""
    )
    html = f"""\
{_PREHEADER}
<div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:480px;
            margin:auto;padding:24px;border-radius:12px;background:#fff8f0;color:#222">
  <h2 style="margin-top:0;color:#b3261e">&#127877; Secret Santa: {escape(group_name)}</h2>
  <p>Cześć {escape(giver)}!</p>
  <p>{escape(intro)}</p>
  <p>Kupujesz prezent dla tej osoby:</p>
  <p style="font-size:28px;font-weight:700;text-align:center;margin:24px 0;color:#1b5e20">
    {escape(receiver)}
  </p>
  {extra_html}
  <p style="color:#666">Ciii&hellip; nie mów nikomu! &#129323;</p>
</div>"""
    return build_message(
        settings,
        to_name=giver,
        to_email=giver_email,
        subject=f"Secret Santa: {group_name}",
        text=text,
        html=html,
    )


def smtp_test_message(settings: Settings, to_email: str) -> EmailMessage:
    return build_message(
        settings,
        to_name="",
        to_email=to_email,
        subject="Secret Santa: wiadomość testowa",
        text="Jeśli to czytasz, aplikacja Secret Santa umie wysyłać e-maile.\n",
        html="<p>Jeśli to czytasz, aplikacja Secret Santa umie wysyłać e-maile. &#127876;</p>",
    )
