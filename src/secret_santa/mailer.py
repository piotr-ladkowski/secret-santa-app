"""Sending email over SMTP (Yahoo Mail by default)."""

import contextlib
import logging
import smtplib
import ssl
from collections.abc import Callable, Mapping, Sequence
from email.errors import MessageError
from email.headerregistry import Address
from email.message import EmailMessage
from html import escape
from typing import Protocol

from secret_santa.config import Settings
from secret_santa.textfmt import linkify

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


def deliver[K](
    mailer: Mailer, builders: Mapping[K, Callable[[], EmailMessage]]
) -> dict[K, str | None]:
    """Build and send one message per key. Returns {key: None if sent, else an error text}.

    A message that can't even be built (an address the email library refuses) fails on its own
    instead of blocking everyone else's.
    """
    errors: dict[K, str | None] = {}
    ready: list[tuple[K, EmailMessage]] = []
    for key, build in builders.items():
        try:
            ready.append((key, build()))
        except (ValueError, MessageError) as exc:
            errors[key] = f"Błędny adres: {exc}"
    if ready:
        results = mailer.send_many([msg for _, msg in ready])
        errors.update((key, err) for (key, _), err in zip(ready, results, strict=True))
    return errors


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


def _preheader(text: str) -> str:
    """Hidden "preheader": mail clients show it as the preview line instead of the body's start.

    The padding (invisible characters) fills the rest of the preview, so names never show up in an
    inbox list, a phone notification, or the sender's "Sent" folder.
    """
    return (
        '<div style="display:none;max-height:0;overflow:hidden;opacity:0;mso-hide:all">'
        f"{escape(text)}" + "&#847;&zwnj;&nbsp;" * 90 + "</div>"
    )


def _card(title: str, inner_html: str) -> str:
    return f"""\
<div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:480px;
            margin:auto;padding:24px;border-radius:12px;background:#fff8f0;color:#222">
  <h2 style="margin-top:0;color:#b3261e">&#127877; {escape(title)}</h2>
{inner_html}
</div>"""


def _button(url: str, label: str) -> str:
    return (
        f'<p style="text-align:center;margin:24px 0"><a href="{escape(url)}" '
        'style="display:inline-block;padding:12px 20px;border-radius:8px;background:#b3261e;'
        f'color:#fff;text-decoration:none;font-weight:600">{escape(label)}</a></p>'
        f'<p style="font-size:12px;color:#666;word-break:break-all">{escape(url)}</p>'
    )


def assignment_message(
    settings: Settings,
    *,
    group_name: str,
    group_message: str,
    giver: str,
    giver_email: str,
    receiver: str,
    personal_url: str | None = None,
) -> EmailMessage:
    # Both parts open with a spoiler-free paragraph long enough to fill any preview, in case a
    # client builds it from the plain text or ignores the hidden preheader.
    intro = (
        f"Losowanie w grupie „{group_name}” zakończone. Imię osoby, której kupujesz prezent, "
        "jest niżej, żeby nie mignęło nikomu w podglądzie wiadomości ani w powiadomieniu "
        "na telefonie."
    )
    extra_text = f"\n{group_message.strip()}\n" if group_message.strip() else ""
    link_text = (
        f"\n{WISH_LINK_PITCH}\n{personal_url}\n(link jest tylko dla Ciebie)\n"
        if personal_url
        else ""
    )
    text = (
        f"Cześć {giver}!\n\n"
        f"{intro}\n\n\n"
        "Kupujesz prezent dla tej osoby:\n\n"
        f"    {receiver}\n"
        f"{extra_text}"
        f"{link_text}\n"
        "Ciii… nie mów nikomu! 🤫\n"
    )
    extra_html = (
        f'<p style="white-space:pre-line;color:#444">{escape(group_message.strip())}</p>'
        if group_message.strip()
        else ""
    )
    link_html = (
        f"<p>{escape(WISH_LINK_PITCH)}</p>{_button(personal_url, 'Moja lista życzeń')}"
        if personal_url
        else ""
    )
    html = _preheader("Wynik losowania jest w środku. Otwórz, kiedy nikt nie patrzy 🤫") + _card(
        f"Secret Santa: {group_name}",
        f"""\
  <p>Cześć {escape(giver)}!</p>
  <p>{escape(intro)}</p>
  <p>Kupujesz prezent dla tej osoby:</p>
  <p style="font-size:28px;font-weight:700;text-align:center;margin:24px 0;color:#1b5e20">
    {escape(receiver)}
  </p>
  {extra_html}
  {link_html}
  <p style="color:#666">Ciii&hellip; nie mów nikomu! &#129323;</p>""",
    )
    return build_message(
        settings,
        to_name=giver,
        to_email=giver_email,
        subject=f"Secret Santa: {group_name}",
        text=text,
        html=html,
    )


WISH_LINK_PITCH = "Napisz, co chcesz dostać, i zobacz listę życzeń osoby, której kupujesz prezent:"


def wish_link_message(
    settings: Settings, *, group_name: str, name: str, to_email: str, url: str
) -> EmailMessage:
    """The personal link to someone's wish list page. Names nobody else, so it's spoiler-free."""
    about = (
        "Wpisz tam, co chcesz dostać (albo czego lepiej nie kupować). Osoba, która Cię "
        "wylosowała, dostanie to e-mailem, a Ty nadal nie dowiesz się, kto to. Na tej samej "
        "stronie zobaczysz też listę życzeń osoby, której kupujesz prezent."
    )
    text = (
        f"Cześć {name}!\n\n"
        f"Oto Twój osobisty link do listy życzeń w grupie „{group_name}”:\n\n"
        f"{url}\n\n"
        f"{about}\n\n"
        "Link jest tylko dla Ciebie, nie przekazuj go dalej.\n"
    )
    html = _preheader("Twój osobisty link do listy życzeń 🎁") + _card(
        f"Secret Santa: {group_name}",
        f"""\
  <p>Cześć {escape(name)}!</p>
  <p>Oto Twój osobisty link do listy życzeń w grupie „{escape(group_name)}”.</p>
  {_button(url, "Moja lista życzeń")}
  <p>{escape(about)}</p>
  <p style="color:#666">Link jest tylko dla Ciebie, nie przekazuj go dalej.</p>""",
    )
    return build_message(
        settings,
        to_name=name,
        to_email=to_email,
        subject=f"Secret Santa: Twoja lista życzeń ({group_name})",
        text=text,
        html=html,
    )


def wish_update_message(
    settings: Settings,
    *,
    group_name: str,
    giver: str,
    giver_email: str,
    receiver: str,
    wishes: str,
    url: str,
) -> EmailMessage:
    """Tell the giver what their person wrote. As with the assignment, no name in the preview."""
    intro = (
        "Osoba, której kupujesz prezent, uzupełniła swoją listę życzeń. Jej imię i listę "
        "znajdziesz niżej, żeby nie mignęły nikomu w podglądzie wiadomości ani w powiadomieniu "
        "na telefonie."
    )
    page = "Aktualna wersja jest zawsze na Twojej stronie (tam możesz też napisać własną listę):"
    text = f"Cześć {giver}!\n\n{intro}\n\n\nLista życzeń: {receiver}\n\n{wishes}\n\n{page}\n{url}\n"
    html = _preheader("Nowa lista życzeń od osoby, której kupujesz prezent 🎁") + _card(
        f"Secret Santa: {group_name}",
        f"""\
  <p>Cześć {escape(giver)}!</p>
  <p>{escape(intro)}</p>
  <p style="font-size:20px;font-weight:700;margin:24px 0 8px;color:#1b5e20">
    Lista życzeń: {escape(receiver)}
  </p>
  <div style="white-space:pre-line;padding:12px 16px;border-radius:8px;background:#fff;
              border:1px solid #e6ddd2">{linkify(wishes)}</div>
  <p>{escape(page)}</p>
  {_button(url, "Moja strona")}""",
    )
    return build_message(
        settings,
        to_name=giver,
        to_email=giver_email,
        subject=f"Secret Santa: nowa lista życzeń ({group_name})",
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
