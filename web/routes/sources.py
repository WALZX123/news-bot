import logging
import yaml
import sqlite3
from pathlib import Path
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from web.auth import get_config, get_storage, login_required
from web.db import read_cursor
from web.routes.config_routes import read_snapshot
from web.config_text import ConfigTextError, change_source

logger = logging.getLogger("news_bot.web")
sources_bp = Blueprint("sources", __name__)


@sources_bp.get("/")
@login_required
def list_sources():
    from src.fetcher import get_source_health
    config = get_config()
    health_error = None
    try:
        health = get_source_health(health_path=_health_path(config))
    except (OSError, sqlite3.Error) as error:
        logger.error('Cannot read source health (%s)', type(error).__name__)
        health = {}
        health_error = '请求健康记录暂不可用，请检查服务日志。'
    with read_cursor(get_storage()) as cursor:
        cursor.execute(
            "SELECT source, COUNT(*) AS total, "
            "SUM(CASE WHEN created_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 DAY) THEN 1 ELSE 0 END) AS recent, "
            "MAX(created_at) AS last_seen FROM news_items GROUP BY source ORDER BY last_seen DESC")
        stats = {row["source"]: row for row in cursor.fetchall()}
    sources = [{"name": source.name, "url": source.url,
                "total": stats.get(source.name, {}).get("total", 0),
                "recent": stats.get(source.name, {}).get("recent", 0),
                "last_seen": stats.get(source.name, {}).get("last_seen"),
                "health": health.get(source.name)}
               for source in config.rss_sources]
    return render_template("sources.html", sources=sources, health_error=health_error)


def _health_path(config):
    return str(Path(config.config_path).parent / 'runtime' / 'source-health.sqlite3') if config.config_path else None


def _change_sources(**change):
    from src.config import save_config
    try:
        raw, version = read_snapshot()
        updated = change_source(raw, **change)
        save_config(current_app.config["NEWS_BOT_CONFIG_PATH"],
                    updated, expected_sha256=version)
        flash("源配置已保存。新 Web 请求、新任务和调度器下一轮读取；已开始的任务继续使用原配置。", "success")
    except ConfigTextError as error:
        flash(str(error), "danger")
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        flash("未保存源配置。请检查名称唯一、HTTP(S) URL 及文件是否被其他操作修改。", "danger")
    return redirect(url_for("sources.list_sources"))


@sources_bp.post("/add")
@login_required
def add_source():
    name = request.form.get("name", "").strip()
    url = request.form.get("url", "").strip()
    if not name or not url or len(name) > 128 or len(url) > 2048:
        flash("名称需为 1 到 128 个字符，HTTP(S) URL 不超过 2048 个字符。", "danger")
        return redirect(url_for("sources.list_sources"))
    return _change_sources(add=(name, url))


@sources_bp.post("/remove")
@login_required
def remove_source():
    name = request.form.get("name", "")
    return _change_sources(remove_name=name)


@sources_bp.post("/test")
@login_required
def test_source():
    from src.config import RSSSource
    from src.fetcher import FetchError, fetch_source
    name, url = request.form.get("name", "").strip(), request.form.get("url", "").strip()
    if not url or len(url) > 2048 or len(name) > 128:
        flash("源名称或 URL 无效。", "danger")
        return redirect(url_for("sources.list_sources"))
    config = get_config()
    try:
        items = fetch_source(RSSSource(name=name or "test", url=url), config.request_timeout,
                             allow_private_hosts=config.allow_private_rss_hosts,
                             max_news_age_hours=config.max_news_age_hours,
                             max_response_bytes=config.rss_max_response_bytes,
                             task_timeout=config.fetch_task_timeout,
                             health_path=_health_path(config))
    except (FetchError, ValueError):
        flash("源测试失败：检查 HTTP(S) URL、内容格式、网络和私网主机授权配置。", "danger")
        return render_template("sources_test.html", name=name, url=url, items=[]), 400
    flash(f"请求与解析成功，共 {len(items)} 条符合采集条件的新闻。空源不表示请求失败。", "info")
    return render_template("sources_test.html", name=name, url=url, items=items)
