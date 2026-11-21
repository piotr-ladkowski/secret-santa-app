"""The personal page behind each participant's secret link: their wish list, and their person's."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from secret_santa.config import Settings
from secret_santa.mailer import Mailer, deliver, wish_update_message
from secret_santa.models import WishList, utcnow
from secret_santa.web import base_url, flash, get_db, get_mailer, get_settings, templates
from secret_santa.wishes import (
    MAX_WISH_LENGTH,
    clean_wishes,
    ensure_wish_list,
    giver_of,
    personal_url,
)

log = logging.getLogger(__name__)
router = APIRouter()

DbDep = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
MailerDep = Annotated[Mailer, Depends(get_mailer)]


def _load(db: Session, token: str) -> WishList | None:
    if not 16 <= len(token) <= 64:
        return None
    return db.scalar(select(WishList).where(WishList.token == token))


def _not_found(request: Request) -> Response:
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "error": "Ten link nie działa. Sprawdź, czy skopiował się w całości. Nowy możesz "
            "dostać na stronie grupy: wpisz poniżej jej kod."
        },
        status_code=404,
    )


def _private(response: Response) -> Response:
    # The URL is the key to this page: keep it out of caches and out of Referer headers sent to
    # shops linked from a wish list.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _render(
    request: Request, wish_list: WishList, *, text: str | None = None, error: str | None = None
) -> Response:
    person = wish_list.participant
    receiver = person.receiver
    return _private(
        templates.TemplateResponse(
            request,
            "wishes.html",
            {
                "person": person,
                "group": person.group,
                "mine": wish_list,
                "text": wish_list.text if text is None else text,
                "receiver": receiver,
                "theirs": receiver.wish_list if receiver else None,
                "max_length": MAX_WISH_LENGTH,
                "error": error,
            },
            status_code=400 if error else 200,
        )
    )


@router.get("/p/{token}", response_class=HTMLResponse)
def personal_page(token: str, request: Request, db: DbDep) -> Response:
    wish_list = _load(db, token)
    if wish_list is None:
        return _not_found(request)
    return _render(request, wish_list)


@router.post("/p/{token}", response_class=HTMLResponse)
def save_wishes(
    token: str,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    mailer: MailerDep,
    wishes: Annotated[str, Form(max_length=20_000)] = "",
    notify: Annotated[bool, Form()] = False,
) -> Response:
    wish_list = _load(db, token)
    if wish_list is None:
        return _not_found(request)
    text = clean_wishes(wishes)
    if len(text) > MAX_WISH_LENGTH:
        return _render(
            request,
            wish_list,
            text=text,
            error=f"To za dużo tekstu: {len(text)} znaków, a mieści się {MAX_WISH_LENGTH}.",
        )
    back = RedirectResponse(f"/p/{token}", status_code=303)
    if text == wish_list.text:
        flash(request, "Bez zmian.", "info")
        return back

    wish_list.text = text
    wish_list.updated_at = utcnow()
    db.commit()
    person = wish_list.participant
    log.info("wish list saved: participant=%s", person.id)

    giver = giver_of(db, person) if notify and text else None
    if giver is None or not giver.email:
        flash(request, "Zapisano.", "success")
        return back
    giver_page = ensure_wish_list(giver)
    db.commit()
    error = deliver(
        mailer,
        {
            giver.id: lambda: wish_update_message(
                settings,
                group_name=person.group.name,
                giver=giver.name,
                giver_email=giver.email,
                receiver=person.name,
                wishes=text,
                url=personal_url(base_url(request, settings), giver_page),
            )
        },
    )[giver.id]
    if error is None:
        flash(
            request,
            "Zapisano. Osoba, która Cię wylosowała, dostała Twoją listę e-mailem. 🎁",
            "success",
        )
    else:
        log.warning("wish list notification failed: participant=%s: %s", person.id, error)
        flash(
            request,
            "Zapisano, ale e-mail do osoby, która Cię wylosowała, nie wyszedł. "
            "Twoją listę i tak zobaczy na swojej stronie.",
            "error",
        )
    return back
