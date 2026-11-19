import yaml
import logging
import hashlib
from pathlib import Path
from flask import Blueprint, render_template, request, flash, redirect, url_for, current_app
from web.auth import login_required, get_config
from web.config_text import ConfigTextError, redact_secrets, restore_secrets

logger = logging.getLogger("news_bot.web")
config_bp = Blueprint("config", __name__)


def _mask(value: str) -> str:
    if not value:
        return "(未设置)"
    return "****"


def read_snapshot():
    data = Path(current_app.config["NEWS_BOT_CONFIG_PATH"]).read_bytes()
    return data.decode('utf-8'), hashlib.sha256(data).hexdigest()


@config_bp.route("/")
@login_required
def view_config():
    config = get_config()

    sections = {
        "数据库 (MySQL)": [
            ("主机", config.mysql.host),
            ("端口", str(config.mysql.port)),
            ("用户名", config.mysql.user),
            ("密码", _mask(config.mysql.password)),
            ("数据库名", config.mysql.database),
        ],
        "AI API": [
            ("Base URL", config.ai.base_url),
            ("模型", config.ai.model),
            ("Max Tokens", str(config.ai.max_tokens)),
            ("API Key", _mask(config.ai.api_key)),
        ],
        "钉钉推送": [
            ("Webhook URL", _mask(config.dingtalk.webhook_url)),
            ("Secret", _mask(config.dingtalk.secret) if config.dingtalk.secret else "(未设置)"),
        ],
        "运行参数": [
            ("抓取间隔", f"{config.fetch_interval_minutes} 分钟"),
            ("每次最大新闻数", str(config.max_news_per_run)),
            ("请求超时", f"{config.request_timeout} 秒"),
            ("推送窗口", f"{config.push_time_window.start}:00 - {config.push_time_window.end}:00"),
            ("增量模式", "开启" if config.incremental_mode else "关闭"),
        ],
        "兴趣筛选": [
            ("AI 兴趣描述", config.ai_interests[:80] + "..." if len(config.ai_interests) > 80 else config.ai_interests),
        ],
        "关键词": [
            ("Include (OR)", ", ".join(config.keywords.include) if config.keywords.include else "(全部)"),
            ("Require (AND)", ", ".join(config.keywords.require) if config.keywords.require else "(无)"),
            ("Exclude", ", ".join(config.keywords.exclude) if config.keywords.exclude else "(无)"),
        ],
        "源权重": [
            ("Tier 1 (权重3)", ", ".join(config.source_weights.get("tier1", [])) or "(无)"),
            ("Tier 2 (权重2)", ", ".join(config.source_weights.get("tier2", [])) or "(无)"),
        ],
        "报告": [
            ("周报", "开启" if config.reports.weekly else "关闭"),
            ("月报", "开启" if config.reports.monthly else "关闭"),
            ("季报", "开启" if config.reports.quarterly else "关闭"),
            ("年报", "开启" if config.reports.yearly else "关闭"),
        ],
    }

    return render_template("config_view.html", sections=sections)


@config_bp.route("/edit")
@login_required
def edit_config_form():
    try:
        raw, version = read_snapshot()
        return render_template("config_edit.html", raw_yaml=redact_secrets(raw), version=version, error=None)
    except ConfigTextError as error:
        return render_template("config_edit.html", raw_yaml="", version="", error=str(error)), 422
    except (OSError, ValueError, yaml.YAMLError):
        return render_template("config_edit.html", raw_yaml="", version="", error="无法读取所选配置文件。"), 503


@config_bp.route("/edit", methods=["POST"])
@login_required
def save_config():
    from src.config import save_config as persist_config
    try:
        raw = request.form.get("yaml_content", "")
        old, version = read_snapshot()
        if not request.form.get("version") or request.form["version"] != version:
            raise ValueError("配置已被其他操作修改，请重新打开编辑页。")
        restored = restore_secrets(raw, old)
        persist_config(current_app.config["NEWS_BOT_CONFIG_PATH"],
                       restored, expected_sha256=version)
        flash("配置已备份并保存。Web 新请求、新任务和调度器下一轮读取新配置；已开始的任务使用原配置。日志级别、日志文件或环境变量变更需要重启对应进程。", "success")
        return redirect(url_for("config.view_config"))
    except ConfigTextError as error:
        flash(str(error), "danger")
        return redirect(url_for("config.edit_config_form")), 303
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        # Parser exceptions include source snippets: do not echo submitted secrets.
        flash("配置未保存。请检查格式、数值、源名称/URL、保密占位符及文件版本后重新编辑。", "danger")
        return redirect(url_for("config.edit_config_form")), 303
