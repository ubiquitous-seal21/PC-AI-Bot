"""Home AI Hub: web server. Serves the GUI and a small JSON API."""
import datetime
import hashlib
import os
import subprocess
import threading
import time

from flask import Flask, jsonify, request, send_from_directory, session

import engine as E

PASSWORD = os.environ.get("HUB_PASSWORD", "")
if len(PASSWORD) < 8:
    raise SystemExit("Set HUB_PASSWORD in .env (at least 8 characters).")
ACCOUNT_NAME = os.environ.get("HUB_ACCOUNT_NAME", "admin").strip() or "admin"
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "40"))

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("HUB_COOKIE_SECURE", "false").lower() in ("1", "true", "yes")
app.config["PERMANENT_SESSION_LIFETIME"] = datetime.timedelta(hours=12)
app.secret_key = hashlib.sha256(("hub-session|" + PASSWORD).encode()).digest()

_fails = {}
_fail_lock = threading.Lock()
_update_lock = threading.Lock()
_update_running = False
UPDATE_STATUS = os.environ.get("UPDATE_STATUS", "/repo/.ai-hub-update.log")
UPDATE_SCRIPT = os.environ.get("UPDATE_SCRIPT", "/app/update.sh")

try:
    with open(UPDATE_STATUS, "r", encoding="utf-8") as status:
        initial_update_status = status.read().strip()
except FileNotFoundError:
    initial_update_status = ""
_update_running = bool(initial_update_status.startswith(("Update started", "Update in progress")))


def mark_update_finished(proc):
    proc.wait()
    global _update_running
    deadline = time.time() + 600
    while time.time() < deadline:
        try:
            with open(UPDATE_STATUS, "r", encoding="utf-8") as status:
                message = status.read().strip()
        except FileNotFoundError:
            message = ""
        if message.startswith(("Update complete:", "Update failed:", "Update blocked")):
            with _update_lock:
                _update_running = False
            return
        time.sleep(1)
    with _update_lock:
        _update_running = False


def ok(**kw):
    return jsonify({"ok": True, **kw})


def err(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


@app.errorhandler(413)
def upload_too_large(_):
    return err(f"Upload exceeds the {MAX_UPLOAD_MB} MB limit.", 413)


@app.before_request
def guard():
    p = request.path
    if p in ("/", "/api/login") or p == "/favicon.ico":
        return None
    if not session.get("auth") or not session.get("account_id"):
        return err("Please log in.", 401)
    if request.method != "GET" and request.headers.get("X-Hub") != "1":
        return err("Bad request.", 403)
    return None


@app.get("/")
def index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")


@app.get("/favicon.ico")
def favicon():
    return ("", 204)


@app.post("/api/login")
def login():
    ip = request.remote_addr or "?"
    with _fail_lock:
        n, until = _fails.get(ip, (0, 0))
        if until > time.time():
            return err("Too many attempts. Wait a minute.", 429)
    raw = request.get_json(silent=True) or {}
    pw = str(raw.get("password", ""))
    name = str(raw.get("username") or raw.get("name") or ACCOUNT_NAME).strip()
    E.ensure_default_account(PASSWORD, ACCOUNT_NAME)
    account = E.verify_account(name, pw)
    if account:
        with _fail_lock:
            _fails.pop(ip, None)
        session.permanent = True
        session["auth"] = True
        session["account_id"] = account["id"]
        session["account_name"] = account["name"]
        return ok(account=account)
    with _fail_lock:
        n += 1
        _fails[ip] = (n, time.time() + 60 if n >= 5 else 0)
    time.sleep(1)
    return err("Wrong password.", 401)


@app.post("/api/logout")
def logout():
    session.clear()
    return ok()


def current_account_id():
    return session.get("account_id")


@app.get("/api/accounts")
def accounts():
    return jsonify({"ok": True, "accounts": E.list_accounts(),
                    "current": current_account_id()})


@app.post("/api/accounts")
def create_account():
    raw = request.get_json(silent=True) or {}
    try:
        account = E.create_account(raw.get("name", ""), raw.get("password", ""))
        return ok(account=account)
    except ValueError as e:
        return err(str(e))


@app.post("/api/accounts/<int:aid>/switch")
def switch_account(aid):
    account = E.get_account(aid)
    if not account:
        return err("Account not found.", 404)
    raw = request.get_json(silent=True) or {}
    if not E.verify_account(account["name"], str(raw.get("password", ""))):
        return err("Wrong account password.", 401)
    session["account_id"] = account["id"]
    session["account_name"] = account["name"]
    return ok(account=account)


@app.get("/api/state")
def state():
    aid = current_account_id()
    general = E.account_kv_get(aid, "general", {}) if aid is not None else {}
    return jsonify({"account": E.get_account(aid),
                    "accounts": E.list_accounts(),
                    "briefing": E.get_briefing_settings(aid),
                    "general": {"recipients": ", ".join(E.get_recipients(aid)) if (E.MAIL_TO or general.get("recipients")) else ""},
                    "presets": E.get_presets(aid), "schedules": E.get_schedules(aid),
                    "status": E.system_status(aid),
                    "uploads_mb": MAX_UPLOAD_MB})


@app.put("/api/briefing/settings")
def put_briefing():
    return ok(settings=E.save_briefing_settings(request.get_json(silent=True) or {}, current_account_id()))


@app.put("/api/general")
def put_general():
    try:
        return ok(general=E.save_general(request.get_json(silent=True) or {}, current_account_id()))
    except ValueError as e:
        return err(str(e))


@app.post("/api/briefing/build")
def build():
    return ok(id=E.enqueue_briefing(account_id=current_account_id()))


@app.get("/api/runs")
def runs():
    return jsonify(E.list_runs(request.args.get("kind"), 60, current_account_id()))


@app.get("/api/runs/<int:rid>")
def one_run(rid):
    r = E.get_run(rid, current_account_id())
    return jsonify(r) if r else err("Not found", 404)


@app.post("/api/runs/<int:rid>/send")
def send_run(rid):
    try:
        r = E.get_run(rid, current_account_id())
        if not r:
            return err("Not found", 404)
        E.send_briefing(rid)
        return ok()
    except Exception as e:
        return err(str(e))


@app.post("/api/task")
def task():
    f = request.form
    urls = [u for u in (f.get("urls", "")).splitlines() if u.strip()]
    files = [(os.path.basename(x.filename or "file"), x.read()) for x in request.files.getlist("files")]
    note = f.get("note", "")
    if not (urls or files or note.strip()):
        return err("Add a link, a file or some text first.")
    try:
        rid = E.enqueue_task(f.get("preset_id", "ask"), note, urls, files, f.get("email") == "1",
                             account_id=current_account_id())
        return ok(id=rid)
    except ValueError as e:
        return err(str(e))


@app.post("/api/presets")
def preset_save():
    try:
        return ok(preset=E.save_preset(request.get_json(silent=True) or {}, current_account_id()))
    except ValueError as e:
        return err(str(e))


@app.delete("/api/presets/<pid>")
def preset_delete(pid):
    E.delete_preset(pid, current_account_id())
    return ok()


@app.post("/api/schedules")
def schedule_save():
    try:
        return ok(schedule=E.save_schedule(request.get_json(silent=True) or {}, current_account_id()))
    except (ValueError, KeyError) as e:
        return err(str(e))


@app.delete("/api/schedules/<sid>")
def schedule_delete(sid):
    E.delete_schedule(sid, current_account_id())
    return ok()


@app.post("/api/email/test")
def email_test():
    try:
        E.send_email("Home AI Hub: test email", "If you can read this, email delivery works.",
                     account_id=current_account_id())
        return ok()
    except Exception as e:
        return err(str(e))


@app.post("/api/pc/wake")
def pc_wake():
    try:
        E.send_magic()
        return ok()
    except Exception as e:
        return err(str(e))


@app.post("/api/pc/off")
def pc_off():
    if E.power_off_pc():
        return ok()
    return err("Could not power off the PC. Check SSH, PC address, and power-off configuration.", 502)


@app.put("/api/pc/pause")
def pc_pause():
    raw = request.get_json(silent=True) or {}
    if "paused" not in raw:
        return err("Missing paused value.")
    return ok(paused=E.set_paused(raw["paused"]))


@app.post("/api/update")
def update_from_github():
    global _update_running
    with _update_lock:
        if _update_running:
            return ok(already_running=True)
        if not os.path.isfile(UPDATE_SCRIPT):
            return err("Update script is unavailable.", 500)
        _update_running = True
        try:
            with open(UPDATE_STATUS, "w", encoding="utf-8") as status:
                status.write("Update started\n")
                status.flush()
            log_file = open(UPDATE_STATUS + ".process", "a", encoding="utf-8")
            proc = subprocess.Popen([UPDATE_SCRIPT], stdout=log_file, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            log_file.close()
            threading.Thread(target=mark_update_finished, args=(proc,), daemon=True).start()
        except Exception as e:
            _update_running = False
            return err(f"Could not start update: {e}", 500)
    return ok(started=True)


@app.get("/api/update/status")
def update_status():
    try:
        with open(UPDATE_STATUS, "r", encoding="utf-8") as f:
            message = f.read().strip()
    except FileNotFoundError:
        message = "No update has been started."
    process_log = ""
    try:
        with open(UPDATE_STATUS + ".process", "r", encoding="utf-8") as f:
            process_log = f.read()[-4000:]
    except FileNotFoundError:
        pass
    progress = 5
    if message.startswith("Update progress:"):
        phase = message.split(":", 2)
        if len(phase) >= 2:
            try:
                progress = max(5, min(95, int(phase[1])))
            except ValueError:
                pass
        message = phase[2].strip() if len(phase) == 3 else message
    elif message.startswith("Update complete"):
        progress = 100
    elif message.startswith(("Update failed", "Update blocked")):
        progress = 100
    return ok(message=message, process_log=process_log, running=_update_running,
              progress=progress)


@app.post("/api/chat")
def chat():
    raw = request.get_json(silent=True) or {}
    try:
        rid = E.enqueue_chat(raw.get("message", ""), current_account_id())
        return ok(id=rid)
    except (RuntimeError, ValueError) as e:
        return err(str(e), 409 if isinstance(e, RuntimeError) else 400)


def main():
    from waitress import serve
    E.init()
    for fn in (E.worker_loop, E.scheduler_loop, E.idle_loop):
        threading.Thread(target=fn, daemon=True).start()
    E.log("hub started on port", os.environ.get("PORT", "8088"))
    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8088")), threads=8)


if __name__ == "__main__":
    main()
