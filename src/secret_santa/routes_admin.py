"""Organizer pages under /admin: create groups, watch sign-ups, run the draw, (re)send emails.

The organizer usually takes part too, so these pages never show who drew whom, except
`POST /groups/{id}/reveal`, which the organizer has to ask for explicitly.
"""

import hmac
import logging
import time
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from secret_santa.config import Settings
from secret_santa.draw import MIN_PARTICIPANTS, draw
from secret_santa.mailer import Mailer, assignment_message, deliver, smtp_test_message
from secret_santa.models import Group, Participant, utcnow
from secret_santa.web import (
    base_url,
    clean_email,
    clean_name,
    flash,
    get_db,
    get_mailer,
    get_settings,
    new_code,
    parse_names,
    plural,
    templates,
)
from secret_santa.wishes import ensure_wish_list, personal_url, send_links

log = logging.getLogger(__name__)


def require_admin(request: Request) -> None:
    if not request.session.get("admin"):
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})


DbDep = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
MailerDep = Annotated[Mailer, Depends(get_mailer)]

auth_router = APIRouter(prefix="/admin")
router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


def _back(group_id: int) -> RedirectResponse:
    return RedirectResponse(f"/admin/groups/{group_id}", status_code=303)


def _get_group(db: Session, group_id: int) -> Group:
    group = db.get(Group, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Nie ma takiej grupy.")
    return group


def _get_participant(db: Session, participant_id: int) -> Participant:
    person = db.get(Participant, participant_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Nie ma takiego uczestnika.")
    return person


# --- login ------------------------------------------------------------------------------------


@auth_router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    if request.session.get("admin"):
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(request, "admin/login.html", {})


@auth_router.post("/login")
def login(
    request: Request, settings: SettingsDep, password: Annotated[str, Form(max_length=1000)]
) -> Response:
    if hmac.compare_digest(password.encode(), settings.admin_password.encode()):
        request.session["admin"] = True
        return RedirectResponse("/admin", status_code=303)
    log.warning("failed admin login from %s", request.client.host if request.client else "?")
    time.sleep(1)  # slow down guessing; sync endpoint, so this only holds a worker thread
    return templates.TemplateResponse(
        request, "admin/login.html", {"error": "Nieprawidłowe hasło."}, status_code=401
    )


@auth_router.post("/logout")
def logout(request: Request) -> Response:
    request.session.pop("admin", None)
    return RedirectResponse("/", status_code=303)


# --- groups -----------------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def admin_index(request: Request, db: DbDep, settings: SettingsDep) -> Response:
    joined = func.count(Participant.email)
    total = func.count(Participant.id)
    rows = db.execute(
        select(Group, joined, total)
        .outerjoin(Participant, Participant.group_id == Group.id)
        .group_by(Group.id)
        .order_by(Group.created_at.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "admin/index.html",
        {"rows": rows, "sender": settings.sender_address, "smtp_host": settings.smtp_host},
    )


@router.post("/groups")
def create_group(
    request: Request,
    db: DbDep,
    name: Annotated[str, Form(max_length=200)],
    names: Annotated[str, Form(max_length=20_000)] = "",
    message: Annotated[str, Form(max_length=5_000)] = "",
) -> Response:
    group_name = clean_name(name)[:200]
    if not group_name:
        flash(request, "Podaj nazwę grupy.", "error")
        return RedirectResponse("/admin", status_code=303)
    group = Group(name=group_name, message=message.strip(), code=new_code(db))
    group.participants = [Participant(name=n) for n in parse_names(names)]
    db.add(group)
    db.commit()
    log.info("group created: id=%s participants=%s", group.id, len(group.participants))
    n = len(group.participants)
    flash(request, f"Utworzono grupę ({n} {plural(n, 'osoba', 'osoby', 'osób')}).", "success")
    return _back(group.id)


@router.get("/groups/{group_id}", response_class=HTMLResponse)
def group_page(group_id: int, request: Request, db: DbDep, settings: SettingsDep) -> Response:
    group = _get_group(db, group_id)
    people = group.participants
    joined = sum(p.is_registered for p in people)
    return templates.TemplateResponse(
        request,
        "admin/group.html",
        {
            "group": group,
            "people": people,
            "joined": joined,
            "total": len(people),
            "ready": joined == len(people) >= MIN_PARTICIPANTS,
            "min_participants": MIN_PARTICIPANTS,
            "sent": sum(p.email_sent_at is not None for p in people),
            "failed": sum(p.email_error is not None and p.email_sent_at is None for p in people),
            "share_url": f"{base_url(request, settings)}/g/{group.code}",
            "links_sent": sum(bool(p.wish_list and p.wish_list.link_sent_at) for p in people),
            "wishes_filled": sum(bool(p.wish_list and p.wish_list.is_filled) for p in people),
        },
    )


@router.post("/groups/{group_id}/edit")
def edit_group(
    group_id: int,
    request: Request,
    db: DbDep,
    name: Annotated[str, Form(max_length=200)],
    message: Annotated[str, Form(max_length=5_000)] = "",
) -> Response:
    group = _get_group(db, group_id)
    if clean_name(name):
        group.name = clean_name(name)[:200]
    group.message = message.strip()
    db.commit()
    flash(request, "Zapisano.", "success")
    return _back(group_id)


@router.post("/groups/{group_id}/delete")
def delete_group(group_id: int, request: Request, db: DbDep) -> Response:
    group = _get_group(db, group_id)
    db.delete(group)
    db.commit()
    flash(request, f"Usunięto „{group.name}”.", "success")
    return RedirectResponse("/admin", status_code=303)


# --- participants -----------------------------------------------------------------------------


@router.post("/groups/{group_id}/participants")
def add_participants(
    group_id: int,
    request: Request,
    db: DbDep,
    names: Annotated[str, Form(max_length=20_000)],
) -> Response:
    group = _get_group(db, group_id)
    if group.is_drawn:
        flash(request, "Losowanie już się odbyło, listy nie można już zmieniać.", "error")
        return _back(group_id)
    existing = {p.name.casefold() for p in group.participants}
    new = [n for n in parse_names(names) if n.casefold() not in existing]
    group.participants.extend(Participant(name=n) for n in new)
    db.commit()
    n = len(new)
    flash(
        request,
        f"Dodano {n} {plural(n, 'osobę', 'osoby', 'osób')}." if new else "Nie ma nikogo nowego.",
        "success",
    )
    return _back(group_id)


@router.post("/participants/{participant_id}/delete")
def delete_participant(participant_id: int, request: Request, db: DbDep) -> Response:
    person = _get_participant(db, participant_id)
    group_id = person.group_id
    if person.group.is_drawn:
        flash(request, "Losowanie już się odbyło, listy nie można już zmieniać.", "error")
        return _back(group_id)
    db.delete(person)
    db.commit()
    flash(request, f"Usunięto: {person.name}.", "success")
    return _back(group_id)


@router.post("/participants/{participant_id}/rename")
def rename_participant(
    participant_id: int,
    request: Request,
    db: DbDep,
    name: Annotated[str, Form(max_length=200)],
) -> Response:
    person = _get_participant(db, participant_id)
    if clean_name(name):
        person.name = clean_name(name)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            flash(request, "Ktoś w tej grupie ma już takie imię.", "error")
    return _back(person.group_id)


@router.post("/participants/{participant_id}/email")
def set_email(
    participant_id: int,
    request: Request,
    db: DbDep,
    email: Annotated[str, Form(max_length=320)] = "",
) -> Response:
    """Fix a typo, register someone by hand, or (before the draw) free a wrongly claimed name."""
    person = _get_participant(db, participant_id)
    group_id = person.group_id
    if not email.strip():
        if person.group.is_drawn:
            flash(request, "Po losowaniu adres można zmienić, ale nie usunąć.", "error")
            return _back(group_id)
        person.email = None
        person.registered_at = None
        db.commit()
        flash(
            request, f"Wypisano: {person.name}. Ta osoba może zapisać się jeszcze raz.", "success"
        )
        return _back(group_id)
    try:
        # No DNS check here: the organizer may know better than a resolver hiccup.
        normalized = clean_email(email, check_dns=False)
    except ValueError as exc:
        flash(request, f"{person.name}: {exc}", "error")
        return _back(group_id)
    if normalized != person.email:
        person.email = normalized
        person.registered_at = person.registered_at or utcnow()
        # A new address means the old send no longer counts.
        person.email_sent_at = None
        person.email_error = None
        if person.wish_list is not None:
            person.wish_list.link_sent_at = None
            person.wish_list.link_error = None
        db.commit()
        msg = f"Zmieniono e-mail: {person.name}."
        if person.group.is_drawn:
            msg += " Teraz wyślij jej wynik: Edytuj → „Wyślij”."
        flash(request, msg, "success")
    return _back(group_id)


# --- draw & email -----------------------------------------------------------------------------


def _send(
    db: Session, settings: Settings, mailer: Mailer, base: str, people: list[Participant]
) -> tuple[int, int]:
    """Email each person their assignment. Records the outcome per person; returns (ok, failed).

    The email also carries the person's wish list link, so after a fresh draw nobody needs a
    separate link email.
    """
    people = [p for p in people if p.receiver is not None and p.email]
    for person in people:
        ensure_wish_list(person)
    db.commit()  # the tokens must exist before any email points at them

    errors = deliver(
        mailer,
        {
            p.id: partial(
                assignment_message,
                settings,
                group_name=p.group.name,
                group_message=p.group.message,
                giver=p.name,
                giver_email=p.email,
                receiver=p.receiver.name,
                personal_url=personal_url(base, p.wish_list),
            )
            for p in people
        },
    )
    for person in people:
        if errors[person.id] is None:
            person.email_sent_at = person.wish_list.link_sent_at = utcnow()
            person.email_error = person.wish_list.link_error = None
        else:
            person.email_error = errors[person.id]
    db.commit()
    failed = sum(e is not None for e in errors.values())
    log.info("assignment emails: sent=%s failed=%s", len(errors) - failed, failed)
    return len(errors) - failed, failed


def _flash_send_result(request: Request, ok: int, failed: int) -> None:
    sent = f"Wysłano {ok} {plural(ok, 'e-mail', 'e-maile', 'e-maili')}"
    if failed:
        flash(request, f"{sent}, nieudane: {failed}. Szczegóły na liście poniżej.", "error")
    else:
        flash(request, f"{sent}. 🎁", "success")


@router.post("/groups/{group_id}/draw")
def run_draw(
    group_id: int, request: Request, db: DbDep, settings: SettingsDep, mailer: MailerDep
) -> Response:
    # Claim the draw first: this takes SQLite's write lock, so a double click or a sign-up
    # racing with us cannot slip in between the checks below and the assignments.
    claimed = db.execute(
        update(Group)
        .where(Group.id == group_id, Group.drawn_at.is_(None))
        .values(drawn_at=utcnow())
    ).rowcount
    if claimed != 1:
        db.rollback()
        _get_group(db, group_id)  # 404 if it does not exist
        flash(request, "Losowanie już się odbyło.", "error")
        return _back(group_id)

    people = list(
        db.scalars(
            select(Participant)
            .where(Participant.group_id == group_id)
            .execution_options(populate_existing=True)
        )
    )
    waiting = [p.name for p in people if not p.is_registered]
    if waiting or len(people) < MIN_PARTICIPANTS:
        db.rollback()
        flash(
            request,
            f"Wciąż czekamy na: {', '.join(waiting)}."
            if waiting
            else f"Potrzeba co najmniej {MIN_PARTICIPANTS} osób.",
            "error",
        )
        return _back(group_id)

    by_id = {p.id: p for p in people}
    for giver_id, receiver_id in draw(list(by_id)).items():
        by_id[giver_id].receiver = by_id[receiver_id]
        by_id[giver_id].email_sent_at = None
        by_id[giver_id].email_error = None
    db.commit()
    log.info("draw done: group=%s participants=%s", group_id, len(people))

    ok, failed = _send(db, settings, mailer, base_url(request, settings), people)
    _flash_send_result(request, ok, failed)
    return _back(group_id)


@router.post("/groups/{group_id}/send")
def send_unsent(
    group_id: int, request: Request, db: DbDep, settings: SettingsDep, mailer: MailerDep
) -> Response:
    """Retry everyone whose email has not gone out yet (after an SMTP failure or address fix)."""
    group = _get_group(db, group_id)
    pending = [p for p in group.participants if p.email_sent_at is None]
    if not group.is_drawn or not pending:
        flash(request, "Nie ma nic do wysłania.", "info")
        return _back(group_id)
    _flash_send_result(request, *_send(db, settings, mailer, base_url(request, settings), pending))
    return _back(group_id)


@router.post("/participants/{participant_id}/send")
def send_one(
    participant_id: int,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    mailer: MailerDep,
) -> Response:
    """(Re)send one person's assignment, e.g. when it landed in spam or they deleted it."""
    person = _get_participant(db, participant_id)
    if not person.group.is_drawn:
        flash(request, "Najpierw przeprowadź losowanie.", "error")
        return _back(person.group_id)
    ok, _ = _send(db, settings, mailer, base_url(request, settings), [person])
    if ok:
        flash(request, f"Wysłano wynik losowania: {person.name} ({person.email}).", "success")
    else:
        flash(request, f"Błąd wysyłki ({person.name}): {person.email_error}", "error")
    return _back(person.group_id)


# --- wish lists -------------------------------------------------------------------------------


@router.post("/groups/{group_id}/wish-links")
def send_wish_links(
    group_id: int, request: Request, db: DbDep, settings: SettingsDep, mailer: MailerDep
) -> Response:
    """Email everyone who hasn't got it yet their personal wish list link."""
    group = _get_group(db, group_id)
    if not group.is_drawn:
        flash(request, "Najpierw przeprowadź losowanie.", "error")
        return _back(group_id)
    pending = [
        p for p in group.participants if p.email and not (p.wish_list and p.wish_list.link_sent_at)
    ]
    if not pending:
        flash(request, "Wszyscy mają już link.", "info")
        return _back(group_id)
    _flash_send_result(
        request, *send_links(db, settings, mailer, base_url(request, settings), pending)
    )
    return _back(group_id)


@router.post("/participants/{participant_id}/wish-link")
def send_wish_link(
    participant_id: int,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    mailer: MailerDep,
) -> Response:
    person = _get_participant(db, participant_id)
    if not person.group.is_drawn:
        flash(request, "Najpierw przeprowadź losowanie.", "error")
        return _back(person.group_id)
    ok, _ = send_links(db, settings, mailer, base_url(request, settings), [person])
    if ok:
        flash(request, f"Wysłano link do listy życzeń: {person.name} ({person.email}).", "success")
    else:
        flash(request, f"Błąd wysyłki ({person.name}): {person.wish_list.link_error}", "error")
    return _back(person.group_id)


@router.post("/groups/{group_id}/reveal", response_class=HTMLResponse)
def reveal(group_id: int, request: Request, db: DbDep) -> Response:
    """Show who drew whom, for an organizer who explicitly asks.

    POST only, rendered directly (no redirect, no-store): there is no URL that shows the pairs, so
    browser history, autocomplete, a bookmark or a back button can't open them by accident.
    """
    group = _get_group(db, group_id)
    if not group.is_drawn:
        flash(request, "Najpierw przeprowadź losowanie.", "error")
        return _back(group_id)
    pairs = [(p.name, p.receiver.name) for p in group.participants if p.receiver is not None]
    log.info("draw results shown to the organizer: group=%s", group_id)
    response = templates.TemplateResponse(
        request, "admin/reveal.html", {"group": group, "pairs": pairs}
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/test-email")
def send_test_email(
    request: Request,
    settings: SettingsDep,
    mailer: MailerDep,
    email: Annotated[str, Form(max_length=320)],
) -> Response:
    try:
        to = clean_email(email, check_dns=False)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/admin", status_code=303)
    [error] = mailer.send_many([smtp_test_message(settings, to)])
    if error:
        flash(request, f"Nie udało się wysłać testowego e-maila: {error}", "error")
    else:
        flash(request, f"Wysłano testowy e-mail na {to}. Sprawdź skrzynkę (i spam).", "success")
    return RedirectResponse("/admin", status_code=303)
