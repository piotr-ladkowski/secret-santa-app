"""Pages for participants: enter a code, pick your name, leave your email."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from secret_santa.config import Settings
from secret_santa.models import Group, Participant, utcnow
from secret_santa.web import (
    clean_email,
    get_db,
    get_settings,
    normalize_code,
    templates,
)

log = logging.getLogger(__name__)
router = APIRouter()

DbDep = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
def readyz(db: DbDep) -> dict[str, str]:
    db.execute(text("SELECT 1"))
    return {"status": "ready"}


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> Response:
    return templates.TemplateResponse(request, "index.html", {})


@router.get("/join")
def join_by_code(request: Request, db: DbDep, code: str = "") -> Response:
    normalized = normalize_code(code)
    if normalized and db.scalar(select(Group.id).where(Group.code == normalized)):
        return RedirectResponse(f"/g/{normalized}", status_code=303)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"error": "Nie ma grupy z takim kodem. Sprawdź go i spróbuj jeszcze raz.", "code": code},
        status_code=404,
    )


def _load_group(db: Session, code: str) -> Group | None:
    return db.scalar(select(Group).where(Group.code == normalize_code(code)))


def _render_join(
    request: Request,
    group: Group,
    *,
    error: str | None = None,
    form: dict | None = None,
    status_code: int = 200,
) -> Response:
    participants = group.participants
    my_id = request.session.get("me", {}).get(group.code)
    me = next((p for p in participants if p.id == my_id and p.is_registered), None)
    return templates.TemplateResponse(
        request,
        "join.html",
        {
            "group": group,
            "participants": participants,
            "available": [p for p in participants if not p.is_registered],
            "joined": sum(p.is_registered for p in participants),
            "total": len(participants),
            "me": me,
            "error": error,
            "form": form or {},
        },
        status_code=status_code,
    )


@router.get("/g/{code}", response_class=HTMLResponse)
def join_page(code: str, request: Request, db: DbDep) -> Response:
    group = _load_group(db, code)
    if group is None:
        return join_by_code(request, db, code)
    if code != group.code:
        return RedirectResponse(f"/g/{group.code}", status_code=303)
    return _render_join(request, group)


@router.post("/g/{code}", response_class=HTMLResponse)
def register(
    code: str,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    participant_id: Annotated[int, Form()],
    email: Annotated[str, Form(max_length=320)],
) -> Response:
    group = _load_group(db, code)
    if group is None:
        return join_by_code(request, db, code)
    form = {"participant_id": participant_id, "email": email}

    if group.is_drawn:
        return _render_join(
            request, group, error="Losowanie już się odbyło.", form=form, status_code=409
        )
    try:
        normalized = clean_email(email, check_dns=settings.check_email_dns)
    except ValueError as exc:
        return _render_join(request, group, error=str(exc), form=form, status_code=400)

    # Conditional update: of two people grabbing the same name at once, exactly one wins.
    result = db.execute(
        update(Participant)
        .where(
            Participant.id == participant_id,
            Participant.group_id == group.id,
            Participant.email.is_(None),
        )
        .values(email=normalized, registered_at=utcnow())
    )
    if result.rowcount != 1:
        db.rollback()
        db.refresh(group)
        return _render_join(
            request,
            group,
            error="To imię jest już zajęte. Wybierz swoje z listy albo zapytaj organizatora.",
            form=form,
            status_code=409,
        )
    db.commit()
    db.refresh(group)

    log.info("participant registered: group=%s participant_id=%s", group.id, participant_id)
    me = dict(request.session.get("me", {}))
    me[group.code] = participant_id
    request.session["me"] = me
    return RedirectResponse(f"/g/{group.code}", status_code=303)
