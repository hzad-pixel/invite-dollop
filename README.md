# invite-dollop — Canva Team Invite bot + admin panel

Telegram bot that invites an email address to a Canva team, plus a web
admin panel in the same process. Base invite logic from the public repo
`ardiansyah-23/canva-bot`; the admin panel is new code in this repo.

## How it works

1. User opens the Telegram bot, sends `/start`, then sends an email.
2. The bot validates the email, queues it, and invites one email at a time.
3. Inviting uses Playwright Chromium with a saved Canva session
   (`canva_session.json`) on `canva.com/settings/your-team/members`.

## Admin panel

Open `/admin` on the deployed URL.

- **Default password: `admin123`** — change it on first login. Until you do,
  the dashboard stays locked behind the change-password page.
- The new password is stored only as a salted hash in `admin_password.json`
  (runtime file, never committed). It can only be changed from the panel.
- **Canva session:** panel → Canva session → upload `canva_session.json`
  or paste its JSON. The file is never shown back, never committed.
  Use “Test Canva session” to check if the login is still alive.
- **Dashboard:** invite counts, queue size, token/session status.

## Run anywhere (no hard-coded port)

Start command everywhere is:

```
python bot.py
```

The panel binds `0.0.0.0` and reads `PORT`, else `SERVER_PORT`, else `5000`.
Render sets `PORT`, Botkeep sets `SERVER_PORT` — both work with no change.

Set in the host's Environment panel (never in code):

```
TELEGRAM_BOT_TOKEN=...
```

Optional: `ADMIN_PASSWORD`, `ADMIN_SESSION_SECRET`, `CANVA_SESSION_FILE`.

## Deploy

### Botkeep

New workload → GitHub repo → Python → start command `python bot.py` →
set `TELEGRAM_BOT_TOKEN` in Environment → open the panel via Domains,
log in at `/admin`, change the default password, upload the Canva session.

### Render

New Web Service → this repo → build `pip install -r requirements.txt` →
start `python bot.py` → set `TELEGRAM_BOT_TOKEN`. A `render.yaml` is included.

### VPS / Docker

```
docker build -t invite-dollop .
docker run -e TELEGRAM_BOT_TOKEN=... -p 5000:5000 invite-dollop
```

## Honest limits (read before deploying)

- **Playwright/Chromium is the risk on free hosts.** The Dockerfile image
  already has Chromium + system libraries, so Docker/VPS works. Plain
  Python runtimes on Render free / Botkeep do NOT document Chromium or its
  system libraries — the Telegram bot and panel will start, but the invite
  step may fail there until a host with Chromium is used. Test one invite
  after deploy; do not assume it works from the panel being up.
- **The session file is not in this repo and cannot be made by the bot.**
  Create it once from a logged-in Canva browser (Playwright
  `storage_state`), then upload it from the panel. When it expires,
  re-upload a fresh one.
- **Default password:** change it on first login. A public panel URL with
  the default password lets anyone change the password and steal the
  Canva session. Treat the session like a Canva password.
