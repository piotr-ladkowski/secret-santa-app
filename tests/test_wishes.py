import re
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import text, update

from secret_santa.config import Settings
from secret_santa.main import create_app
from secret_santa.models import WishList, utcnow
from secret_santa.textfmt import linkify
from tests.conftest import (
    ADMIN_PASSWORD,
    FakeMailer,
    assignments,
    create_group,
    participant_ids,
    register_all,
)

PEOPLE = ["Anna", "Bartek", "Kasia", "Tomek"]


def email_of(name: str) -> str:
    return f"{name.lower()}@example.com"


def links(mailer: FakeMailer) -> dict[str, str]:
    """{recipient email: personal page path} from the most recent email to each address."""
    out = {}
    for msg in mailer.sent:
        body = msg.get_body(("plain",)).get_content()
        if match := re.search(r"/p/[\w-]+", body):
            out[msg["To"].addresses[0].addr_spec] = match.group(0)
    return out


def drawn_group(admin: TestClient) -> tuple[int, str]:
    group_id, code = create_group(admin)
    register_all(admin, code)
    r = admin.post(f"/admin/groups/{group_id}/draw")
    assert "Wysłano 4 e-maile" in r.text
    return group_id, code


def giver_of(mailer: FakeMailer, name: str) -> str:
    """Email of the person who drew `name`, from the assignment emails."""
    return next(email for email, receiver in assignments(mailer).items() if receiver == name)


def test_assignment_email_carries_a_personal_link(admin: TestClient, mailer: FakeMailer) -> None:
    group_id, _ = drawn_group(admin)
    paths = links(mailer)
    assert set(paths) == {email_of(n) for n in PEOPLE}
    assert len(set(paths.values())) == 4  # one secret link per person
    page = admin.get(f"/admin/groups/{group_id}").text
    assert "Linki wysłane: <strong>4/4</strong>" in page
    assert "/p/" not in page  # the organizer never sees the links


def test_personal_page(admin: TestClient, mailer: FakeMailer) -> None:
    drawn_group(admin)
    r = admin.get(links(mailer)[email_of("Anna")])
    assert r.status_code == 200
    assert "Cześć, Anna!" in r.text
    assert f"Kupujesz prezent dla: {assignments(mailer)[email_of('Anna')]}" in r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_bad_links(client: TestClient) -> None:
    assert client.get("/p/short").status_code == 404
    r = client.get("/p/" + "x" * 24)
    assert r.status_code == 404 and "Ten link nie działa" in r.text


def test_saving_wishes_emails_the_giver(admin: TestClient, mailer: FakeMailer) -> None:
    drawn_group(admin)
    anna = links(mailer)[email_of("Anna")]
    santa = giver_of(mailer, "Anna")
    before = len(mailer.sent)

    wishes = "Ciepłe skarpety\r\nhttps://example.com/x?a=1&b=2."
    r = admin.post(anna, data={"wishes": wishes, "notify": "1"})
    assert "dostała Twoją listę e-mailem" in r.text
    assert len(mailer.sent) == before + 1
    note = mailer.sent[-1]
    assert note["To"].addresses[0].addr_spec == santa
    body = note.get_body(("plain",)).get_content()
    assert (
        "Lista życzeń: Anna" in body and "Ciepłe skarpety\nhttps://example.com/x?a=1&b=2." in body
    )
    # The email links to the giver's own page, which shows Anna's wishes with a clickable link.
    santa_page = re.search(r"/p/[\w-]+", body).group(0)
    assert santa_page == links(mailer)[santa]
    page = admin.get(santa_page).text
    assert "Kupujesz prezent dla: Anna" in page
    assert '<a href="https://example.com/x?a=1&amp;b=2" rel="noopener noreferrer nofollow"' in page


def test_no_email_without_notify_or_changes(admin: TestClient, mailer: FakeMailer) -> None:
    drawn_group(admin)
    anna = links(mailer)[email_of("Anna")]
    before = len(mailer.sent)
    assert "Zapisano." in admin.post(anna, data={"wishes": "Książka"}).text
    assert "Bez zmian." in admin.post(anna, data={"wishes": " Książka \n", "notify": "1"}).text
    assert len(mailer.sent) == before
    assert "Książka</textarea>" in admin.get(anna).text


def test_wishes_are_escaped(admin: TestClient, mailer: FakeMailer) -> None:
    drawn_group(admin)
    admin.post(links(mailer)[email_of("Anna")], data={"wishes": "<script>alert(1)</script>"})
    page = admin.get(links(mailer)[giver_of(mailer, "Anna")]).text
    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)" in page


def test_too_long_is_refused_but_kept_in_the_form(admin: TestClient, mailer: FakeMailer) -> None:
    drawn_group(admin)
    anna = links(mailer)[email_of("Anna")]
    r = admin.post(anna, data={"wishes": "a" * 2001})
    assert r.status_code == 400 and "a" * 2001 in r.text
    assert "a" * 2001 not in admin.get(anna).text


def test_admin_sees_who_wrote_but_not_what(admin: TestClient, mailer: FakeMailer) -> None:
    group_id, _ = drawn_group(admin)
    admin.post(links(mailer)[email_of("Anna")], data={"wishes": "Tajne skarpety"})
    page = admin.get(f"/admin/groups/{group_id}").text
    assert "Uzupełnione listy: <strong>1/4</strong>" in page and "lista życzeń ✓" in page
    assert "Tajne skarpety" not in page


def test_upgrading_a_database_drawn_before_wish_lists(
    tmp_path, settings: Settings, mailer: FakeMailer
) -> None:
    """The production case: the draw happened (and emails went out) before this feature existed."""
    old = TestClient(create_app(settings, mailer))
    old.post("/admin/login", data={"password": ADMIN_PASSWORD})
    group_id, _ = drawn_group(old)
    pairs = assignments(mailer)
    with old.app.state.db.engine.begin() as conn:  # what an old database looks like
        conn.execute(text("DROP TABLE wish_lists"))
    old.app.state.db.engine.dispose()
    mailer.sent.clear()

    # Restart on the same file: the table is added, nothing else changes.
    admin = TestClient(create_app(settings, mailer))
    admin.post("/admin/login", data={"password": ADMIN_PASSWORD})
    page = admin.get(f"/admin/groups/{group_id}").text
    assert "Wysłane e-maile: <strong>4/4</strong>" in page  # old send status intact
    assert "Linki wysłane: <strong>0/4</strong>" in page

    r = admin.post(f"/admin/groups/{group_id}/wish-links")
    assert "Wysłano 4 e-maile" in r.text
    assert {m["Subject"] for m in mailer.sent} == {"Secret Santa: Twoja lista życzeń (Family 2026)"}
    for email, path in links(mailer).items():  # every page shows the original assignment
        assert f"Kupujesz prezent dla: {pairs[email]}<" in admin.get(path).text
    r = admin.post(f"/admin/groups/{group_id}/wish-links")
    assert "Wszyscy mają już link." in r.text and len(mailer.sent) == 4


def test_lost_link_self_service(client: TestClient, admin: TestClient, mailer: FakeMailer) -> None:
    _, code = drawn_group(admin)
    with admin.app.state.db.sessionmaker() as db:  # pretend the draw was a while ago
        db.execute(update(WishList).values(link_sent_at=utcnow() - timedelta(days=1)))
        db.commit()
    before = len(mailer.sent)

    r = client.post(f"/g/{code}/link", data={"email": "stranger@example.com"})
    generic = "Jeśli ten adres jest na liście tej grupy"
    assert generic in r.text and len(mailer.sent) == before

    r = client.post(f"/g/{code}/link", data={"email": "KASIA@example.com"})
    assert generic in r.text and len(mailer.sent) == before + 1
    assert mailer.sent[-1]["To"].addresses[0].addr_spec == "kasia@example.com"
    assert "/p/" in mailer.sent[-1].get_body(("plain",)).get_content()

    client.post(f"/g/{code}/link", data={"email": "kasia@example.com"})  # right away again
    assert len(mailer.sent) == before + 1


def test_lost_link_form_only_after_the_draw(admin: TestClient, mailer: FakeMailer) -> None:
    _, code = create_group(admin)
    assert "Wyślij mi link" not in admin.get(f"/g/{code}").text
    pid = participant_ids(admin, code)["Anna"]
    admin.post(f"/g/{code}", data={"participant_id": pid, "email": "anna@example.com"})
    admin.post(f"/g/{code}/link", data={"email": "anna@example.com"})
    assert mailer.sent == []


def test_new_address_needs_a_new_link(admin: TestClient, mailer: FakeMailer) -> None:
    group_id, _ = drawn_group(admin)
    page = admin.get(f"/admin/groups/{group_id}").text
    anna = re.search(r'action="/admin/participants/(\d+)/email"', page).group(1)
    admin.post(f"/admin/participants/{anna}/email", data={"email": "anna@new.example"})
    page = admin.get(f"/admin/groups/{group_id}").text
    assert "Linki wysłane: <strong>3/4</strong>" in page

    mailer.fail_for = {"anna@new.example"}
    r = admin.post(f"/admin/participants/{anna}/wish-link")
    assert (
        "Błąd wysyłki (Anna)" in r.text and "Link do listy życzeń: SMTPRecipientsRefused" in r.text
    )
    mailer.fail_for = set()
    r = admin.post(f"/admin/groups/{group_id}/wish-links")
    assert "Wysłano 1 e-mail." in r.text
    assert mailer.sent[-1]["To"].addresses[0].addr_spec == "anna@new.example"


def test_linkify() -> None:
    assert str(linkify("a <b> https://x.pl/?q=1&r=2, koniec.")) == (
        'a &lt;b&gt; <a href="https://x.pl/?q=1&amp;r=2" rel="noopener noreferrer nofollow" '
        'target="_blank">https://x.pl/?q=1&amp;r=2</a>, koniec.'
    )
    assert str(linkify('javascript:alert(1) "https://x.pl"')) == (
        'javascript:alert(1) &#34;<a href="https://x.pl" rel="noopener noreferrer nofollow" '
        'target="_blank">https://x.pl</a>&#34;'
    )
