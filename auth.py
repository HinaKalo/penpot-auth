import hashlib
import html
import http.client
import json
import logging
import os
import re
import socket
import socketserver
import time
import unicodedata
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from threading import BoundedSemaphore, Lock
from urllib.parse import parse_qs, urlencode, urlsplit

ORIGIN = "https://pen.moonl.cloud"
SOCKET_PATH = Path("/run/penpot-auth/http.sock")
FILES = Path(__file__).resolve().parent
AUTH_ROUTES = {"/auth/login", "/auth/register"}
COOKIE_HEADERS = {"Host": "pen.moonl.cloud", "X-Forwarded-Proto": "https"}
MAX_FORM_SIZE = 4096
PUBLIC_CONFIG = "/js/config.js"
PAGE_TEMPLATE = (FILES / "account.html").read_text(encoding="utf-8")
STYLESHEET = (FILES / "account.css").read_bytes()
BRIDGE = (FILES / "bridge.js").read_bytes()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
request_lock = Lock()
heavy_requests = BoundedSemaphore(4)
requests = OrderedDict()


class AccountError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def check_rate(client, register):
    window, limit = (3600, 8) if register else (300, 30)
    now = time.monotonic()
    key = (register, client)
    with request_lock:
        started, count = requests.pop(key, (now, 0))
        if now - started >= window:
            started, count = now, 0
        requests[key] = (started, count + 1)
        if len(requests) > 4096:
            requests.popitem(last=False)
    if count >= limit:
        raise AccountError(429, "尝试次数过多，请稍后再试。")


def account_email(username, register):
    if not register and username.lower() == "owner@pen.moonl.cloud":
        return "owner@pen.moonl.cloud"
    canonical = unicodedata.normalize("NFKC", username).casefold()
    if not re.fullmatch(r"[^\W_][\w-]{1,31}", canonical):
        raise AccountError(400, "用户名须为 2–32 个字符，以字母、汉字或数字开头，可用下划线和短横线。")
    if canonical == "owner":
        if register:
            raise AccountError(409, "这个用户名已被使用。")
        return "owner@pen.moonl.cloud"
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return f"{digest}@users.pen.moonl.cloud"


def create_profile(username, email, password):
    payload = {"cmd": "create-profile", "params": {
        "fullname": username, "email": email, "password": password,
    }}
    with socket.create_connection(("127.0.0.1", 6063), timeout=8) as conn:
        conn.sendall(json.dumps(payload, ensure_ascii=False).encode() + b"\n")
        with conn.makefile("rb") as source:
            for _ in range(12):
                line = source.readline(65536)
                if not line:
                    break
                frame = json.loads(line)
                if frame.get("tag") != "ret":
                    continue
                if frame.get("val") and not frame.get("err"):
                    return
                err = frame.get("err") or {}
                if err.get("code") == "email-already-exists" or err.get("hint") == "email already exists":
                    raise AccountError(409, "这个用户名已被使用。")
                logging.warning("Penpot profile creation failed (%s)", err.get("code", "unknown"))
                break
    raise AccountError(503, "注册暂时不可用，请稍后重试。")


def login(email, password):
    conn = http.client.HTTPConnection("127.0.0.1", 6060, timeout=8)
    try:
        body = json.dumps({"email": email, "password": password}).encode()
        conn.request("POST", "/api/rpc/command/login-with-password", body, {
            **COOKIE_HEADERS, "Origin": ORIGIN, "Content-Type": "application/json", "Accept": "application/json",
        })
        response = conn.getresponse()
        cookies = [value for name, value in response.getheaders() if name.lower() == "set-cookie"]
        if response.status == 200 and cookies:
            return cookies
        if response.status == 429:
            raise AccountError(429, "尝试次数过多，请稍后再试。")
        if response.status in (400, 401, 403):
            raise AccountError(401, "用户名或密码不正确。")
        raise AccountError(503, "登录暂时不可用，请稍后重试。")
    finally:
        conn.close()


def next_path(value):
    if len(value) > 2048 or not value.startswith("/?") or any(ord(ch) < 32 for ch in value):
        return "/"
    query = parse_qs(urlsplit(value).query)
    screen = query.get("screen", [""])[0]
    if screen not in {"workspace", "viewer"} and not screen.startswith("dashboard-"):
        return "/"
    return value


class AuthHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(12)
        super().setup()

    def log_message(self, *_args):
        pass

    def reply(self, status, body=b"", content_type="text/plain; charset=utf-8", headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; img-src 'self'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def account_page(self, register, *, status=200, error="", username="", destination="/"):
        route = "/auth/register" if register else "/auth/login"
        action = "注册并登录" if register else "登录"
        heading = "创建账号" if register else "登录 Penpot"
        other_route = "/auth/login" if register else "/auth/register"
        other_text = "已有账号？登录" if register else "没有账号？自助注册"
        link_query = ""
        if destination != "/":
            link_query = "?" + urlencode({"next": destination})
        fields = {
            "heading": heading, "form_action": route, "action": action,
            "password_hint": '<p class="hint" id="password-hint">至少 8 个字符</p>' if register else "",
            "password_attrs": 'minlength="8" aria-describedby="password-hint"' if register else "",
            "autocomplete": "new-password" if register else "current-password",
            "other_link": html.escape(other_route + link_query), "other_text": other_text,
            "error": f'<p class="error" role="alert">{html.escape(error)}</p>' if error else "",
            "username": html.escape(username), "destination": html.escape(destination),
        }
        self.reply(status, PAGE_TEMPLATE.format_map(fields).encode(), "text/html; charset=utf-8")

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path in AUTH_ROUTES:
            dest = next_path(parse_qs(url.query).get("next", ["/"])[0])
            self.account_page(url.path == "/auth/register", destination=dest)
        elif url.path == "/auth/account.css":
            self.reply(200, STYLESHEET, "text/css; charset=utf-8")
        elif url.path == PUBLIC_CONFIG:
            conn = http.client.HTTPConnection("penpot-frontend", 8080, timeout=4)
            try:
                conn.request("GET", PUBLIC_CONFIG, headers=COOKIE_HEADERS)
                upstream = conn.getresponse()
                config = upstream.read(65536)
                if upstream.status != 200:
                    self.reply(502)
                else:
                    self.reply(200, config + b"\n" + BRIDGE, "application/javascript; charset=utf-8")
            except OSError:
                self.reply(502)
            finally:
                conn.close()
        else:
            self.reply(404)

    do_HEAD = do_GET

    def do_POST(self):
        route = urlsplit(self.path).path
        if route not in AUTH_ROUTES:
            self.reply(404)
            return
        if self.headers.get("Origin") != ORIGIN:
            self.reply(403)
            return
        if self.headers.get_content_type() != "application/x-www-form-urlencoded":
            self.reply(415)
            return
        length = self.headers.get("Content-Length", "")
        if not length.isdecimal() or len(length) > 4 or not 0 < int(length) <= MAX_FORM_SIZE:
            self.reply(413)
            return
        try:
            form = parse_qs(self.rfile.read(int(length)).decode(), max_num_fields=3, keep_blank_values=True)
        except (ValueError, UnicodeError):
            self.reply(400)
            return
        username = form.get("username", [""])[0].strip()
        password = form.get("password", [""])[0]
        destination = next_path(form.get("next", ["/"])[0])
        register = route == "/auth/register"
        try:
            if not username or len(username) > 64 or not password or len(password) > 128:
                raise AccountError(400, "请输入有效的用户名和密码。")
            if register and (len(password) < 8 or not password.strip()):
                raise AccountError(400, "密码至少需要 8 个字符。")
            email = account_email(username, register)
            check_rate(self.headers.get("X-Penpot-Client", "local"), register)
            if not heavy_requests.acquire(blocking=False):
                raise AccountError(503, "请求较多，请稍后再试。")
            try:
                if register:
                    create_profile(username, email, password)
                cookies = login(email, password)
            finally:
                heavy_requests.release()
        except AccountError as exc:
            self.account_page(register, status=exc.status, error=str(exc), username=username[:64], destination=destination)
        except (OSError, ValueError, http.client.HTTPException):
            logging.warning("Penpot authentication upstream unavailable")
            self.account_page(register, status=503, error="服务暂时不可用，请稍后再试。",
                              username=username[:64], destination=destination)
        else:
            headers = [("Location", destination)] + [("Set-Cookie", value) for value in cookies]
            self.reply(303, headers=headers)


class AuthServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 32


if __name__ == "__main__":
    SOCKET_PATH.unlink(missing_ok=True)
    os.umask(0o117)
    with AuthServer(str(SOCKET_PATH), AuthHandler) as server:
        server.serve_forever()
