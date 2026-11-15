import re
from collections.abc import Sequence
from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from secret_santa.config import Settings
from secret_santa.main import create_app

ADMIN_PASSWORD = "correct horse battery"


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        self.fail_for: set[str] = set()

    def send_many(self, messages: Sequence[EmailMessage]) -> list[str | None]:
        results: list[str | None] = []
        for msg in messages:
            if msg["To"].addresses[0].addr_spec in self.fail_for:
                results.append("SMTPRecipientsRefused: nope")
            else:
                self.sent.append(msg)
                results.append(None)
        return results


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        admin_password=ADMIN_PASSWORD,
        secret_key="test-secret-key-0123456789",
        database_url=f"sqlite:///{tmp_path}/test.db",
        public_base_url="",
        check_email_dns=False,
        smtp_username="santa@yahoo.com",
        smtp_password="app-password",
    )


@pytest.fixture
def mailer() -> FakeMailer:
    return FakeMailer()


@pytest.fixture
def client(settings: Settings, mailer: FakeMailer) -> TestClient:
    return TestClient(create_app(settings, mailer))


@pytest.fixture
def admin(client: TestClient) -> TestClient:
    r = client.post("/admin/login", data={"password": ADMIN_PASSWORD}, follow_redirects=False)
    assert r.status_code == 303
    return client


def create_group(admin: TestClient, names: str = "Anna\nBartek\nKasia\nTomek") -> tuple[int, str]:
    r = admin.post(
        "/admin/groups",
        data={"name": "Family 2026", "names": names, "message": "Budget: 100 zł"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    group_id = int(r.headers["location"].rsplit("/", 1)[1])
    page = admin.get(f"/admin/groups/{group_id}").text
    code = re.search(r'<code class="big-code">(\w+)</code>', page).group(1)
    return group_id, code


def participant_ids(client: TestClient, code: str) -> dict[str, int]:
    page = client.get(f"/g/{code}").text
    return {
        name.strip(): int(pid)
        for pid, name in re.findall(
            r'name="participant_id" value="(\d+)"[^>]*>\s*<span>([^<]+)</span>', page
        )
    }
