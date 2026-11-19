import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from flask import Flask, g, render_template, request


def create_app(config_path=None):
    """Read the selected configuration; runtime storage never performs schema DDL."""
    app = Flask(__name__, template_folder="templates")
    app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
    app.config.update(
        MAX_CONTENT_LENGTH=512 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=os.environ.get("WEB_COOKIE_SECURE", "").lower() == "true",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        TRUSTED_HOSTS=[v.strip() for v in os.environ.get("WEB_TRUSTED_HOSTS", "localhost,127.0.0.1,[::1]").split(",") if v.strip()],
        WEB_TRUSTED_ORIGINS=[v.strip().rstrip("/") for v in os.environ.get("WEB_TRUSTED_ORIGINS", "").split(",") if v.strip()],
    )
    from src.config import load_config
    config = load_config(config_path)
    app.config["news_bot_config"] = config
    app.config["NEWS_BOT_CONFIG_PATH"] = getattr(config, "config_path", "") or os.path.abspath(config_path or "config.yaml")
    from src.storage import Storage
    app.config["news_bot_storage"] = Storage(config.mysql)
    app.config["NEWS_BOT_STORAGE_CONFIG"] = config.mysql
    app.extensions["news_bot_storage_lock"] = threading.Lock()

    @app.before_request
    def request_config():
        g.csp_nonce = secrets.token_urlsafe(24)
        try:
            g.news_bot_config = load_config(app.config["NEWS_BOT_CONFIG_PATH"])
        except (OSError, ValueError, TypeError):
            g.config_error = True

    from web.auth import auth_bp, configure_auth
    configure_auth(app)
    app.register_blueprint(auth_bp)
    from web.routes import register_routes
    register_routes(app)

    @app.template_filter("display_time")
    def display_time(value, fmt="%Y-%m-%d %H:%M"):
        if not value:
            return "—"
        try:
            if isinstance(value, str):
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone(timedelta(hours=8))).strftime(fmt)
        except (ValueError, TypeError, AttributeError):
            return "—"

    @app.template_filter("safe_http_url")
    def safe_http_url(value):
        try:
            parsed = urlsplit(str(value))
            if (parsed.scheme.lower() in {"http", "https"} and parsed.hostname
                    and not parsed.username and not parsed.password
                    and not any(ord(c) < 32 for c in str(value))):
                return str(value)
        except ValueError:
            pass
        return "#"

    @app.after_request
    def security_headers(response):
        response.headers.update({
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self' 'nonce-" + getattr(g, "csp_nonce", "")
                + "'; style-src 'self' 'unsafe-inline'; "
                "font-src 'self'; img-src 'self' data:; object-src 'none'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            ),
        })
        if request.is_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.errorhandler(404)
    def not_found(error):
        return render_template("error.html", code=404, message="页面未找到"), 404

    @app.errorhandler(500)
    def server_error(error):
        return render_template("error.html", code=500, message="服务器内部错误，请检查服务日志。"), 500

    return app
