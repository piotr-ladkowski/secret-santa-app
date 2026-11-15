# secret-santa-app

A small Secret Santa web app: FastAPI, server-rendered pages, SQLite, email over SMTP (Yahoo Mail by
default). The pages and emails are in Polish; the code and these docs are in English.

## How it works

1. **You (the organizer)** log in at `/admin`, create a group and type in everyone's names.
2. You share the group's **link** (`https://…/g/K7Q2XM9P`) or just the **code**. People can also
   type the code on the home page.
3. **Everyone** opens it, picks their own name and enters their email. A name can be claimed only
   once. You see who has joined (and their addresses) on the group page.
4. When everyone has joined, you press **Losuj i wyślij e-maile**. Each person gets an email with the
   name of the person they buy a gift for.

The draw puts everyone in one random circle (A→B→C→…→A): nobody draws themselves and, with 3+
people, no two people simply swap gifts.

**You can take part too:** the admin pages never show who drew whom. That's only in the emails. If an
email bounces or lands in spam, fix the address and press **Wyślij** / **Wyślij ponownie** next to
that person. They get the same assignment again, and you still don't see it.

If you do want to know (after Christmas, or to sort out a mix-up), the group page has a folded
**Chcę zobaczyć, kto kogo wylosował** → **Pokaż wyniki losowania**, behind a confirmation. It is a
POST rendered with `Cache-Control: no-store`, so no URL, bookmark or back button shows the pairs
again by accident.

### Keeping the results secret from yourself

The app never shows or logs the pairs on its own (traces carry SQL without parameter values). They
can still reach you from outside the app:

- **The sending mailbox.** Yahoo may keep a copy of every sent email in **Sent**. Send yourself a test
  email first and check Sent: if it's there, the real ones will be too. **Bounces** for mistyped
  addresses also land in that inbox and usually quote the email. The clean fix is a separate Yahoo
  account just for this app that you never open. With your own account, stay out of Sent after the
  draw and don't open "Mail Delivery" messages; to find a bad address, ask who didn't get theirs.
  The emails keep the name out of the subject and the preview text, so the list view alone
  doesn't give it away.
- **The database** (`receiver_id` in `participants`): don't `SELECT *` from it or open a backup in a
  DB browser. `SELECT id, name, email, email_sent_at, email_error FROM participants` is safe.
- **Sending someone's result to yourself.** After the draw, **Wyślij** sends that person's result to
  the address in their row. Never put your own address on someone else's row.

Things to know:

- Anyone with the code can claim any name that is still free. Check the addresses on the group page
  before the draw. Before the draw you can clear a wrongly claimed name ("Edytuj" → save an empty email)
  so the right person can claim it.
- After the draw the list is frozen. A failed send doesn't redo the draw: fix the address and press
  "Wyślij niewysłane (N)".
- Emails are sent while the request runs (one SMTP connection for the batch). For family-sized
  groups that takes a few seconds.

## Configuration

Environment variables (or a `.env` file, see `.env.example`):

| Variable | Default | |
| --- | --- | --- |
| `ADMIN_PASSWORD` | **required** | Password for `/admin` (min. 8 characters) |
| `SECRET_KEY` | **required** | Signs the session cookie. `openssl rand -hex 32` |
| `PUBLIC_BASE_URL` | request URL | e.g. `https://secret-santa.pladkowski.pl`, used for share links. `https://` also makes the cookie `Secure` |
| `DATABASE_URL` | `sqlite:///data/secret-santa.db` | The image sets `sqlite:////data/secret-santa.db` (absolute) |
| `CHECK_EMAIL_DNS` | `true` | Look up the email's domain on sign-up (catches `gmail.con`) |
| `SMTP_HOST` | `smtp.mail.yahoo.com` | |
| `SMTP_PORT` | `465` | |
| `SMTP_SECURITY` | `ssl` | `ssl` (465), `starttls` (587) or `none` (local testing) |
| `SMTP_USERNAME` | | Your full Yahoo address |
| `SMTP_PASSWORD` | | A Yahoo **app password**, not your normal password |
| `SMTP_FROM` | `SMTP_USERNAME` | Yahoo only accepts its own address here |
| `SMTP_FROM_NAME` | `Secret Santa` | Display name on the emails |
| `LOG_LEVEL` | `INFO` | |

### Yahoo Mail app password

Yahoo doesn't accept your account password over SMTP:

1. Log in to Yahoo → **Account Info** → **Account Security**.
2. **Generate app password** (or "Manage app passwords"). Name it e.g. `secret-santa`.
3. Use the generated 16-character password as `SMTP_PASSWORD` and your full address as
   `SMTP_USERNAME`.

Then use **Wyślij testowy e-mail** on `/admin` before the draw.

Yahoo limits how much an account can send per day. A family group is far below that, but don't
use this for a whole company.

## Development

```bash
uv sync
cp .env.example .env

# terminal 1: a local SMTP server that prints every email instead of sending it
uv run python -m aiosmtpd -n -l localhost:1025

# terminal 2
uv run uvicorn secret_santa.main:create_app --factory --reload
# http://localhost:8000/admin   (password from .env)
```

Checks (the same as CI):

```bash
uv run ruff check src tests && uv run ruff format --check src tests && uv run pytest
```

## Container

```bash
docker build -t secret-santa .
docker run --rm -p 8000:8000 -v santa-data:/data \
  -e ADMIN_PASSWORD=changeme123 -e SECRET_KEY=$(openssl rand -hex 32) \
  -e SMTP_USERNAME=you@yahoo.com -e SMTP_PASSWORD=... secret-santa
```

Runs as uid 10001, works with a read-only root filesystem, and keeps its state in `/data`
(one SQLite file). Health endpoints: `/healthz` (process up) and `/readyz` (database reachable).
The image starts under `opentelemetry-instrument`. Tracing stays off until
`OTEL_TRACES_EXPORTER=otlp` and an `OTEL_EXPORTER_OTLP_ENDPOINT` are set.

## Deployment

Pushing to `main` runs `.github/workflows/image.yaml`: tests, then builds and pushes
`ghcr.io/piotr-ladkowski/secret-santa-app:<commit sha>`. Argo CD Image Updater picks up the newest
tag and rolls it out. The Kubernetes manifests live in
[homelab-gitops](https://github.com/piotr-ladkowski/homelab-gitops) under
`apps/shared-apps/secret-santa/`.
