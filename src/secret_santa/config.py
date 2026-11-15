"""Settings, all read from environment variables (or a local `.env` file).

Required in production: ADMIN_PASSWORD, SECRET_KEY, SMTP_USERNAME, SMTP_PASSWORD, PUBLIC_BASE_URL.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- app ---
    # Password for /admin (the organizer). There is a single admin account.
    admin_password: str = Field(min_length=8)
    # Signs the session cookie. Generate with: openssl rand -hex 32
    secret_key: str = Field(min_length=16)
    # Absolute URL people use to reach the app, e.g. https://secret-santa.pladkowski.pl.
    # Used for the share links shown in /admin. Empty = derive from the incoming request.
    # An https:// value also marks the session cookie as Secure.
    public_base_url: str = ""
    database_url: str = "sqlite:///data/secret-santa.db"
    log_level: str = "INFO"
    # Look up the email domain in DNS when people register, to catch typos like "gmail.con".
    check_email_dns: bool = True

    # --- email (defaults are Yahoo Mail's SMTP server) ---
    smtp_host: str = "smtp.mail.yahoo.com"
    smtp_port: int = 465
    # ssl = implicit TLS (port 465), starttls = upgrade (port 587), none = local testing only
    smtp_security: Literal["ssl", "starttls", "none"] = "ssl"
    smtp_username: str = ""
    # For Yahoo this must be an *app password*, not the account password.
    smtp_password: str = ""
    # Envelope/From address. Yahoo rejects mail whose From differs from the login, so the
    # default is the username.
    smtp_from: str = ""
    smtp_from_name: str = "Secret Santa"
    smtp_timeout: float = 30.0

    @property
    def sender_address(self) -> str:
        return self.smtp_from or self.smtp_username

    @property
    def cookie_secure(self) -> bool:
        return self.public_base_url.startswith("https://")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
