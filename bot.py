"""Canva Team Invite bot + web admin panel.

Base invite logic: ardiansyah-23/canva-bot (public repo).
New in this fork: a web admin panel (Flask) in the same process.

Host-agnostic by design (Render / Botkeep / VPS / any Python host):
  * Panel + health endpoint listen on 0.0.0.0 and PORT, else SERVER_PORT, else 5000.
    Render sets PORT, Botkeep sets SERVER_PORT -- both are honoured, no port
    is hard-coded, so there is no "51234" style port to configure on a host.
  * Start command everywhere is simply:  python bot.py
  * Secrets are NEVER committed: see .gitignore and .env.example.

Telegram bot:
  * User sends /start, then an email address.
  * Email is validated, queued, and invited one at a time via Playwright
    Chromium using the saved Canva session file (canva_session.json).
  * That session file is NOT in this repo. Upload it from the admin panel.

Admin panel (default login, change it on first login):
  * Open /admin  -> login page.
  * Default password: admin123  (change it from the panel on first login;
    until you do, the dashboard stays locked behind the change-password page).
  * New password is stored ONLY as a salted PBKDF2 hash in
    admin_password.json (runtime file, gitignored, never in code/repo).
    It can only be changed from inside the panel, never from code.
  * Panel pages: dashboard (invite stats), change password, upload/paste
    Canva session, test whether the session is still logged in to Canva.
  * The panel NEVER shows the session file content, the bot token, or a
    password -- only status (exists / missing, size, last updated).

Environment variables:
  TELEGRAM_BOT_TOKEN  Telegram bot token (required for the bot; panel still
                      runs without it so you can see the missing-token status)
  ADMIN_PASSWORD      Optional first password. If unset, default is admin123.
  ADMIN_SESSION_SECRET Optional Flask session secret. If unset, a random one
                      is generated at each start (logins do not survive restart).
  CANVA_SESSION_FILE  Optional path, default canva_session.json
  PORT / SERVER_PORT  Panel port (see above)
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone

import asyncio
from playwright.async_api import async_playwright
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    from flask import Flask, redirect, request, session as flask_session, url_for
except ImportError:  # panel is optional if Flask is not installed
    Flask = None

# ---------------------------------------------------------------- config
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
SESSION_FILE = os.getenv("CANVA_SESSION_FILE", "canva_session.json")
PASSWORD_FILE = "admin_password.json"
DEFAULT_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
PORT = int(os.getenv("PORT") or os.getenv("SERVER_PORT") or 5000)

queue = asyncio.Queue()
stats_lock = threading.Lock()
stats = {
    "queued": 0,
    "invited_ok": 0,
    "invited_fail": 0,
    "last_email": "",
    "last_result": "",
    "last_at": "",
    "started_at": datetime.now(timezone.utc).isoformat(),
}


def _bump(key, email="", result=""):
    with stats_lock:
        stats[key] += 1
        if email:
            stats["last_email"] = email
        if result:
            stats["last_result"] = result
        stats["last_at"] = datetime.now(timezone.utc).isoformat()


def _snapshot():
    with stats_lock:
        snap = dict(stats)
    snap["queue_size"] = queue.qsize() if queue else 0
    snap["token_set"] = bool(TOKEN)
    snap["session_exists"] = os.path.exists(SESSION_FILE)
    snap["session_size"] = (
        os.path.getsize(SESSION_FILE) if os.path.exists(SESSION_FILE) else 0
    )
    snap["session_updated"] = (
        datetime.fromtimestamp(
            os.path.getmtime(SESSION_FILE), tz=timezone.utc
        ).isoformat()
        if os.path.exists(SESSION_FILE)
        else ""
    )
    return snap


# ------------------------------------------------------- password store
def _hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), 200_000
    ).hex()
    return f"pbkdf2:{salt}:{digest}"


def _check_password(password, stored):
    try:
        _, salt, digest = stored.split(":")
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), 200_000
        ).hex()
        return hmac.compare_digest(candidate, digest)
    except Exception:
        return False


def load_password_hash():
    """Return (hash, is_default). The hash file is created on first change."""
    if os.path.exists(PASSWORD_FILE):
        try:
            with open(PASSWORD_FILE) as f:
                return json.load(f)["hash"], False
        except Exception:
            pass
    return _hash_password(DEFAULT_PASSWORD), True


def save_password_hash(password):
    data = {"hash": _hash_password(password)}
    with open(PASSWORD_FILE, "w") as f:
        json.dump(data, f)
    try:
        os.chmod(PASSWORD_FILE, 0o600)
    except OSError:
        pass


def is_valid_email(email: str) -> bool:
    return bool(re.match(r"[^@]+@[^@]+\.[^@]+", email))


# ------------------------------------------------------------ invite job
async def invite_canva(email: str):
    """Return (ok, reason). Reason is user-safe, never contains cookies."""
    if not os.path.exists(SESSION_FILE):
        return False, "Canva session not uploaded yet. Upload it from the admin panel."
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        try:
            context = await browser.new_context(storage_state=SESSION_FILE)
            page = await context.new_page()
            await page.goto(
                "https://www.canva.com/settings/your-team/members", timeout=60000
            )
            await page.click(
                "button:has-text('Invite people'), button:has-text('Undang orang')"
            )
            email_input = page.locator(
                "input[type='email'], input[placeholder*='name@example.com']"
            )
            await email_input.fill(email)
            await page.keyboard.press("Enter")
            await page.click(
                "button:has-text('Send invitations'), button:has-text('Kirim undangan')"
            )
            await page.wait_for_timeout(3000)
            return True, "Invitation sent."
        except Exception as e:  # noqa: BLE001 - surfaced as a short reason only
            return False, f"Invite failed: {type(e).__name__}"
        finally:
            await browser.close()


async def test_session():
    """Check if the saved session is still logged in to Canva (no data shown)."""
    if not os.path.exists(SESSION_FILE):
        return False, "No session file uploaded."
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        try:
            context = await browser.new_context(storage_state=SESSION_FILE)
            page = await context.new_page()
            await page.goto(
                "https://www.canva.com/settings/your-team/members", timeout=60000
            )
            await page.wait_for_timeout(3000)
            invite_visible = await page.locator(
                "button:has-text('Invite people')"
            ).count()
            return bool(invite_visible), (
                "Session looks logged in."
                if invite_visible
                else "Session may be expired (Invite button not found)."
            )
        except Exception as e:  # noqa: BLE001
            return False, f"Test failed: {type(e).__name__}"
        finally:
            await browser.close()


async def worker():
    """Process invites one at a time from the queue."""
    while True:
        update, email = await queue.get()
        try:
            await update.message.reply_text(f"Processing invite for {email}...")
            ok, reason = await invite_canva(email)
            if ok:
                _bump("invited_ok", email, "ok")
                await update.message.reply_text(f"Done! Invitation sent to {email}.")
            else:
                _bump("invited_fail", email, "fail")
                await update.message.reply_text(
                    f"Could not invite {email}. {reason} Please contact the admin."
                )
        finally:
            queue.task_done()


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not is_valid_email(text):
        await update.message.reply_text(
            "Invalid email format. Please send a valid email address."
        )
        return
    _bump("queued", text, "queued")
    await update.message.reply_text(f"Email {text} received and queued...")
    await queue.put((update, text))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hello! Send your Canva email address to receive a team invite."
    )


async def post_init(application):
    asyncio.create_task(worker())


def run_bot():
    if not TOKEN:
        print("WARNING: TELEGRAM_BOT_TOKEN is not set. Bot disabled, panel still runs.")
        return
    try:
        app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        print("Bot running...")
        app.run_polling()
    except Exception as e:  # noqa: BLE001 - panel must stay up to show the error
        print(f"Bot stopped: {type(e).__name__}: {e}")
        with stats_lock:
            stats["last_result"] = f"bot error: {type(e).__name__}"


# ------------------------------------------------------------ admin panel
def create_panel():
    app = Flask(__name__)
    app.secret_key = os.getenv("ADMIN_SESSION_SECRET") or secrets.token_hex(32)

    def logged_in():
        return bool(flask_session.get("admin"))

    def must_change():
        _, is_default = load_password_hash()
        return is_default and not flask_session.get("password_changed_ok")

    def page(title, body, msg=""):
        alert = f"<p><b>{msg}</b></p>" if msg else ""
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>" + title + "</title>"
            "<style>body{font-family:sans-serif;max-width:640px;margin:40px auto;"
            "padding:0 16px}input,textarea,button{padding:8px;margin:4px 0;width:100%;"
            "box-sizing:border-box}button{cursor:pointer}</style></head><body>"
            f"<h1>{title}</h1>{alert}{body}"
            "<p><a href='/admin'>Dashboard</a> | "
            "<a href='/admin/change-password'>Change password</a> | "
            "<a href='/admin/session'>Canva session</a> | "
            "<a href='/admin/logout'>Logout</a></p></body></html>"
        )

    @app.route("/")
    def health():
        snap = _snapshot()
        return {
            "status": "ok",
            "bot_token_set": snap["token_set"],
            "session_exists": snap["session_exists"],
            "invited_ok": snap["invited_ok"],
            "invited_fail": snap["invited_fail"],
        }

    @app.route("/admin/login", methods=["GET", "POST"])
    def login():
        msg = ""
        if request.method == "POST":
            stored, _ = load_password_hash()
            if _check_password(request.form.get("password", ""), stored):
                flask_session["admin"] = True
                _, is_default = load_password_hash()
                if is_default:
                    return redirect(url_for("change_password"))
                return redirect(url_for("dashboard"))
            msg = "Wrong password."
        return page(
            "Admin login",
            "<form method='post'><input type='password' name='password' "
            "placeholder='Admin password' required><button>Login</button></form>",
            msg,
        )

    @app.route("/admin/logout")
    def logout():
        flask_session.clear()
        return redirect(url_for("login"))

    @app.route("/admin/change-password", methods=["GET", "POST"])
    def change_password():
        if not logged_in():
            return redirect(url_for("login"))
        msg = ""
        if request.method == "POST":
            new = request.form.get("new_password", "")
            confirm = request.form.get("confirm_password", "")
            if len(new) < 8:
                msg = "New password must be at least 8 characters."
            elif new != confirm:
                msg = "Passwords do not match."
            else:
                save_password_hash(new)
                flask_session["password_changed_ok"] = True
                return redirect(url_for("dashboard"))
        _, is_default = load_password_hash()
        note = (
            "<p><b>You are still on the default password. Change it now "
            "to unlock the dashboard.</b></p>"
            if is_default
            else ""
        )
        return page(
            "Change admin password",
            note
            + "<form method='post'><input type='password' name='new_password' "
            "placeholder='New password (min 8 chars)' required>"
            "<input type='password' name='confirm_password' "
            "placeholder='Confirm new password' required>"
            "<button>Save new password</button></form>",
            msg,
        )

    @app.route("/admin")
    def dashboard():
        if not logged_in():
            return redirect(url_for("login"))
        if must_change():
            return redirect(url_for("change_password"))
        snap = _snapshot()
        body = (
            f"<p>Bot token: {'set' if snap['token_set'] else 'MISSING - set TELEGRAM_BOT_TOKEN'}</p>"
            f"<p>Canva session: {'uploaded' if snap['session_exists'] else 'NOT uploaded'}"
            + (f" ({snap['session_size']} bytes, updated {snap['session_updated']})"
               if snap["session_exists"] else "")
            + "</p>"
            f"<p>Queued: {snap['queued']} | Sent: {snap['invited_ok']} | "
            f"Failed: {snap['invited_fail']} | In queue now: {snap['queue_size']}</p>"
            f"<p>Last: {snap['last_email']} - {snap['last_result']} ({snap['last_at']})</p>"
            "<form method='post' action='/admin/test-session'>"
            "<button>Test Canva session</button></form>"
        )
        return page("Admin dashboard", body)

    @app.route("/admin/test-session", methods=["POST"])
    def test_session_route():
        if not logged_in():
            return redirect(url_for("login"))
        if must_change():
            return redirect(url_for("change_password"))
        ok, reason = asyncio.run(test_session())
        return page(
            "Canva session test",
            f"<p>Result: {'OK' if ok else 'FAILED'} - {reason}</p>",
        )

    @app.route("/admin/session", methods=["GET", "POST"])
    def session_page():
        if not logged_in():
            return redirect(url_for("login"))
        if must_change():
            return redirect(url_for("change_password"))
        msg = ""
        if request.method == "POST":
            data = None
            uploaded = request.files.get("session_file")
            if uploaded and uploaded.filename:
                data = uploaded.read().decode("utf-8", "ignore")
            elif request.form.get("session_json", "").strip():
                data = request.form.get("session_json", "")
            if data:
                try:
                    json.loads(data)  # validate only, never display it back
                    with open(SESSION_FILE, "w") as f:
                        f.write(data)
                    try:
                        os.chmod(SESSION_FILE, 0o600)
                    except OSError:
                        pass
                    msg = "Session saved. Use 'Test Canva session' to check it."
                except Exception:
                    msg = "Invalid JSON. Upload a valid canva_session.json file."
            else:
                msg = "Upload a file or paste session JSON."
        snap = _snapshot()
        body = (
            f"<p>Current: {'uploaded' if snap['session_exists'] else 'not uploaded'}"
            + (f" ({snap['session_size']} bytes)" if snap["session_exists"] else "")
            + "</p><p>Session content is never shown here, for safety.</p>"
            "<form method='post' enctype='multipart/form-data'>"
            "<input type='file' name='session_file'>"
            "<textarea name='session_json' rows='6' "
            "placeholder='Or paste session JSON here'></textarea>"
            "<button>Save session</button></form>"
        )
        return page("Canva session", body, msg)

    return app


if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    if Flask is None:
        print("Flask not installed. Install requirements.txt to enable the panel.")
        while True:
            time.sleep(3600)
    else:
        print(f"Admin panel on 0.0.0.0:{PORT}  (open /admin, default password: admin123)")
        create_panel().run(host="0.0.0.0", port=PORT)
