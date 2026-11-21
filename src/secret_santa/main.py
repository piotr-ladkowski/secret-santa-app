"""App factory. Run with:  uvicorn secret_santa.main:create_app --factory"""

import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from secret_santa import routes_admin, routes_public, routes_wishes
from secret_santa.config import Settings, get_settings
from secret_santa.db import Database
from secret_santa.mailer import Mailer, SmtpMailer
from secret_santa.web import BASE_DIR


def create_app(settings: Settings | None = None, mailer: Mailer | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    app = FastAPI(title="Secret Santa", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.db = Database(settings.database_url)
    app.state.mailer = mailer or SmtpMailer(settings)

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="santa_session",
        max_age=60 * 60 * 24 * 60,  # 60 days: long enough to come back to the join page
        same_site="lax",  # cross-site POSTs carry no cookie, which covers CSRF for the forms
        https_only=settings.cookie_secure,
    )
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    app.include_router(routes_public.router)
    app.include_router(routes_wishes.router)
    app.include_router(routes_admin.auth_router)
    app.include_router(routes_admin.router)
    return app
