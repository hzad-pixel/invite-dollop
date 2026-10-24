"""Canva Team Invite bot + web admin panel -- everything from the panel.

Base invite logic: ardiansyah-23/canva-bot (public repo).
New in this fork: a web admin panel (Flask) in the same process.

NO Environment variables are required any more:
  * Telegram token: add/change it in the panel (/admin/token).
  * Webhook: one click in the panel (/admin/token -> Set Webhook).
    The webhook URL is built automatically from the panel URL you are
    visiting, so you never type a webhook URL or a token-bearing link.
  * Canva session: upload it in the panel (/admin/session).
  * Admin password: default admin123, change it in the panel only.

Host-agnostic (Render / Botkeep / VPS / any Python host):
  * Start command everywhere is simply:  python bot.py
  * Panel + health endpoint listen on 0.0.0.0 and PORT, else SERVER_PORT,
    else 5000. Render sets PORT, Botkeep sets SERVER_PORT.
  * Two bot modes, switched from the panel:
      - Polling (default): the bot polls Telegram in a background thread.
      - Webhook: polling is stopped, Telegram POSTs updates to
        /telegram-webhook on this same panel, and a worker thread
        processes them. Delete the webhook in the panel to go back
        to polling. Never run both at once -- Telegram rejects polling
        while a webhook is set (409 conflict).
  * Secrets are NEVER committed: see .gitignore. Token, password hash,
    webhook secret and session live only in runtime files (0600) on the
    host, and the panel NEVER shows a token, a session, or a password --
    only status (set / missing, bot username, webhook URL).

Telegram bot:
  * User sends /start, then an email address.
  * Email is validated, queued, and invited one at a time via Playwright
    Chromium using the saved Canva session file (canva_session.json).
  * That session file is NOT in this repo. Upload it from the admin panel.
"""

import hashlib
import hmac
import json
import os
import queue as queue_mod
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import asyncio
from playwright.async_api import async_playwright
from telegram import Bot, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    from flask import Flask, jsonify, redirect, request, session as flask_session, url_for
except ImportError:  # panel is optional if Flask is not installed
    Flask = None

# ---------------------------------------------------------------- config
TOKEN_FILE = "telegram_token.json"
SESSION_FILE = os.getenv("CANVA_SESSION_FILE", "canva_session.json")
PASSWORD_FILE = "admin_password.json"
DEFAULT_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
PORT = int(os.getenv("PORT") or os.getenv("SERVER_PORT") or 5000)

work_queue = queue_mod.Queue()
stats_lock = threading.Lock()
stats = {
    "queued": 0,
    "invited_ok": 0,
    "invited_fail": 0,
    "last_email": "",
    "last_result": "",
    "last_at": "",
    "started_at": datetime.now(timezone.utc).isoformat(),
    "bot_error": "",
    "mode": "polling",
}


def _bump(key, email="", result=""):
    with stats_lock:
        stats[key] += 1
        if email:
            stats["last_email"] = email
        if result:
            stats["last_result"] = result
        stats["last_at"] = datetime.now(timezone.utc).isoformat()


def _set_mode(mode):
    with stats_lock:
        stats["mode"] = mode


def _set_bot_error(msg):
    with stats_lock:
        stats["bot_error"] = msg


def _snapshot():
    with stats_lock:
        snap = dict(stats)
    token_data = load_token_data()
    snap["token_set"] = bool(token_data.get("token") or os.getenv("TELEGRAM_BOT_TOKEN"))
    snap["bot_username"] = token_data.get("username", "")
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
    snap["queue_size"] = work_queue.qsize()
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
    if os.path.exists(PASSWORD_FILE):
        try:
            with open(PASSWORD_FILE) as f:
                return json.load(f)["hash"], False
        except Exception:
            pass
    return _hash_password(DEFAULT_PASSWORD), True


def save_password_hash(password):
    with open(PASSWORD_FILE, "w") as f:
        json.dump({"hash": _hash_password(password)}, f)
    try:
        os.chmod(PASSWORD_FILE, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------- token store
def load_token_data():
    """Panel-saved token first, env token as fallback. Never logged."""
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE) as f:
                data = json.load(f)
            if data.get("token"):
                return data
        except Exception:
            pass
    env_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    return {"token": env_token, "username": "", "source": "env"} if env_token else {}


def get_token():
    return load_token_data().get("token", "")


def save_token_data(token, username=""):
    data = load_token_data()
    data.update({"token": token, "username": username, "source": "panel"})
    if not data.get("webhook_secret"):
        data["webhook_secret"] = secrets.token_hex(24)
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f)
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    return data


def clear_token_file():
    if os.path.exists(TOKEN_FILE):
        try:
            os.remove(TOKEN_FILE)
        except OSError:
            pass


def _tg_api(token, method, payload=None):
    """One Telegram Bot API call. Returns (ok, result_or_error_string)."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
        if data.get("ok"):
            return True, data.get("result")
        return False, str(data.get("description", "Telegram API error"))
    except urllib.error.HTTPError as e:
        try:
            data = json.load(e)
            return False, str(data.get("description", f"HTTP {e.code}"))
        except Exception:
            return False, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 - shown as a short panel error
        return False, f"{type(e).__name__}: {e}"


def validate_token(token):
    ok, result = _tg_api(token, "getMe")
    if not ok:
        return False, "", result
    if isinstance(result, dict):
        return True, result.get("username", ""), ""
    return False, "", "Unexpected getMe response"


def get_webhook_info(token):
    return _tg_api(token, "getWebhookInfo")


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


async def _process_invite(chat_id, email):
    token = get_token()
    if not token:
        return
    bot = Bot(token=token)
    try:
        await bot.send_message(chat_id=chat_id, text=f"Processing invite for {email}...")
        ok, reason = await invite_canva(email)
        if ok:
            _bump("invited_ok", email, "ok")
            await bot.send_message(
                chat_id=chat_id, text=f"Done! Invitation sent to {email}."
            )
        else:
            _bump("invited_fail", email, "fail")
            await bot.send_message(
                chat_id=chat_id,
                text=f"Could not invite {email}. {reason} Please contact the admin.",
            )
    except Exception as e:  # noqa: BLE001
        _set_bot_error(f"{type(e).__name__}: {e}")


def worker_loop():
    """One worker for both modes: polling handlers and webhook both queue here."""
    while True:
        try:
            chat_id, email = work_queue.get()
            asyncio.run(_process_invite(chat_id, email))
        except Exception as e:  # noqa: BLE001 - worker must never die
            _set_bot_error(f"worker: {type(e).__name__}: {e}")
            time.sleep(2)


# ------------------------------------------------------- polling manager
class PollingManager:
    def __init__(self):
        self.thread = None
        self.stop_event = threading.Event()
        self.token = ""

    def is_running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, token):
        self.stop(token)
        if not token:
            return
        self.token = token
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self, token=""):
        if self.thread is not None and self.thread.is_alive():
            self.stop_event.set()
            self.thread.join(timeout=10)
        self.thread = None

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_run())
        except Exception as e:  # noqa: BLE001 - panel must stay up
            print(f"Polling stopped: {type(e).__name__}: {e}")
            _set_bot_error(f"polling: {type(e).__name__}: {e}")
        finally:
            loop.close()

    async def _async_run(self):
        async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
            text = (update.message.text or "").strip()
            if not is_valid_email(text):
                await update.message.reply_text(
                    "Invalid email format. Please send a valid email address."
                )
                return
            _bump("queued", text, "queued")
            await update.message.reply_text(f"Email {text} received and queued...")
            work_queue.put((update.effective_chat.id, text))

        async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            await update.message.reply_text(
                "Hello! Send your Canva email address to receive a team invite."
            )

        app = ApplicationBuilder().token(self.token).build()
        app.add_handler(CommandHandler("start", start_cmd))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        print("Bot polling started...")
        try:
            while not self.stop_event.is_set():
                await asyncio.sleep(1)
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
            print("Bot polling stopped.")


polling = PollingManager()


def start_polling_if_no_webhook():
    token = get_token()
    if not token:
        print("No Telegram token yet. Add it in the admin panel (/admin/token).")
        return
    ok, info = get_webhook_info(token)
    if ok and isinstance(info, dict) and info.get("url"):
        _set_mode("webhook")
        print(f"Webhook already set ({info['url']}). Polling NOT started.")
        return
    _set_mode("polling")
    polling.start(token)


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
            "<a href='/admin/token'>Telegram token + Webhook</a> | "
            "<a href='/admin/change-password'>Change password</a> | "
            "<a href='/admin/session'>Canva session</a> | "
            "<a href='/admin/logout'>Logout</a></p></body></html>"
        )

    def require_login():
        if not logged_in():
            return redirect(url_for("login"))
        if must_change():
            return redirect(url_for("change_password"))
        return None

    @app.route("/")
    def health():
        snap = _snapshot()
        return {
            "status": "ok",
            "mode": snap["mode"],
            "bot_token_set": snap["token_set"],
            "bot_username": snap["bot_username"],
            "session_exists": snap["session_exists"],
            "invited_ok": snap["invited_ok"],
            "invited_fail": snap["invited_fail"],
        }

    @app.route("/telegram-webhook", methods=["POST"])
    def telegram_webhook():
        token_data = load_token_data()
        token = token_data.get("token", "")
        if not token:
            return jsonify({"ok": False}), 200
        secret = token_data.get("webhook_secret", "")
        if secret and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
            return jsonify({"ok": False}), 200
        try:
            data = request.get_json(force=True) or {}
            message = data.get("message") or {}
            chat = message.get("chat") or {}
            text = (message.get("text") or "").strip()
            chat_id = chat.get("id")
            if not chat_id or not text:
                return jsonify({"ok": True}), 200
            if text.startswith("/start"):
                asyncio.run(
                    Bot(token=token).send_message(
                        chat_id=chat_id,
                        text="Hello! Send your Canva email address to receive a team invite.",
                    )
                )
            elif is_valid_email(text):
                _bump("queued", text, "queued")
                work_queue.put((chat_id, text))
                asyncio.run(
                    Bot(token=token).send_message(
                        chat_id=chat_id,
                        text=f"Email {text} received and queued...",
                    )
                )
            else:
                asyncio.run(
                    Bot(token=token).send_message(
                        chat_id=chat_id,
                        text="Invalid email format. Please send a valid email address.",
                    )
                )
        except Exception as e:  # noqa: BLE001 - always answer 200 to Telegram
            print(f"Webhook update error: {type(e).__name__}: {e}")
        return jsonify({"ok": True}), 200

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
        blocked = require_login()
        if blocked:
            return blocked
        snap = _snapshot()
        body = (
            f"<p>Mode: {snap['mode']} | Polling running: {polling.is_running()}</p>"
            f"<p>Bot token: {'set' if snap['token_set'] else 'NOT set - add it in Telegram token page'}"
            + (f" (@{snap['bot_username']})" if snap["bot_username"] else "")
            + "</p>"
            f"<p>Canva session: {'uploaded' if snap['session_exists'] else 'NOT uploaded'}"
            + (f" ({snap['session_size']} bytes, updated {snap['session_updated']})"
               if snap["session_exists"] else "")
            + "</p>"
            f"<p>Queued: {snap['queued']} | Sent: {snap['invited_ok']} | "
            f"Failed: {snap['invited_fail']} | In queue now: {snap['queue_size']}</p>"
            f"<p>Last: {snap['last_email']} - {snap['last_result']} ({snap['last_at']})</p>"
            + (f"<p>Bot error: {snap['bot_error']}</p>" if snap["bot_error"] else "")
            + "<form method='post' action='/admin/test-session'>"
            "<button>Test Canva session</button></form>"
        )
        return page("Admin dashboard", body)

    @app.route("/admin/test-session", methods=["POST"])
    def test_session_route():
        blocked = require_login()
        if blocked:
            return blocked
        ok, reason = asyncio.run(test_session())
        return page("Canva session test", f"<p>Result: {'OK' if ok else 'FAILED'} - {reason}</p>")

    @app.route("/admin/token", methods=["GET", "POST"])
    def token_page():
        blocked = require_login()
        if blocked:
            return blocked
        msg = ""
        snap = _snapshot()
        # --- save token ---
        if request.method == "POST" and request.form.get("action") == "save":
            new_token = request.form.get("bot_token", "").strip()
            if not new_token:
                msg = "Paste a bot token first."
            else:
                ok, username, err = validate_token(new_token)
                if not ok:
                    msg = f"Token invalid, NOT saved. Telegram says: {err}"
                else:
                    polling.stop()
                    save_token_data(new_token, username)
                    # New token: webhook or polling? Check its own webhook info.
                    ok2, info = get_webhook_info(new_token)
                    if ok2 and isinstance(info, dict) and info.get("url"):
                        _set_mode("webhook")
                        msg = f"Token saved for @{username}. Webhook already set, polling NOT started."
                    else:
                        _set_mode("polling")
                        polling.start(new_token)
                        msg = f"Token saved for @{username}. Polling started."
                    snap = _snapshot()
        # --- set webhook (one click, URL auto-built from panel URL) ---
        if request.method == "POST" and request.form.get("action") == "set_webhook":
            token = get_token()
            if not token:
                msg = "Add a token first."
            else:
                webhook_url = request.host_url.rstrip("/") + "/telegram-webhook"
                if not webhook_url.startswith("https://"):
                    msg = (
                        "Webhook needs a public HTTPS panel URL. "
                        "Open the panel via its public domain (Render/Botkeep Domains), "
                        "not http://localhost, then click again."
                    )
                else:
                    data = save_token_data(token, snap["bot_username"])
                    polling.stop()
                    ok, result = _tg_api(
                        token,
                        "setWebhook",
                        {
                            "url": webhook_url,
                            "secret_token": data["webhook_secret"],
                            "drop_pending_updates": True,
                        },
                    )
                    if ok:
                        _set_mode("webhook")
                        msg = f"Webhook set automatically to {webhook_url}. Polling stopped."
                    else:
                        _set_mode("polling")
                        polling.start(token)
                        msg = f"Webhook FAILED, polling restarted. Telegram says: {result}"
        # --- delete webhook -> back to polling ---
        if request.method == "POST" and request.form.get("action") == "delete_webhook":
            token = get_token()
            if token:
                ok, result = _tg_api(
                    token, "deleteWebhook", {"drop_pending_updates": True}
                )
                if ok:
                    _set_mode("polling")
                    polling.start(token)
                    msg = "Webhook deleted. Polling started."
                else:
                    msg = f"Delete webhook FAILED. Telegram says: {result}"
        # --- webhook info (no token shown) ---
        webhook_info = ""
        token = get_token()
        if token:
            ok, info = get_webhook_info(token)
            if ok and isinstance(info, dict):
                webhook_info = (
                    f"<p>Webhook URL: {info.get('url') or '(none - polling mode)'}<br>"
                    f"Pending updates: {info.get('pending_update_count', '?')}<br>"
                    f"Last error: {info.get('last_error_message', '(none)')}</p>"
                )
            else:
                webhook_info = f"<p>Webhook info unavailable: {info}</p>"
        body = (
            f"<p>Token: {'set' if snap['token_set'] else 'NOT set'}"
            + (f" (@{snap['bot_username']})" if snap["bot_username"] else "")
            + "</p><p>Token is never shown here, for safety. Paste a new one to replace it.</p>"
            + webhook_info
            + "<form method='post'><input type='hidden' name='action' value='save'>"
            "<input type='password' name='bot_token' placeholder='Paste Telegram bot token' required>"
            "<button>Save token</button></form>"
            "<form method='post'><input type='hidden' name='action' value='set_webhook'>"
            "<button>Set Webhook (auto)</button></form>"
            "<form method='post'><input type='hidden' name='action' value='delete_webhook'>"
            "<button>Delete Webhook (back to polling)</button></form>"
            "<p>Set Webhook builds the URL from the panel address you are "
            "visiting right now, so open the panel via its public HTTPS domain first.</p>"
        )
        return page("Telegram token + Webhook", body, msg)

    @app.route("/admin/session", methods=["GET", "POST"])
    def session_page():
        blocked = require_login()
        if blocked:
            return blocked
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
                    json.loads(data)
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
    threading.Thread(target=worker_loop, daemon=True).start()
    start_polling_if_no_webhook()
    if Flask is None:
        print("Flask not installed. Install requirements.txt to enable the panel.")
        while True:
            time.sleep(3600)
    else:
        print(f"Admin panel on 0.0.0.0:{PORT}  (open /admin, default password: admin123)")
        create_panel().run(host="0.0.0.0", port=PORT)
