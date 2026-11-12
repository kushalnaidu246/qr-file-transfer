"""FileDrop server: phone web page + desktop UI backend."""
import json
import secrets
import os
import shutil
import socket
import sys
import tempfile
import time
import threading
import urllib.parse
import zipfile
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import qrcode

HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
STATE = {"chat": [], "pin": "", "fails": 0, "token": "", "inbox": "", "outbox": "", "url": "", "seen": {}}


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def safe_name(name):
    name = os.path.basename(urllib.parse.unquote(name)).strip()
    return name if name and name not in (".", "..") else None


def unique_path(folder, name):
    base, ext = os.path.splitext(name)
    path, n = os.path.join(folder, name), 1
    while os.path.exists(path) or os.path.exists(path + ".part"):
        path = os.path.join(folder, f"{base} ({n}){ext}")
        n += 1
    return path


def list_dir(folder):
    items = []
    for n in os.listdir(folder):
        p = os.path.join(folder, n)
        if os.path.isfile(p) and not n.startswith(".") and not n.endswith(".part"):
            st = os.stat(p)
            items.append({"name": n, "size": st.st_size, "mtime": st.st_mtime})
    items.sort(key=lambda i: -i["mtime"])
    return items


def list_tree(root):
    """Top-level files plus one level of named folders (each with its files)."""
    folders = []
    for n in os.listdir(root):
        p = os.path.join(root, n)
        if os.path.isdir(p) and not n.startswith("."):
            files = list_dir(p)
            if files:
                folders.append({"name": n, "files": files, "size": sum(f["size"] for f in files),
                                "mtime": max(f["mtime"] for f in files), "busy": is_busy(p)})
    folders.sort(key=lambda i: -i["mtime"])
    return {"files": list_dir(root), "folders": folders}


def is_busy(folder):
    """True while a file is still being written into folder (upload or copy in progress)."""
    return any(n.endswith(".part") for n in os.listdir(folder))


def copy_in(src, folder):
    """Copy src into folder without exposing a half-written file: write .part, then rename."""
    dest = unique_path(folder, os.path.basename(src))
    tmp = dest + ".part"
    try:
        shutil.copyfile(src, tmp)
        shutil.copymode(src, tmp)
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return dest


def target_dir(root, folder):
    """Subfolder of root for an optional folder name (created on demand)."""
    name = safe_name(folder) if folder else None
    if not name:
        return root
    p = os.path.join(root, name)
    os.makedirs(p, exist_ok=True)
    return p


def new_pin():
    STATE["pin"] = f"{secrets.randbelow(10**4):04d}"
    STATE["fails"] = 0


def qr_matrix(text):
    q = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_M)
    q.add_data(text)
    return [[1 if v else 0 for v in row] for row in q.get_matrix()]


CHAT_LOCK = threading.Lock()
LIVE_SECS = 30  # a phone counts as connected while it keeps polling (page closed => drops off)
COOKIE_AGE = 86400  # pairing survives closing the tab / reopening from history for a day


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        pass

    def send(self, code, body=b"", ctype="text/plain; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def page(self, name):
        with open(os.path.join(HERE, name), "rb") as f:
            self.send(200, f.read(), "text/html; charset=utf-8")

    def is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def cookie(self, key):
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return c[key].value if key in c else None

    def authed(self):
        ok = self.cookie("t") == STATE["token"]
        if ok:
            STATE["seen"][self.client_address[0]] = time.time()  # last request from this phone
        return ok

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        tok = urllib.parse.parse_qs(parsed.query).get("t", [""])[0]

        if path.startswith("/desktop"):
            if not self.is_local():
                return self.send(403)
            if path == "/desktop":
                return self.page("desktop.html")
            if path == "/desktop/chat":
                return self.chat_list(parsed.query)
            if path == "/desktop/info":
                data = {"url": STATE["url"], "qr": qr_matrix(STATE["url"]),
                        "pin": STATE["pin"], "connected": sorted(ip for ip, t in list(STATE["seen"].items()) if time.time() - t < LIVE_SECS),
                        "inbox": list_tree(STATE["inbox"]), "outbox": list_tree(STATE["outbox"])}
                return self.send(200, json.dumps(data).encode(), "application/json")
            return self.send(404)

        if path == "/" and tok == STATE["token"]:
            return self.send(302, headers={"Location": "/", "Set-Cookie":
                                           f"q={tok}; Path=/; Max-Age={COOKIE_AGE}; SameSite=Strict"})
        if path == "/":
            return self.page("index.html")
        if not self.authed():
            return self.send(401, b"pin" if self.cookie("q") == STATE["token"] else b"scan")
        if path == "/chat":
            return self.chat_list(parsed.query)
        if path == "/api/files":
            return self.send(200, json.dumps(list_tree(STATE["outbox"])).encode(), "application/json")
        if path.startswith("/zip/"):
            name = safe_name(path[5:])
            src = name and os.path.join(STATE["outbox"], name)
            if not src or not os.path.isdir(src):
                return self.send(404, b"Not found")
            if is_busy(src):
                return self.send(409, b"Still copying on the PC. Try again in a moment.")
            with tempfile.TemporaryFile() as tmp:
                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
                    for f in list_dir(src):
                        z.write(os.path.join(src, f["name"]), f"{name}/{f['name']}")
                size = tmp.tell()
                tmp.seek(0)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition",
                                 "attachment; filename*=UTF-8''" + urllib.parse.quote(name + ".zip"))
                self.end_headers()
                shutil.copyfileobj(tmp, self.wfile, 1 << 20)
            return
        if path.startswith("/files/"):
            name = safe_name(path[7:])
            folder = urllib.parse.parse_qs(parsed.query).get("folder", [""])[0]
            sub = safe_name(folder) if folder else ""
            p = name and os.path.join(STATE["outbox"], sub or "", name)
            if not p or not os.path.isfile(p):
                return self.send(404, b"Not found")
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(os.path.getsize(p)))
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
            self.end_headers()
            with open(p, "rb") as f:
                while chunk := f.read(1 << 20):
                    self.wfile.write(chunk)
            return
        self.send(404)

    def chat_list(self, query):
        since = int(urllib.parse.parse_qs(query).get("since", ["0"])[0] or 0)
        return self.send(200, json.dumps([m for m in STATE["chat"] if m["id"] > since]).encode(),
                         "application/json")

    def chat_add(self, sender):
        n = min(int(self.headers.get("Content-Length", 0)), 30000)
        text = self.rfile.read(n).decode("utf-8", "replace").rstrip()[:5000]
        if not text:
            return self.send(400, b"Empty")
        with CHAT_LOCK:
            last = STATE["chat"][-1]["id"] if STATE["chat"] else 0
            STATE["chat"].append({"id": last + 1, "from": sender, "text": text, "ts": time.time()})
            del STATE["chat"][:-200]
        self.send(204)

    def do_POST(self):
        if self.path == "/chat":
            return self.chat_add("phone") if self.authed() else self.send(401, b"scan")
        if self.path == "/desktop/chat":
            return self.chat_add("pc") if self.is_local() else self.send(403)
        if self.path != "/pair":
            return self.send(404)
        n = min(int(self.headers.get("Content-Length", 0)), 64)
        pin = self.rfile.read(n).decode().strip()
        if self.cookie("q") != STATE["token"]:
            return self.send(401, b"Scan the QR code first")
        if secrets.compare_digest(pin.encode(), STATE["pin"].encode()):
            new_pin()  # a PIN works once
            return self.send(204, headers={"Set-Cookie": f"t={STATE['token']}; Path=/; Max-Age={COOKIE_AGE}; SameSite=Strict"})
        STATE["fails"] += 1
        if STATE["fails"] >= 5:
            new_pin()  # too many guesses: old PIN is dead
            return self.send(429, b"Too many attempts. Look at the new PIN on your PC.")
        self.send(403, b"Wrong PIN")

    def do_PUT(self):
        if not self.authed():
            return self.send(401, b"Scan the QR code on your PC")
        up = urllib.parse.urlparse(self.path)
        name = safe_name(up.path.removeprefix("/upload/"))
        if not name:
            return self.send(400, b"Bad name")
        folder = urllib.parse.parse_qs(up.query).get("folder", [""])[0]
        remaining = int(self.headers.get("Content-Length", 0))
        dest = unique_path(target_dir(STATE["inbox"], folder), name)
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        if remaining:
            os.remove(tmp)
            return self.send(400, b"Incomplete upload")
        os.replace(tmp, dest)
        self.send(201, os.path.basename(dest).encode())


def start(share, port=8000):
    """Start the server in a background thread; returns (httpd, port)."""
    inbox, outbox = os.path.join(share, "From phone"), os.path.join(share, "For phone")
    os.makedirs(inbox, exist_ok=True)
    os.makedirs(outbox, exist_ok=True)
    archive = os.path.join(share, "Earlier sessions")  # leftovers are kept, just not shown
    for folder in (inbox, outbox):
        for n in os.listdir(folder):
            p = os.path.join(folder, n)
            if not n.startswith("."):
                os.makedirs(archive, exist_ok=True)
                shutil.move(p, unique_path(archive, n))
    while True:
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
            break
        except OSError:
            port += 1
    token = secrets.token_urlsafe(8)
    STATE["chat"] = []
    STATE.update(token=token, inbox=inbox, outbox=outbox,
                 url=f"http://{local_ip()}:{port}/?t={token}", seen={})
    new_pin()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port
