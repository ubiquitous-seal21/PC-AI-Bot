"""Home AI hub engine: storage, PC power, Ollama, sources, news briefing, email, scheduler.

The gaming PC only runs Ollama. This code runs on the always-on server, wakes the
PC with Wake-on-LAN when there is work, and powers it off again when idle.
"""
import base64
import contextlib
import copy
import datetime
import glob
import hashlib
import html
import io
import ipaddress
import json
import os
import queue
import re
import smtplib
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from email.message import EmailMessage
from urllib.parse import urljoin, urlparse

import requests


def env(key, default=""):
    return os.environ.get(key, default)


PC_MAC = env("PC_MAC")
PC_IP = env("PC_IP")
PC_USER = env("PC_SSH_USER")
PC_OFF_CMD = env("PC_OFF_CMD", "sudo systemctl poweroff")
LAN_BROADCAST = env("LAN_BROADCAST", "255.255.255.255")
OLLAMA = env("OLLAMA_URL") or f"http://{PC_IP}:11434"
TEXT_MODEL = env("TEXT_MODEL", "qwen3:8b")
VISION_MODEL = env("VISION_MODEL", "qwen2.5vl:7b")
FRESH_URL = env("FRESHRSS_URL")
FRESH_USER = env("FRESHRSS_USER")
FRESH_PASS = env("FRESHRSS_API_PASSWORD")
SMTP_HOST = env("SMTP_HOST")
SMTP_PORT = int(env("SMTP_PORT", "587"))
SMTP_USER = env("SMTP_USER")
SMTP_PASSWORD = env("SMTP_PASSWORD")
SMTP_SECURITY = env("SMTP_SECURITY", "starttls")  # starttls | ssl | none
MAIL_FROM = env("MAIL_FROM") or SMTP_USER
MAIL_TO = env("MAIL_TO")
IDLE_OFF_MIN = int(env("IDLE_OFF_MIN", "5"))  # 0 = never power the PC off
WHISPER_MODEL = env("WHISPER_MODEL", "base")
SUB_LANG = env("SUB_LANG", "en")
FRAMES = int(env("FRAMES", "8"))
MAX_VIDEO_MIN = int(env("MAX_VIDEO_MIN", "30"))
DATA = env("DATA_DIR", "/data")
SSH_KEY = env("SSH_KEY", "/ssh/id_ed25519")


def log(*a):
    print(datetime.datetime.now().strftime("%H:%M:%S"), *a, flush=True)


# ---------- storage (SQLite) ----------
os.makedirs(DATA, exist_ok=True)
_db = sqlite3.connect(os.path.join(DATA, "hub.db"), check_same_thread=False)
_db.row_factory = sqlite3.Row
_lock = threading.RLock()
_cfg_lock = threading.Lock()
_db.executescript("""
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, title TEXT, created REAL,
  status TEXT, detail TEXT DEFAULT '', output TEXT DEFAULT '', extra TEXT DEFAULT '',
  emailed INTEGER DEFAULT 0, auto INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS scores(item_id TEXT, fhash TEXT, data TEXT, PRIMARY KEY(item_id, fhash));
""")


def sql(query, args=()):
    with _lock:
        cur = _db.execute(query, args)
        _db.commit()
        return [dict(r) for r in cur.fetchall()] if cur.description else []


def kv_get(key, default=None):
    rows = sql("SELECT value FROM kv WHERE key=?", (key,))
    return json.loads(rows[0]["value"]) if rows else default


def kv_set(key, value):
    sql("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)))


def new_run(kind, title, auto=0):
    with _lock:
        cur = _db.execute("INSERT INTO runs(kind,title,created,status,auto) VALUES(?,?,?,?,?)",
                          (kind, title, time.time(), "queued", auto))
        _db.commit()
        return cur.lastrowid


def update_run(rid, **fields):
    if fields:
        sql("UPDATE runs SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?",
            (*fields.values(), rid))


def get_run(rid):
    rows = sql("SELECT * FROM runs WHERE id=?", (rid,))
    return rows[0] if rows else None


def list_runs(kind=None, limit=60):
    cols = "id,kind,title,created,status,detail,emailed"
    if kind:
        return sql(f"SELECT {cols} FROM runs WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit))
    return sql(f"SELECT {cols} FROM runs ORDER BY id DESC LIMIT ?", (limit,))


# ---------- settings, presets, schedules ----------
EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

DEFAULT_BRIEFING = {
    "enabled": True, "deliver_time": "07:00", "prep_minutes": 45, "lookback_hours": 24,
    "max_items": 200, "top_n": 10, "min_importance": 5, "include_read": False,
    "focuses": [
        {"name": "World", "description": "international affairs, politics, conflicts, the economy", "count": 4},
        {"name": "Technology and AI", "description": "AI, software, hardware, big tech, cybersecurity", "count": 4},
        {"name": "Science and health", "description": "research results, space, medicine, climate", "count": 3},
    ],
}


def num(v, lo, hi, default):
    try:
        v = int(float(v))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def clean_time(v, default):
    v = str(v or "")
    return v if TIME_RE.match(v) else default


def clean_briefing(raw):
    d = DEFAULT_BRIEFING
    s = {
        "enabled": bool(raw.get("enabled", d["enabled"])),
        "deliver_time": clean_time(raw.get("deliver_time"), d["deliver_time"]),
        "prep_minutes": num(raw.get("prep_minutes"), 10, 240, d["prep_minutes"]),
        "lookback_hours": num(raw.get("lookback_hours"), 1, 72, d["lookback_hours"]),
        "max_items": num(raw.get("max_items"), 20, 500, d["max_items"]),
        "top_n": num(raw.get("top_n"), 1, 30, d["top_n"]),
        "min_importance": num(raw.get("min_importance"), 0, 10, d["min_importance"]),
        "include_read": bool(raw.get("include_read", d["include_read"])),
    }
    focuses, seen = [], set()
    for f in (raw.get("focuses", d["focuses"]) or [])[:12]:
        name = str(f.get("name", "")).strip()[:60]
        if name and name.lower() not in seen:
            seen.add(name.lower())
            focuses.append({"name": name, "description": str(f.get("description", "")).strip()[:300],
                            "count": num(f.get("count"), 1, 20, 4)})
    s["focuses"] = focuses
    return s


def get_briefing_settings():
    return clean_briefing(kv_get("briefing", {}) or {})


def save_briefing_settings(raw):
    s = clean_briefing(raw)
    kv_set("briefing", s)
    return s


def parse_emails(text):
    parts = [p for p in re.split(r"[,\s;]+", text or "") if p]
    bad = [p for p in parts if not EMAIL_RE.match(p)]
    if bad:
        raise ValueError("Not a valid email address: " + bad[0])
    return parts


def get_recipients():
    mine = parse_emails((kv_get("general", {}) or {}).get("recipients", ""))
    return mine or parse_emails(MAIL_TO)


def save_general(raw):
    emails = parse_emails(raw.get("recipients", ""))
    kv_set("general", {"recipients": ", ".join(emails)})
    return {"recipients": ", ".join(emails)}


RECIPE_INSTRUCTION = """Turn the sources into a clear, usable recipe.
Write plain text with no markdown symbols like ** or #, using this layout:
TITLE
Servings and time (estimate if not stated)
INGREDIENTS: one per line with a quantity. If a quantity was not stated, estimate it and mark it (~).
STEPS: numbered and concise.
NOTES: substitutions, and anything unclear in the sources.
If an attached image shows recipe text, transcribe it. If it only shows a dish, identify it, write the most likely recipe and say the quantities are estimates.
Use only information present in the sources. Do not invent steps."""

BUILTIN_PRESETS = [
    {"id": "recipe", "name": "Recipe from a link, video or photo", "vision": True,
     "instruction": RECIPE_INSTRUCTION},
    {"id": "summary", "name": "Summarize", "vision": False,
     "instruction": "Summarize the sources for a busy reader: a 2-sentence overview, then 5 bullet "
                    "key points, then anything the reader should act on. Say if something is unclear or missing."},
    {"id": "image", "name": "Explain an image or screenshot", "vision": True,
     "instruction": "Describe what the attached image shows and explain anything in it that is "
                    "technical, unusual or important. Transcribe any text that matters."},
    {"id": "mealplan", "name": "Meal plan and shopping list", "vision": False,
     "instruction": "Make a 5-day dinner plan from the reader's notes (ingredients on hand, diet, time "
                    "limits). Then list what to buy, grouped by supermarket aisle, skipping what they "
                    "already have. Keep each dinner to one line."},
    {"id": "ask", "name": "Ask anything", "vision": False,
     "instruction": "Answer the request directly, using the sources if any are provided. "
                    "Say so when you are unsure instead of guessing."},
]


def get_presets():
    with _cfg_lock:
        p = kv_get("presets")
        if p is None:
            p = copy.deepcopy(BUILTIN_PRESETS)
            kv_set("presets", p)
        return p


def save_preset(raw):
    name = str(raw.get("name", "")).strip()[:80]
    instruction = str(raw.get("instruction", "")).strip()[:4000]
    if not name or not instruction:
        raise ValueError("A preset needs a name and instructions.")
    with _cfg_lock:
        presets = kv_get("presets") or copy.deepcopy(BUILTIN_PRESETS)
        pid = str(raw.get("id") or uuid.uuid4().hex[:8])
        item = {"id": pid, "name": name, "instruction": instruction, "vision": bool(raw.get("vision"))}
        for i, p in enumerate(presets):
            if p["id"] == pid:
                presets[i] = item
                break
        else:
            presets.append(item)
        kv_set("presets", presets)
    return item


def delete_preset(pid):
    with _cfg_lock:
        kv_set("presets", [p for p in (kv_get("presets") or []) if p["id"] != pid])


def get_schedules():
    return kv_get("schedules", []) or []


def save_schedule(raw):
    if not any(p["id"] == raw.get("preset_id") for p in get_presets()):
        raise ValueError("Pick a preset for this scheduled task.")
    days = sorted({d for d in (raw.get("days") or []) if isinstance(d, int) and 0 <= d <= 6})
    item = {
        "id": str(raw.get("id") or uuid.uuid4().hex[:8]),
        "name": str(raw.get("name", "")).strip()[:80] or "Scheduled task",
        "preset_id": raw["preset_id"], "note": str(raw.get("note", ""))[:4000],
        "urls": [u.strip() for u in (raw.get("urls") or []) if str(u).strip()][:5],
        "days": days or list(range(7)), "time": clean_time(raw.get("time"), "08:00"),
        "enabled": bool(raw.get("enabled", True)), "last_run": "",
    }
    with _cfg_lock:
        items = get_schedules()
        for i, s in enumerate(items):
            if s["id"] == item["id"]:
                item["last_run"] = s.get("last_run", "")
                items[i] = item
                break
        else:
            items.append(item)
        kv_set("schedules", items)
    return item


def delete_schedule(sid):
    with _cfg_lock:
        kv_set("schedules", [s for s in get_schedules() if s["id"] != sid])


# ---------- small helpers ----------
def strip_html(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def srt_to_text(srt):
    out, prev = [], ""
    for line in srt.splitlines():
        line = line.strip()
        if not line or line.isdigit() or "-->" in line:
            continue
        line = re.sub(r"<[^>]+>", "", line).strip()
        if line and line != prev:
            out.append(line)
            prev = line
    return " ".join(out)


def run(cmd, timeout):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ---------- PC power ----------
state = {"last": time.time(), "busy": 0, "worker_busy": False,
         "paused": False, "we_woke": False}
jobs = queue.Queue()


def ollama_up():
    try:
        return requests.get(OLLAMA, timeout=3).ok
    except requests.RequestException:
        return False


def magic_packet():
    mac = bytes.fromhex(re.sub(r"[^0-9a-fA-F]", "", PC_MAC))
    if len(mac) != 6:
        raise ValueError("PC_MAC must be 6 bytes, e.g. AA:BB:CC:DD:EE:FF")
    return b"\xff" * 6 + mac * 16


def send_magic():
    pkt = magic_packet()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    for addr in {LAN_BROADCAST, "255.255.255.255"}:
        s.sendto(pkt, (addr, 9))
    s.close()


def ensure_pc(timeout=300):
    if ollama_up():
        return
    log("waking PC")
    deadline, last_send = time.time() + timeout, 0
    while time.time() < deadline:
        if time.time() - last_send > 30:
            send_magic()
            last_send = time.time()
        time.sleep(5)
        if ollama_up():
            with _lock:
                state["we_woke"] = True
            time.sleep(3)
            log("PC is up")
            return
    raise RuntimeError("The PC did not come up (Wake-on-LAN or Ollama problem)")


@contextlib.contextmanager
def pc_job(prog=None):
    with _lock:
        state["busy"] += 1
    try:
        if prog:
            prog("Waking the PC" if not ollama_up() else "Thinking")
        ensure_pc()
        if prog:
            prog("Thinking")
        yield
    finally:
        with _lock:
            state["busy"] -= 1
            state["last"] = time.time()


def power_off_pc():
    if not PC_USER:
        return False
    cmd = ["ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
           "-o", "StrictHostKeyChecking=accept-new",
           "-o", f"UserKnownHostsFile={DATA}/known_hosts",
           f"{PC_USER}@{PC_IP}", PC_OFF_CMD]
    try:
        p = run(cmd, 30)
        log("power-off sent, exit code", p.returncode, p.stderr.strip()[:200])
        if p.returncode != 0:
            return False
        return True
    except Exception as e:
        log("power-off failed:", e)
        return False


def pc_has_active_sessions():
    """Fail closed: if session state cannot be verified, do not auto-power-off."""
    if not PC_USER:
        return True
    cmd = ["ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
           "-o", "StrictHostKeyChecking=accept-new",
           "-o", f"UserKnownHostsFile={DATA}/known_hosts",
           f"{PC_USER}@{PC_IP}",
           "session_output=$(loginctl list-sessions --no-legend 2>/dev/null); "
           "if [ -z \"$session_output\" ]; then exit 0; fi; "
           "printf '%s\\n' \"$session_output\" | awk '{print $1}' | "
           "while read -r sid; do loginctl show-session \"$sid\" "
           "--no-pager -p State -p Active -p IdleHint -p Remote 2>/dev/null; done"]
    try:
        p = run(cmd, 30)
        if p.returncode != 0:
            log("active-session check failed:", p.stderr.strip()[:200])
            return True
        for line in p.stdout.splitlines():
            if line.strip() in ("Active=yes", "State=active", "IdleHint=no"):
                return True
        return False
    except Exception as e:
        log("active-session check failed:", repr(e))
        return True


def idle_loop():
    while True:
        time.sleep(30)
        if IDLE_OFF_MIN <= 0:
            continue
        with _lock:
            idle = (state["busy"] == 0 and not state["worker_busy"]
                    and time.time() - state["last"] > IDLE_OFF_MIN * 60)
            woke = state["we_woke"]
        if idle and woke and not pc_has_active_sessions():
            if power_off_pc():
                with _lock:
                    state["we_woke"] = False


# ---------- Ollama ----------
def chat(prompt, model, images=None, schema=None, num_ctx=8192):
    if model.startswith("qwen3") and "vl" not in model and schema:
        prompt = "/no_think\n" + prompt
    msg = {"role": "user", "content": prompt}
    if images:
        msg["images"] = images
    payload = {"model": model, "messages": [msg], "stream": False,
               "options": {"num_ctx": num_ctx, "temperature": 0.3}}
    if schema:
        payload["format"] = schema
    r = requests.post(f"{OLLAMA}/api/chat", json=payload, timeout=1800)
    r.raise_for_status()
    out = r.json()["message"]["content"]
    return re.sub(r"(?s)<think>.*?</think>", "", out).strip()


# ---------- sources: links, videos, files ----------
URL_RE = re.compile(r"https?://[^\s<>\"')]+")


def validate_public_url(url):
    """Require HTTP(S) and reject DNS names that resolve into private networks."""
    parsed = urlparse(str(url).strip())
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise RuntimeError("Only public HTTP and HTTPS links are allowed.")
    if parsed.username or parsed.password:
        raise RuntimeError("Links containing credentials are not allowed.")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise RuntimeError("Local and loopback links are not allowed.")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
                                                            type=socket.SOCK_STREAM)}
    except socket.gaierror as e:
        raise RuntimeError(f"Could not resolve link host: {hostname}") from e
    if not addresses:
        raise RuntimeError("Could not resolve link host.")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global:
            raise RuntimeError("Links to private, loopback, link-local, or reserved networks are not allowed.")
    return str(parsed.geturl())


def public_http_get(url, timeout=30):
    current = validate_public_url(url)
    for _ in range(5):
        r = requests.get(current, timeout=timeout, allow_redirects=False,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code in (301, 302, 303, 307, 308):
            location = r.headers.get("Location")
            if not location:
                r.raise_for_status()
            current = validate_public_url(urljoin(current, location))
            continue
        r.raise_for_status()
        return r
    raise RuntimeError("The link redirected too many times.")


def video_info(url):
    url = validate_public_url(url)
    try:
        p = run(["yt-dlp", "--skip-download", "--no-playlist", "--dump-single-json", url], 90)
    except Exception:
        return None
    if p.returncode != 0 or not p.stdout.strip():
        return None
    try:
        return json.loads(p.stdout)
    except ValueError:
        return None


def get_captions(url, d):
    url = validate_public_url(url)
    run(["yt-dlp", "--skip-download", "--no-playlist", "--write-subs", "--write-auto-subs",
         "--sub-langs", f"{SUB_LANG}.*,{SUB_LANG}", "--convert-subs", "srt",
         "-o", f"{d}/sub", url], 180)
    files = sorted(glob.glob(f"{d}/sub*.srt"))
    return srt_to_text(open(files[0], encoding="utf-8", errors="ignore").read()) if files else ""


_whisper = None


def transcribe(url, d):
    global _whisper
    url = validate_public_url(url)
    run(["yt-dlp", "--no-playlist", "-f", "ba/b", "-x", "--audio-format", "mp3",
         "-o", f"{d}/audio.%(ext)s", url], 900)
    audio = glob.glob(f"{d}/audio.*")
    if not audio:
        return ""
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8",
                                download_root=os.path.join(DATA, "whisper"))
    segs, _ = _whisper.transcribe(audio[0], vad_filter=True)
    return " ".join(s.text.strip() for s in segs)


def get_frames(url, d, duration):
    url = validate_public_url(url)
    run(["yt-dlp", "--no-playlist", "-f", "wv*[height>=360]/wv*/w", "-o", f"{d}/video.%(ext)s", url], 900)
    vids = [f for f in glob.glob(f"{d}/video.*") if not f.endswith(".part")]
    if not vids:
        return []
    if not duration:  # some sites don't report it; ask ffprobe
        q = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", vids[0]], 30)
        try:
            duration = float(q.stdout.strip())
        except ValueError:
            return []
    duration = min(duration, MAX_VIDEO_MIN * 60)
    out = []
    for i in range(FRAMES):
        t = duration * (i + 0.5) / FRAMES
        path = f"{d}/frame{i}.jpg"
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.1f}", "-i", vids[0],
             "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4", path], 60)
        if os.path.exists(path):
            out.append(base64.b64encode(open(path, "rb").read()).decode())
    return out


def source_from_video(url, info, prog):
    duration = info.get("duration") or 0
    with tempfile.TemporaryDirectory() as d:
        prog("Reading the video's captions")
        text = get_captions(url, d)
        if not text and duration <= MAX_VIDEO_MIN * 60:
            prog("No captions: transcribing the audio (slow)")
            try:
                text = transcribe(url, d)
            except Exception as e:  # keep going with the description and frames
                log("transcription failed:", repr(e))
                text = ""
        prog("Grabbing frames from the video")
        frames = get_frames(url, d, duration)
    body = (f"Title: {info.get('title', '')}\nDescription: {(info.get('description') or '')[:1500]}\n"
            f"Transcript: {text[:16000] or '(none available)'}")
    return {"label": f"Video {url}", "text": body, "images": frames}


def source_from_article(url):
    import trafilatura
    r = public_http_get(url, timeout=30)
    text = trafilatura.extract(r.text, include_comments=False) or strip_html(r.text)
    if len(text) < 200:
        raise RuntimeError(f"I couldn't read any text from {url}")
    return {"label": f"Web page {url}", "text": text[:16000], "images": []}


def source_from_url(url, prog):
    url = validate_public_url(url)
    info = video_info(url)
    return source_from_video(url, info, prog) if info else source_from_article(url)


def source_from_file(name, data):
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if ext in ("png", "jpg", "jpeg", "webp", "gif"):
        return {"label": f"Image {name}", "text": "(see the attached image)",
                "images": [base64.b64encode(data).decode()]}
    if ext == "pdf":
        from pypdf import PdfReader
        pages = PdfReader(io.BytesIO(data)).pages[:40]
        text = "\n".join((p.extract_text() or "") for p in pages)
    else:
        text = data.decode("utf-8", errors="ignore")
    return {"label": f"File {name}", "text": text[:16000] or "(no readable text)", "images": []}


def build_prompt(preset, note, sources):
    parts = [preset["instruction"].strip()]
    if note.strip():
        parts.append("The reader's request or notes:\n" + note.strip())
    for i, s in enumerate(sources, 1):
        parts.append(f"[Source {i}] {s['label']}\n{s['text']}")
    if any(s["images"] for s in sources):
        parts.append("The attached images belong to the sources above, in order.")
    return "\n\n".join(parts)


# ---------- email ----------
def email_configured():
    return bool(SMTP_HOST and MAIL_FROM)


def send_email(subject, text, html_body=None, to=None):
    to = to or get_recipients()
    if not to:
        raise RuntimeError("No recipient set. Add one on the Customize tab.")
    if not email_configured():
        raise RuntimeError("Email is not set up. Fill in the SMTP lines in .env and restart.")
    msg = EmailMessage()
    msg["Subject"] = subject.replace("\n", " ")[:200]
    msg["From"] = MAIL_FROM
    msg["To"] = ", ".join(to)
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    srv = (smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) if SMTP_SECURITY == "ssl"
           else smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30))
    with srv:
        if SMTP_SECURITY == "starttls":
            srv.starttls()
        if SMTP_USER:
            srv.login(SMTP_USER, SMTP_PASSWORD)
        srv.send_message(msg)


def text_as_html(text):
    return ('<div style="font-family:system-ui,sans-serif;max-width:680px"><pre style="white-space:pre-wrap;'
            'font:inherit">' + html.escape(text) + "</pre></div>")


# ---------- tasks (recipes, summaries, anything) ----------
def run_task(rid, preset, note, urls, files, email):
    def prog(t):
        update_run(rid, status="running", detail=t)

    try:
        with pc_job(prog):
            prog("Collecting sources")
            all_urls = list(dict.fromkeys([u.strip() for u in urls if u.strip()] + URL_RE.findall(note)))[:5]
            sources = []
            for u in all_urls:
                prog(f"Reading {u[:70]}")
                sources.append(source_from_url(u, prog))
            for name, data in files:
                sources.append(source_from_file(name, data))
            images = [im for s in sources for im in s["images"]][:12]
            prompt = build_prompt(preset, note, sources)
            model = VISION_MODEL if images or preset.get("vision") else TEXT_MODEL
            out = chat(prompt, model, images or None, num_ctx=16384)
        update_run(rid, status="done", detail="", output=out)
        if email:
            try:
                send_email(get_run(rid)["title"], out, text_as_html(out))
                update_run(rid, emailed=1, detail="Emailed")
            except Exception as e:
                update_run(rid, detail=f"Email failed: {e}")
    except Exception as e:
        log("task failed:", repr(e))
        update_run(rid, status="error", detail=str(e)[:500])


def run_chat(rid, note):
    def prog(t):
        update_run(rid, status="running", detail=t)
    try:
        prog("Waiting for the PC")
        with pc_job(prog):
            out = chat(note.strip(), TEXT_MODEL, num_ctx=16384)
        update_run(rid, status="done", detail="", output=out)
    except Exception as e:
        log("chat failed:", repr(e))
        update_run(rid, status="error", detail=str(e)[:500])


def enqueue_task(preset_id, note, urls, files, email, title=None):
    preset = next((p for p in get_presets() if p["id"] == preset_id), None)
    if not preset:
        raise ValueError("Unknown preset")
    first_line = note.strip().split("\n")[0][:50] if note.strip() else ""
    rid = new_run("task", title or (preset["name"] + (": " + first_line if first_line else "")))
    jobs.put(("task", rid, preset, note, urls, files, email))
    return rid


def enqueue_chat(note):
    note = str(note or "").strip()[:4000]
    if not note:
        raise ValueError("Enter a message.")
    with _lock:
        if state["worker_busy"] or state["paused"] or jobs.qsize():
            raise RuntimeError("The chatbot is unavailable while background work is active or paused.")
    rid = new_run("chat", "Chat")
    jobs.put(("chat", rid, note))
    return rid


# ---------- news briefing ----------
SCORE_SCHEMA = {"type": "object",
                "properties": {"importance": {"type": "integer"},
                               "topics": {"type": "array", "items": {"type": "string"}},
                               "summary": {"type": "string"}},
                "required": ["importance", "topics", "summary"]}
PROMPT_VERSION = "v1"
STOP = set("the and for with from that this are was were has have had its his her their says say said "
           "after over into about new will can could would more than not you your what how why who".split())


def fresh_items(s):
    if not (FRESH_URL and FRESH_USER and FRESH_PASS):
        raise RuntimeError("FreshRSS settings are missing in .env")
    base = FRESH_URL.rstrip("/") + "/api/greader.php"
    r = requests.post(base + "/accounts/ClientLogin",
                      data={"Email": FRESH_USER, "Passwd": FRESH_PASS}, timeout=30)
    r.raise_for_status()
    m = re.search(r"Auth=(\S+)", r.text)
    if not m:
        raise RuntimeError("FreshRSS login failed. Check the API password.")
    params = {"n": s["max_items"], "output": "json",
              "ot": int(time.time() - s["lookback_hours"] * 3600)}
    if not s["include_read"]:
        params["xt"] = "user/-/state/com.google/read"
    r = requests.get(base + "/reader/api/0/stream/contents/reading-list", params=params,
                     headers={"Authorization": f"GoogleLogin auth={m.group(1)}"}, timeout=90)
    r.raise_for_status()
    items = []
    for it in r.json().get("items", []):
        link = ((it.get("canonical") or it.get("alternate") or [{}])[0]).get("href", "")
        label = next((c.rsplit("/", 1)[-1] for c in it.get("categories", []) if "/label/" in c), "")
        body = (it.get("summary") or it.get("content") or {}).get("content", "")
        items.append({"id": it["id"], "title": (it.get("title") or "").strip(), "url": link,
                      "source": (it.get("origin") or {}).get("title", ""), "label": label,
                      "text": strip_html(body)[:3000]})
    return items


def title_tokens(title):
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 2 and w not in STOP}


def same_story(a, b):
    """Headlines about one event share several distinctive words, whatever else they add."""
    common = len(a & b)
    small = min(len(a), len(b))
    return small > 0 and ((common >= 3 and common / small >= 0.5) or (common >= 2 and common / small >= 0.8))


def cluster(items):
    """Group items that report the same story. More independent sources = bigger story."""
    groups = []
    for it in items:
        t = title_tokens(it["title"])
        for g in groups:
            if same_story(t, g["tokens"]):
                g["members"].append(it)
                break
        else:
            groups.append({"tokens": t, "members": [it]})
    out = []
    for g in groups:
        rep = max(g["members"], key=lambda m: len(m["text"]))
        out.append({"rep": rep, "coverage": len({m["source"] or m["id"] for m in g["members"]})})
    return out


def focus_hash(focuses):
    return hashlib.sha1((PROMPT_VERSION + json.dumps(focuses, sort_keys=True)).encode()).hexdigest()[:12]


def score_story(rep, focuses, fh):
    cached = sql("SELECT data FROM scores WHERE item_id=? AND fhash=?", (rep["id"], fh))
    if cached:
        return json.loads(cached[0]["data"])
    focus_lines = "\n".join(f"- {f['name']}: {f['description']}" for f in focuses) or "(none)"
    prompt = (
        "You are the news editor for one reader. Judge this story.\n"
        "importance, 0 to 10: 10 = a major event with wide, lasting impact that any informed person should "
        "know today; 7 = a significant development in its field; 4 = minor or routine; 0 to 2 = clickbait, "
        "ads, listicles, opinion fluff, gossip. New developments beat commentary on old news.\n\n"
        f"Title: {rep['title']}\nSource: {rep['source']}"
        + (f" (feed category: {rep['label']})" if rep["label"] else "")
        + f"\nText: {rep['text'][:1500]}\n\nFocus areas (use only these names):\n{focus_lines}\n\n"
        "Return JSON. importance: integer. topics: the focus area names this story clearly belongs to "
        "(may be empty). summary: at most 2 plain sentences saying what happened, no hype.")
    d = json.loads(chat(prompt, TEXT_MODEL, schema=SCORE_SCHEMA))
    wanted = {str(t).lower() for t in d.get("topics", [])}
    res = {"importance": num(d["importance"], 0, 10, 0),
           "topics": [f["name"] for f in focuses if f["name"].lower() in wanted],
           "summary": str(d["summary"]).strip()}
    sql("INSERT OR REPLACE INTO scores VALUES(?,?,?)", (rep["id"], fh, json.dumps(res)))
    return res


def assemble(scored, s):
    """scored: dicts with rep, coverage, importance, topics, summary, score. Returns [(heading, [items])]."""
    ranked = sorted(scored, key=lambda x: -x["score"])
    top = [x for x in ranked if x["score"] >= s["min_importance"]][:s["top_n"]]
    used = {x["rep"]["id"] for x in top}
    sections = [("Top stories", top)]
    floor = max(s["min_importance"] - 2, 1)
    for f in s["focuses"]:
        pool = [x for x in ranked if f["name"] in x["topics"] and x["rep"]["id"] not in used and x["score"] >= floor]
        pick = pool[:f["count"]]
        used.update(x["rep"]["id"] for x in pick)
        sections.append((f["name"], pick))
    return sections


def render_briefing(sections, n_articles, n_stories, when):
    esc = html.escape
    h = ['<div style="font-family:system-ui,-apple-system,sans-serif;max-width:680px;margin:0 auto;'
         'color:#1a1a1a;line-height:1.45">',
         f'<h1 style="font-size:22px;margin:0 0 4px">Your briefing</h1>'
         f'<div style="color:#666;font-size:13px;margin-bottom:18px">{esc(when)} · read {n_articles} '
         f'articles, {n_stories} distinct stories</div>']
    t = [f"Your briefing, {when}", f"Read {n_articles} articles, {n_stories} distinct stories.", ""]
    for heading, items in sections:
        if not items:
            continue
        h.append(f'<h2 style="font-size:17px;margin:22px 0 8px;border-bottom:1px solid #ddd;'
                 f'padding-bottom:4px">{esc(heading)}</h2>')
        t += [heading.upper(), ""]
        for x in items:
            r = x["rep"]
            meta = " · ".join(p for p in [r["source"], f"{x['score']:.1f}/10",
                                          f"{x['coverage']} sources" if x["coverage"] > 1 else ""] if p)
            title = esc(r["title"] or "(no title)")
            head = (f'<a href="{esc(r["url"])}" style="color:#0b57d0;text-decoration:none;font-weight:600">'
                    f'{title}</a>') if r["url"] else f"<b>{title}</b>"
            h.append(f'<p style="margin:0 0 14px">{head}<br><span style="color:#666;font-size:12px">'
                     f'{esc(meta)}</span><br>{esc(x["summary"])}</p>')
            t += [f"{r['title']} ({meta})", x["summary"], r["url"], ""]
    if not any(items for _, items in sections):
        h.append("<p>Nothing met your importance threshold today.</p>")
        t.append("Nothing met your importance threshold today.")
    h.append("</div>")
    return "".join(h), "\n".join(t)


def build_briefing(rid):
    with _lock:
        state["busy"] += 1
    try:
        s = get_briefing_settings()
        update_run(rid, status="running", detail="Fetching articles from FreshRSS")
        items = fresh_items(s)
        when = datetime.datetime.now().strftime("%A %d %B %Y")
        if not items:
            html_out, text_out = render_briefing([("Top stories", [])], 0, 0, when)
            update_run(rid, status="ready", detail="No articles in the window", output=html_out, extra=text_out)
            return
        groups = cluster(items)
        fh = focus_hash(s["focuses"])
        scored = []
        with pc_job(lambda t: update_run(rid, status="running", detail=t)):
            for n, g in enumerate(groups, 1):
                update_run(rid, detail=f"Scoring story {n} of {len(groups)}")
                try:
                    res = score_story(g["rep"], s["focuses"], fh)
                except Exception as e:
                    log("skip story:", e)
                    continue
                score = min(10.0, res["importance"] + 0.5 * min(g["coverage"] - 1, 3))
                scored.append({**g, **res, "score": score})
        sql("DELETE FROM scores WHERE rowid NOT IN (SELECT rowid FROM scores ORDER BY rowid DESC LIMIT 5000)")
        sections = assemble(scored, s)
        shown = sum(len(i) for _, i in sections)
        html_out, text_out = render_briefing(sections, len(items), len(groups), when)
        update_run(rid, status="ready", output=html_out, extra=text_out,
                   detail=f"{shown} stories chosen from {len(items)} articles")
    except Exception as e:
        log("briefing failed:", repr(e))
        update_run(rid, status="error", detail=str(e)[:500])
    finally:
        with _lock:
            state["busy"] -= 1
            state["last"] = time.time()


def send_briefing(rid):
    r = get_run(rid)
    if not r or r["status"] not in ("ready", "sent"):
        raise RuntimeError("That briefing is not ready yet.")
    send_email(r["title"], r["extra"] or strip_html(r["output"]), r["output"])
    update_run(rid, status="sent", emailed=1)


def enqueue_briefing(auto=0):
    rid = new_run("briefing", "Briefing " + datetime.date.today().strftime("%a %d %b"), auto=auto)
    jobs.put(("build", rid))
    return rid


# ---------- scheduler ----------
def tick(now=None):
    """Called every 30 seconds: starts the morning build, sends it on time, runs scheduled tasks."""
    now = now or datetime.datetime.now()
    today = now.date().isoformat()
    grace = datetime.timedelta(hours=3)
    s = get_briefing_settings()
    if s["enabled"]:
        hh, mm = map(int, s["deliver_time"].split(":"))
        deliver = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        start = deliver - datetime.timedelta(minutes=s["prep_minutes"])
        if start <= now <= deliver + grace and kv_get("last_build") != today:
            kv_set("last_build", today)
            enqueue_briefing(auto=1)
        if now >= deliver:
            for r in sql("SELECT id FROM runs WHERE kind='briefing' AND status='ready' AND auto=1 AND emailed=0"):
                try:
                    send_briefing(r["id"])
                except Exception as e:
                    update_run(r["id"], auto=0, detail=f"Email failed: {e}")
    for sc in get_schedules():
        if not sc.get("enabled", True):
            continue
        hh, mm = map(int, sc["time"].split(":"))
        due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now.weekday() in sc["days"] and due <= now <= due + grace and sc.get("last_run") != today:
            with _cfg_lock:
                items = get_schedules()
                for i in items:
                    if i["id"] == sc["id"]:
                        i["last_run"] = today
                kv_set("schedules", items)
            try:
                enqueue_task(sc["preset_id"], sc.get("note", ""), sc.get("urls", []), [], True, title=sc["name"])
            except Exception as e:
                log("schedule failed:", e)


def worker_loop():
    while True:
        job = jobs.get()
        try:
            with _lock:
                state["worker_busy"] = True
            if state["paused"]:
                jobs.put(job)
                with _lock:
                    state["worker_busy"] = False
                time.sleep(1)
                continue
            if job[0] == "build":
                build_briefing(job[1])
            elif job[0] == "task":
                run_task(*job[1:])
            elif job[0] == "chat":
                run_chat(*job[1:])
        except Exception as e:
            log("worker error:", repr(e))
        finally:
            with _lock:
                state["worker_busy"] = False


def scheduler_loop():
    while True:
        try:
            tick()
        except Exception as e:
            log("scheduler error:", repr(e))
        time.sleep(30)


def init():
    sql("UPDATE runs SET status='error', detail='Interrupted by a restart' WHERE status IN ('queued','running')")
    get_presets()
    set_paused(bool(kv_get("paused", False)))


def system_status():
    with _lock:
        busy = state["busy"]
        worker_busy = state["worker_busy"]
        paused = state["paused"]
        queued = jobs.qsize()
    return {"pc_up": ollama_up(), "email_ready": email_configured(), "recipients": get_recipients() if MAIL_TO or (kv_get("general") or {}).get("recipients") else [],
            "freshrss_ready": bool(FRESH_URL and FRESH_USER and FRESH_PASS),
            "text_model": TEXT_MODEL, "vision_model": VISION_MODEL, "pc_ip": PC_IP,
            "busy": busy, "worker_busy": worker_busy, "paused": paused, "queued": queued,
            "active_sessions": pc_has_active_sessions()}


def set_paused(paused):
    paused = bool(paused)
    with _lock:
        state["paused"] = paused
    kv_set("paused", paused)
    return state["paused"]
