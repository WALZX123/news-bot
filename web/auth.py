"""Authentication and request guards for the local administration interface."""
import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import OrderedDict
from functools import wraps
from urllib.parse import unquote, urlsplit

from flask import (Blueprint, current_app, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

auth_bp = Blueprint("auth", __name__)
SETUP_MESSAGE = ("管理口令尚未配置或格式无效。请设置 WEB_PASSWORD 环境变量，"
                 "或在所选配置文件的 web_password 中填写 Werkzeug scrypt / pbkdf2 密码哈希；"
                 "修改环境变量后重启 Web。")


class LoginLimiter:
    """Per process bounded buckets; reverse proxies may add a shared rate limit."""
    def __init__(self):
        self.buckets = OrderedDict()
        self.lock = threading.Lock()

    def attempt(self, key):
        now = time.monotonic()
        with self.lock:
            self.buckets = OrderedDict((k, v) for k, v in self.buckets.items() if now - v[0] < 300)
            start, count = self.buckets.pop(key, (now, 0))
            self.buckets[key] = (start, count + 1)
            while len(self.buckets) > 1024:
                self.buckets.popitem(last=False)
            return count < 5

    def clear(self, key):
        with self.lock:
            self.buckets.pop(key, None)


def safe_next(value):
    value = value or "/"
    decoded = value
    for _ in range(3):
        decoded = unquote(decoded)
    if (not decoded.startswith("/") or decoded.startswith("//") or "\\" in decoded
            or any(ord(c) < 32 or ord(c) == 127 for c in decoded)
            or urlsplit(decoded).netloc or urlsplit(decoded).scheme):
        return "/"
    return value


def _stored_password():
    if getattr(g, "config_error", False):
        return ""
    env_hash = current_app.config.get("WEB_PASSWORD_HASH", "")
    if env_hash:
        return env_hash
    stored = getattr(get_config(), "web_password", "")
    if isinstance(stored, str) and stored.startswith(("scrypt:", "pbkdf2:sha256:")):
        try:
            method, salt, digest = stored.split("$")
            parts = method.split(":")
            if not salt or not digest or len(stored) > 512:
                return ""
            if parts[0] == "scrypt" and not (len(parts) == 4 and 16384 <= int(parts[1]) <= 131072
                                            and int(parts[2]) == 8 and 1 <= int(parts[3]) <= 4):
                return ""
            if parts[0] == "pbkdf2" and not (len(parts) == 3 and 600000 <= int(parts[2]) <= 2000000):
                return ""
            return stored
        except (ValueError, TypeError):
            return ""
    return ""


def password_version():
    key = current_app.secret_key
    return hmac.new(key.encode() if isinstance(key, str) else key,
                    _stored_password().encode(), hashlib.sha256).hexdigest()


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def check_password(raw):
    stored = _stored_password()
    try:
        return bool(stored) and len(raw) <= 1024 and check_password_hash(stored, raw)
    except (ValueError, TypeError):
        return False


def login_required(function):
    @wraps(function)
    def decorated(*args, **kwargs):
        if (not session.get("authenticated") or not _stored_password()
                or not hmac.compare_digest(session.get("password_version", ""), password_version())):
            session.clear()
            if request.path.startswith("/actions/"):
                return jsonify(status="unauthorized", message="登录已失效，请重新登录。"), 401
            return redirect(url_for("auth.login_page", next=request.full_path.rstrip("?")))
        return function(*args, **kwargs)
    return decorated


def get_storage():
    config = get_config()
    # The app holds only the current pool. Existing requests/jobs retain their
    # own old Storage reference until they finish; no private SDK close API is used.
    with current_app.extensions['news_bot_storage_lock']:
        if config.mysql != current_app.config["NEWS_BOT_STORAGE_CONFIG"]:
            from src.storage import Storage
            storage = Storage(config.mysql)
            current_app.config["news_bot_storage"] = storage
            current_app.config["NEWS_BOT_STORAGE_CONFIG"] = config.mysql
        return current_app.config["news_bot_storage"]


def get_config():
    return getattr(g, "news_bot_config", current_app.config["news_bot_config"])


def configure_auth(app):
    password = os.environ.get("WEB_PASSWORD", "")
    app.config["WEB_PASSWORD_HASH"] = generate_password_hash(password) if password else ""
    app.extensions["login_limiter"] = LoginLimiter()
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def protect_mutations():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        allowed = {request.host_url.rstrip("/")} | set(app.config["WEB_TRUSTED_ORIGINS"])
        for value in (request.headers.get("Origin"), request.headers.get("Referer")):
            if value:
                try:
                    parsed = urlsplit(value)
                    valid_origin = (parsed.scheme in ("http", "https")
                                    and f"{parsed.scheme}://{parsed.netloc}" in allowed)
                except ValueError:
                    valid_origin = False
                if not valid_origin:
                    return jsonify(status="forbidden", message="请求来源不可信。"), 403
        provided = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
        expected = session.get("csrf_token", "")
        if not expected or not hmac.compare_digest(provided, expected):
            return jsonify(status="forbidden", message="表单已失效，请刷新页面后重试。"), 403


@auth_bp.get("/login")
def login_page():
    setup_error = None if _stored_password() else SETUP_MESSAGE
    return render_template("login.html", setup_error=setup_error,
                           next_url=safe_next(request.args.get("next"))), 503 if setup_error else 200


@auth_bp.post("/login")
def login():
    next_url = safe_next(request.form.get("next") or request.args.get("next"))
    if not _stored_password():
        return render_template("login.html", setup_error=SETUP_MESSAGE, next_url=next_url), 503
    limiter = current_app.extensions["login_limiter"]
    key = request.remote_addr or "unknown"
    if not limiter.attempt(key):
        response = current_app.make_response((render_template("login.html", setup_error=None, next_url=next_url), 429))
        response.headers["Retry-After"] = "300"
        return response
    if check_password(request.form.get("password", "")):
        limiter.clear(key)
        session.clear()
        session["authenticated"] = True
        session["password_version"] = password_version()
        session.permanent = True
        csrf_token()
        return redirect(next_url)
    flash("密码错误。连续失败后请等待五分钟再试。", "danger")
    return render_template("login.html", setup_error=None, next_url=next_url), 401


@auth_bp.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("auth.login_page"))
