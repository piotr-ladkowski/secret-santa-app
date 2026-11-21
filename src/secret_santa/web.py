"""Helpers shared by the route modules: dependencies, templates, flash messages, input cleanup."""

import re
import secrets
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from email_validator import EmailNotValidError, EmailUndeliverableError, validate_email
from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from secret_santa.config import Settings
from secret_santa.mailer import Mailer
from secret_santa.models import Group
from secret_santa.textfmt import linkify

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# No 0/O, 1/I/L: codes get read out loud and typed from phones.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8  # 31^8 ~ 8.5e11 possibilities


# --- dependencies ---------------------------------------------------------------------------


def get_db(request: Request) -> Iterator[Session]:
    yield from request.app.state.db.session()


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_mailer(request: Request) -> Mailer:
    return request.app.state.mailer


# --- flash messages (stored in the signed session cookie) -----------------------------------


def flash(request: Request, message: str, category: str = "info") -> None:
    request.session.setdefault("_flash", []).append([category, message])


def _pop_flashes(request: Request) -> list[list[str]]:
    return request.session.pop("_flash", [])


def _fmt_dt(value: datetime | None) -> str:
    return value.strftime("%d.%m.%Y %H:%M UTC") if value else ""


def plural(n: int, one: str, few: str, many: str) -> str:
    """Polish plural: 1 osoba, 2-4 osoby (but 12-14 osób), 0 and 5+ osób."""
    if n == 1:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


templates.env.globals["get_flashed_messages"] = _pop_flashes
templates.env.globals["plural"] = plural
templates.env.filters["dt"] = _fmt_dt
templates.env.filters["linkify"] = linkify


# --- input helpers --------------------------------------------------------------------------


def new_code(db: Session) -> str:
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        if db.scalar(select(Group.id).where(Group.code == code)) is None:
            return code


def normalize_code(raw: str) -> str:
    """Accept codes typed with spaces, dashes or lower case."""
    return re.sub(r"[^A-Z0-9]", "", raw.upper())[:32]


def clean_name(raw: str) -> str:
    """One line, single spaces, at most 100 characters."""
    return " ".join(raw.split())[:100]


def parse_names(raw: str) -> list[str]:
    """One name per line (commas also work). Drops blanks and case-insensitive duplicates."""
    seen: set[str] = set()
    names: list[str] = []
    for part in re.split(r"[\n,;]", raw):
        name = clean_name(part)
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            names.append(name)
    return names


def clean_email(raw: str, *, check_dns: bool) -> str:
    """Return a normalized address or raise ValueError with a (Polish) message fit for the user.

    Non-ASCII before the @ (SMTPUTF8) is refused: Yahoo will not relay it and Python's email
    headers cannot carry it. Non-ASCII domains are fine and stored in their ASCII (punycode) form.
    """
    raw = raw.strip()
    local, at, domain = raw.rpartition("@")
    if not at:
        raise ValueError("W adresie e-mail brakuje znaku @.")
    if not local.isascii():
        raise ValueError(
            "Przed znakiem @ nie może być polskich liter (ą, ę, ż…). Sprawdź, czy adres jest dobry."
        )
    try:
        result = validate_email(raw, check_deliverability=check_dns, allow_smtputf8=False)
    except EmailUndeliverableError as exc:
        raise ValueError(
            f"Domena {domain} nie przyjmuje poczty. Sprawdź, czy nie ma w niej literówki."
        ) from exc
    except EmailNotValidError as exc:
        raise ValueError(
            "To nie wygląda na poprawny adres e-mail. Sprawdź, czy nie ma literówki."
        ) from exc
    return result.ascii_email or result.normalized


def base_url(request: Request, settings: Settings) -> str:
    return (settings.public_base_url or str(request.base_url)).rstrip("/")
