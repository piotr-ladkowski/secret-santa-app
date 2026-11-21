"""Wish lists: personal links, saving wishes, and emailing them to the person who buys the gift.

Everyone gets a secret link (/p/<token>) by email. On that page they write what they would like
and see the wishes of the person *they* buy for. Picking a name on the group page is not enough
to get there: anyone with the group code could then read someone else's page and work out who drew
whom. For the same reason the organizer's pages never show the links.
"""

import logging
import secrets
from datetime import timedelta
from functools import partial

from sqlalchemy import select
from sqlalchemy.orm import Session

from secret_santa.config import Settings
from secret_santa.mailer import Mailer, deliver, wish_link_message
from secret_santa.models import Participant, WishList, utcnow

log = logging.getLogger(__name__)

MAX_WISH_LENGTH = 2000
# A participant can ask for their link again on the group page; don't let that spam them.
SELF_SERVICE_COOLDOWN = timedelta(minutes=2)


def ensure_wish_list(person: Participant) -> WishList:
    if person.wish_list is None:
        person.wish_list = WishList(token=secrets.token_urlsafe(18), text="")
    return person.wish_list


def personal_url(base: str, wish_list: WishList) -> str:
    return f"{base}/p/{wish_list.token}"


def giver_of(db: Session, person: Participant) -> Participant | None:
    """The person who drew `person` (their Secret Santa)."""
    return db.scalar(select(Participant).where(Participant.receiver_id == person.id))


def clean_wishes(raw: str) -> str:
    """Unix newlines, no trailing spaces, no leading/trailing blank lines."""
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


def send_links(
    db: Session, settings: Settings, mailer: Mailer, base: str, people: list[Participant]
) -> tuple[int, int]:
    """Email each person their personal link. Records the outcome; returns (ok, failed)."""
    people = [p for p in people if p.email]
    for person in people:
        ensure_wish_list(person)
    db.commit()  # the tokens must exist before any email points at them

    errors = deliver(
        mailer,
        {
            p.id: partial(
                wish_link_message,
                settings,
                group_name=p.group.name,
                name=p.name,
                to_email=p.email,
                url=personal_url(base, p.wish_list),
            )
            for p in people
        },
    )
    for person in people:
        wish_list = person.wish_list
        if errors[person.id] is None:
            wish_list.link_sent_at = utcnow()
            wish_list.link_error = None
        else:
            wish_list.link_error = errors[person.id]
    db.commit()
    failed = sum(e is not None for e in errors.values())
    log.info("wish list links: sent=%s failed=%s", len(errors) - failed, failed)
    return len(errors) - failed, failed


def recently_sent(wish_list: WishList | None) -> bool:
    if wish_list is None or wish_list.link_sent_at is None:
        return False
    sent = wish_list.link_sent_at
    if sent.tzinfo is None:  # SQLite hands datetimes back without a timezone; they are UTC
        sent = sent.replace(tzinfo=utcnow().tzinfo)
    return utcnow() - sent < SELF_SERVICE_COOLDOWN
