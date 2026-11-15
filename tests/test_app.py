import re

from fastapi.testclient import TestClient
from sqlalchemy import update

from secret_santa.models import Participant
from secret_santa.web import plural
from tests.conftest import FakeMailer, create_group, participant_ids


def register_all(client: TestClient, code: str) -> dict[str, str]:
    emails = {}
    for name, pid in participant_ids(client, code).items():
        email = f"{name.lower()}@example.com"
        r = client.post(f"/g/{code}", data={"participant_id": pid, "email": email})
        assert r.status_code == 200, r.text
        emails[name] = email
    return emails


def assignments(mailer: FakeMailer) -> dict[str, str]:
    """{giver email: receiver name} parsed from the plain-text part of each sent email."""
    out = {}
    for msg in mailer.sent:
        body = msg.get_body(("plain",)).get_content()
        out[msg["To"].addresses[0].addr_spec] = re.search(r"dla tej osoby:\s+(.+)", body).group(1)
    return out


def assert_no_pairs_shown(html: str) -> None:
    """Admin pages must not pair givers with receivers (names on their own are fine)."""
    assert 'class="pairs"' not in html and "dla tej osoby" not in html


def test_health(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_admin_requires_login(client: TestClient) -> None:
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    r = client.post("/admin/groups", data={"name": "x"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    assert client.post("/admin/login", data={"password": "nope"}).status_code == 401


def test_join_by_code_is_forgiving(admin: TestClient) -> None:
    _, code = create_group(admin)
    messy = f" {code[:4].lower()}-{code[4:]} "
    r = admin.get("/join", params={"code": messy}, follow_redirects=False)
    assert r.headers["location"] == f"/g/{code}"
    assert admin.get(f"/g/{code.lower()}", follow_redirects=False).status_code == 303
    assert admin.get("/join", params={"code": "NOPE1234"}).status_code == 404
    assert admin.get("/g/NOPE1234").status_code == 404


def test_full_flow(admin: TestClient, mailer: FakeMailer) -> None:
    group_id, code = create_group(admin)
    page = admin.get(f"/g/{code}").text
    assert "Budget: 100 zł" in page and "0 z 4" in page

    # Drawing too early is refused and sends nothing.
    r = admin.post(f"/admin/groups/{group_id}/draw")
    assert mailer.sent == []
    assert "Wciąż czekamy na: Anna, Bartek, Kasia, Tomek" in r.text
    assert "zapisy otwarte" in r.text

    emails = register_all(admin, code)
    assert "4 z 4" in admin.get(f"/g/{code}").text

    r = admin.post(f"/admin/groups/{group_id}/draw")
    assert "Wysłano 4 e-maile" in r.text
    pairs = assignments(mailer)
    assert set(pairs) == set(emails.values())
    assert sorted(pairs.values()) == sorted(emails)  # everyone receives exactly once
    for name, email in emails.items():
        assert pairs[email] != name  # nobody draws themselves
    assert all(m["From"].addresses[0].addr_spec == "santa@yahoo.com" for m in mailer.sent)

    # The organizer's page must not reveal the assignments.
    assert_no_pairs_shown(r.text)

    # A second draw (double click, second tab) changes nothing and sends nothing.
    admin.post(f"/admin/groups/{group_id}/draw")
    assert len(mailer.sent) == 4
    assert "Losowanie już się odbyło" in admin.get(f"/g/{code}").text


def test_name_cannot_be_claimed_twice(client: TestClient, admin: TestClient) -> None:
    _, code = create_group(admin)
    pid = participant_ids(client, code)["Anna"]
    assert (
        client.post(f"/g/{code}", data={"participant_id": pid, "email": "a@x.pl"}).status_code
        == 200
    )
    r = client.post(f"/g/{code}", data={"participant_id": pid, "email": "evil@x.pl"})
    assert r.status_code == 409 and "już zajęte" in r.text
    assert "Anna" not in participant_ids(client, code)


def test_participant_from_another_group_rejected(admin: TestClient) -> None:
    _, code_a = create_group(admin)
    _, code_b = create_group(admin, "Xavier\nYvonne\nZoe")
    pid_b = participant_ids(admin, code_b)["Xavier"]
    r = admin.post(f"/g/{code_a}", data={"participant_id": pid_b, "email": "x@x.pl"})
    assert r.status_code == 409
    assert "Xavier" in participant_ids(admin, code_b)


def test_invalid_email(admin: TestClient) -> None:
    _, code = create_group(admin)
    pid = participant_ids(admin, code)["Anna"]
    r = admin.post(f"/g/{code}", data={"participant_id": pid, "email": "not-an-email"})
    assert r.status_code == 400 and "brakuje znaku @" in r.text
    r = admin.post(f"/g/{code}", data={"participant_id": pid, "email": "anna@gmail"})
    assert r.status_code == 400 and "poprawny adres" in r.text
    assert "Anna" in participant_ids(admin, code)


def test_joined_page_remembers_you(admin: TestClient) -> None:
    _, code = create_group(admin)
    pid = participant_ids(admin, code)["Kasia"]
    r = admin.post(f"/g/{code}", data={"participant_id": pid, "email": "Kasia@Example.com"})
    assert "Jesteś na liście, Kasia!" in r.text and "Kasia@example.com" in r.text


def test_failed_sends_can_be_retried(admin: TestClient, mailer: FakeMailer) -> None:
    group_id, code = create_group(admin)
    register_all(admin, code)
    mailer.fail_for = {"bartek@example.com"}
    r = admin.post(f"/admin/groups/{group_id}/draw")
    assert "Wysłano 3 e-maile, nieudane: 1" in r.text and "SMTPRecipientsRefused" in r.text

    # Fix the address, then "send unsent" only emails the one that is missing.
    bartek = participant_ids_admin(admin, group_id)["Bartek"]
    admin.post(f"/admin/participants/{bartek}/email", data={"email": "bartek@fixed.pl"})
    mailer.fail_for = set()
    r = admin.post(f"/admin/groups/{group_id}/send")
    assert "Wysłano 1 e-mail." in r.text
    assert [m["To"].addresses[0].addr_spec for m in mailer.sent][-1] == "bartek@fixed.pl"
    assert len(mailer.sent) == 4

    # Resend one: same assignment again.
    before = assignments(mailer)["anna@example.com"]
    admin.post(f"/admin/participants/{participant_ids_admin(admin, group_id)['Anna']}/send")
    assert len(mailer.sent) == 5 and assignments(mailer)["anna@example.com"] == before


def test_admin_can_unregister_before_draw(admin: TestClient) -> None:
    group_id, code = create_group(admin)
    pid = participant_ids(admin, code)["Tomek"]
    admin.post(f"/g/{code}", data={"participant_id": pid, "email": "wrong@x.pl"})
    assert "Tomek" not in participant_ids(admin, code)
    admin.post(f"/admin/participants/{pid}/email", data={"email": ""})
    assert "Tomek" in participant_ids(admin, code)


def test_list_frozen_after_draw(admin: TestClient) -> None:
    group_id, code = create_group(admin, "A\nB\nC")
    register_all(admin, code)
    admin.post(f"/admin/groups/{group_id}/draw")
    r = admin.post(f"/admin/groups/{group_id}/participants", data={"names": "D"})
    assert "listy nie można już zmieniać" in r.text
    pid = participant_ids_admin(admin, group_id)["A"]
    assert "listy nie można już zmieniać" in admin.post(f"/admin/participants/{pid}/delete").text


def test_names_are_deduplicated(admin: TestClient) -> None:
    _, code = create_group(admin, "Anna\n anna \n\nBartek, Kasia\nAnna")
    assert sorted(participant_ids(admin, code)) == ["Anna", "Bartek", "Kasia"]


def participant_ids_admin(admin: TestClient, group_id: int) -> dict[str, int]:
    page = admin.get(f"/admin/groups/{group_id}").text
    ids = re.findall(r'action="/admin/participants/(\d+)/rename"', page)
    names = re.findall(r'<input name="name" value="([^"]+)" required maxlength="100"', page)
    return dict(zip(names, map(int, ids), strict=True))


def test_non_ascii_local_part_rejected(admin: TestClient) -> None:
    # Python's email headers and Yahoo can't carry SMTPUTF8 addresses; refuse them up front.
    _, code = create_group(admin, "Bożena\nŁukasz\nŻaneta")
    pid = participant_ids(admin, code)["Bożena"]
    r = admin.post(f"/g/{code}", data={"participant_id": pid, "email": "bożena@example.com"})
    assert r.status_code == 400 and "polskich liter" in r.text
    # Polish names are fine everywhere else, and IDN domains are stored as punycode.
    r = admin.post(f"/g/{code}", data={"participant_id": pid, "email": "bozena@żółw.pl"})
    assert r.status_code == 200 and "bozena@xn--w-uga1v8h.pl" in r.text


def test_one_unsendable_address_does_not_block_the_rest(
    admin: TestClient, mailer: FakeMailer
) -> None:
    group_id, code = create_group(admin)
    register_all(admin, code)
    # Simulate an address that got into the DB some other way and that the email library rejects.
    with admin.app.state.db.sessionmaker() as db:
        db.execute(update(Participant).where(Participant.name == "Kasia").values(email="kasia@@x"))
        db.commit()
    r = admin.post(f"/admin/groups/{group_id}/draw")
    assert r.status_code == 200
    assert "Wysłano 3 e-maile, nieudane: 1" in r.text and "Błędny adres" in r.text
    assert len(mailer.sent) == 3


def test_results_only_on_explicit_request(admin: TestClient, mailer: FakeMailer) -> None:
    group_id, code = create_group(admin)
    # Before the draw there is nothing to show.
    r = admin.post(f"/admin/groups/{group_id}/reveal")
    assert "Najpierw przeprowadź losowanie" in r.text
    register_all(admin, code)
    admin.post(f"/admin/groups/{group_id}/draw")
    by_email = assignments(mailer)
    emails = {n: f"{n.lower()}@example.com" for n in ["Anna", "Bartek", "Kasia", "Tomek"]}

    # The group page only offers the button; no URL a browser could reopen shows the pairs.
    page = admin.get(f"/admin/groups/{group_id}").text
    assert "Pokaż wyniki losowania" in page
    assert_no_pairs_shown(page)
    assert admin.get(f"/admin/groups/{group_id}/reveal").status_code == 405

    r = admin.post(f"/admin/groups/{group_id}/reveal")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    shown = dict(re.findall(r"<tr><td>([^<]+)</td><td>🎁 ([^<]+)</td></tr>", r.text))
    assert shown == {name: by_email[email] for name, email in emails.items()}


def test_reveal_needs_admin(client: TestClient, admin: TestClient) -> None:
    group_id, _ = create_group(admin)
    admin.post("/admin/logout")
    r = client.post(f"/admin/groups/{group_id}/reveal", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"


def test_polish_plurals() -> None:
    forms = ("osoba", "osoby", "osób")
    got = {n: plural(n, *forms) for n in (0, 1, 2, 4, 5, 11, 12, 14, 21, 22, 25, 104, 112)}
    assert got == {
        0: "osób", 1: "osoba", 2: "osoby", 4: "osoby", 5: "osób", 11: "osób", 12: "osób",
        14: "osób", 21: "osób", 22: "osoby", 25: "osób", 104: "osoby", 112: "osób",
    }  # fmt: skip
