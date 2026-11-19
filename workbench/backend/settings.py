from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
import yaml

from src.analyzer import ModelRequestError, ModelResponseError, model_target, probe_model
from src.automation import scheduler_status
from src.config import _parse, save_config
from src.secret_vault import store_model_key, vault_file
from src.url_safety import normalize_http_url
from web.config_text import ConfigTextError, change_source, edit_source, redact_secrets, replace_settings_fields, restore_secrets

router = APIRouter()


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class AdvancedUpdate(Revision):
    yaml: str = Field(min_length=1, max_length=1048576)


class ModelUpdate(Revision):
    base_url: str = Field(max_length=2048)
    model: str = Field(min_length=1, max_length=200)
    timeout_seconds: int = Field(ge=1, le=300, strict=True)
    max_tokens: int = Field(ge=256, le=32768, strict=True)
    api_key: SecretStr | None = Field(default=None, max_length=4096)


class AutomationUpdate(Revision):
    fetch_interval_minutes: int = Field(ge=1, le=10080, strict=True)
    window_start: int = Field(ge=0, le=23, strict=True)
    window_end: int = Field(ge=0, le=24, strict=True)
    max_news_per_run: int = Field(ge=0, le=500, strict=True)
    ai_interests: str = Field(max_length=10000)
    auto_summarize: bool | None = None
    summary_limit: int | None = Field(default=None, ge=1, le=40, strict=True)
    max_candidates_per_run: int | None = Field(default=None, ge=1, le=500, strict=True)
    max_news_age_hours: int | None = Field(default=None, ge=1, le=8760, strict=True)
    fetch_task_timeout: int | None = Field(default=None, ge=1, le=600, strict=True)
    incremental_mode: bool | None = None


class CredentialsUpdate(Revision):
    webhook_url: SecretStr | None = Field(default=None, max_length=2048)
    dingtalk_secret: SecretStr | None = Field(default=None, max_length=4096)
    mysql_password: SecretStr | None = Field(default=None, max_length=4096)


def _snapshot(path, expected=None):
    try:
        versioned = Path(path).read_bytes()
        if len(versioned) > 1048576:
            raise ValueError("Configuration too large")
        revision = hashlib.sha256(versioned).hexdigest()
        if expected is not None and revision != expected:
            raise HTTPException(409, "配置已被其他操作更新，请重新读取后再保存。")
        raw = versioned.decode("utf-8")
        return raw, revision, _parse(raw, str(path))
    except (OSError, TypeError, ValueError):
        raise HTTPException(503, "暂时无法读取配置，请检查本地 config.yaml。") from None


def _secret(value):
    return value.get_secret_value() if value else ""


def _model_base_url(value):
    if not value:
        return ""
    normalize_http_url(value)
    parts = urlsplit(value)
    if parts.query or parts.fragment or (parts.scheme.lower() != "https" and
                                         parts.hostname not in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("Model URL must be HTTPS or loopback HTTP without query or fragment")
    return value.rstrip("/")


def _public(request, snapshot):
    raw, revision, config = snapshot
    model = config.ai
    target_url, target_name, _ = model_target(model)
    try:
        base_url = _model_base_url(model.base_url)
    except ValueError:
        base_url = ""
    try:
        advanced = redact_secrets(raw)
    except (ConfigTextError, yaml.YAMLError):
        advanced = None
    if not base_url and model.base_url:
        # A legacy URL may carry its own query credential. Keep it out of the editor.
        advanced = None
    probe = request.app.state.last_probe
    return {
        "revision": revision,
        "yaml": advanced,
        "ai": {
            "base_url": base_url, "model": model.model, "effective_model": target_name,
            "api_key_configured": bool(model.api_key), "max_tokens": model.max_tokens,
            "timeout_seconds": model.timeout_seconds,
            "protected_key": bool(model.api_key_ref),
            "endpoint": urlsplit(target_url).hostname if target_url else "api.deepseek.com",
            "overrides": [name for name in ("AI_API_KEY", "AI_MODEL", "AI_BASE_URL")
                          if name in os.environ],
        },
        "schedule": {
            "fetch_interval_minutes": config.fetch_interval_minutes,
            "window_start": config.push_time_window.start, "window_end": config.push_time_window.end,
            "max_news_per_run": config.max_news_per_run, "ai_interests": config.ai_interests,
            "auto_summarize": config.auto_summarize, "summary_limit": config.summary_limit,
            "max_candidates_per_run": config.max_candidates_per_run, "max_news_age_hours": config.max_news_age_hours,
            "fetch_task_timeout": config.fetch_task_timeout, "incremental_mode": config.incremental_mode,
        },
        "credentials": {"webhook_configured": bool(config.dingtalk.webhook_url),
                        "dingtalk_secret_configured": bool(config.dingtalk.secret),
                        "mysql_password_configured": bool(config.mysql.password)},
        "database_restart_required": config.mysql != request.app.state.database.settings,
        "last_probe": probe["result"] if probe and probe["revision"] == revision else None,
    }


def _save(path, revision, modify):
    raw, actual, _ = _snapshot(path, revision)
    try:
        updated = modify(raw)
        file_settings = _parse(updated, str(path), environment=False)
        if file_settings.ai.base_url:
            _model_base_url(file_settings.ai.base_url)
        if updated != raw:
            save_config(path, updated, expected_sha256=actual)
    except (ConfigTextError, OSError, TypeError, ValueError, yaml.YAMLError):
        _snapshot(path, revision)
        raise HTTPException(422, "配置未保存。请检查格式、设置范围、接口地址和敏感字段占位符。") from None
    return _snapshot(path)


def _apply_snapshot(app, snapshot):
    _, revision, configuration = snapshot
    if revision == app.state.config_revision:
        return
    app.state.settings = configuration
    app.state.config_revision = revision
    for name, (lock, _, _) in app.state.cache.items():
        app.state.cache[name] = (lock, None, 0.0)


async def _update(request, version, modify):
    app = request.app
    async with app.state.settings_lock:
        snapshot = await asyncio.to_thread(_save, app.state.config_path, version, modify)
        _apply_snapshot(app, snapshot)
    return _public(request, snapshot)


@router.get("/settings")
async def read_settings(request: Request):
    app = request.app
    async with app.state.settings_lock:
        snapshot = await asyncio.to_thread(_snapshot, app.state.config_path)
        _apply_snapshot(app, snapshot)
    return _public(request, snapshot)


@router.put("/settings")
async def update_advanced(request: Request, data: AdvancedUpdate):
    return await _update(request, data.revision, lambda previous: restore_secrets(data.yaml, previous))


@router.put("/settings/ai")
async def update_model(request: Request, data: ModelUpdate):
    reference = None

    def edit(previous):
        nonlocal reference
        values = data.model_dump(exclude={"revision", "api_key"})
        values["model"] = data.model.strip()
        values["base_url"] = _model_base_url(data.base_url.strip())
        if key := _secret(data.api_key).strip():
            reference = store_model_key(key, values["base_url"])
            values.update(api_key="", api_key_ref=reference)
        return replace_settings_fields(previous, values, "ai")
    try:
        return await _update(request, data.revision, edit)
    except Exception:
        if reference:
            config_path = request.app.state.config_path
            try:
                # Keep a record that already belongs to a committed config, even if response generation failed.
                if reference not in Path(config_path).read_text(encoding="utf-8"):
                    vault_file(reference).unlink(missing_ok=True)
            except OSError:
                pass
        raise


@router.put("/settings/automation")
async def update_automation(request: Request, data: AutomationUpdate):
    def edit(previous):
        settings = data.model_dump(exclude={"revision", "window_start", "window_end"}, exclude_none=True)
        updated = replace_settings_fields(previous, settings)
        return replace_settings_fields(updated, {"start": data.window_start, "end": data.window_end},
                                       "push_time_window")
    return await _update(request, data.revision, edit)


@router.put("/settings/credentials")
async def update_credentials(request: Request, data: CredentialsUpdate):
    def edit(previous):
        dingtalk = {key: value for key, field in (("webhook_url", data.webhook_url), ("secret", data.dingtalk_secret))
                    if (value := _secret(field))}
        updated = replace_settings_fields(previous, dingtalk, "dingtalk")
        password = _secret(data.mysql_password)
        return replace_settings_fields(updated, {"password": password} if password else {}, "mysql")
    return await _update(request, data.revision, edit)


_PROBE_ERRORS = {
    "missing_key": "API Key 未配置，或当前 Windows 用户无法读取保护区中的密钥。",
    "unauthorized": "服务拒绝了 API Key，请检查密钥。",
    "quota": "服务返回额度不足。",
    "forbidden": "当前密钥没有调用权限。",
    "invalid_request": "模型或请求参数不受接口支持，请核对模型名称与 Base URL。",
    "not_found": "找不到模型或接口，请核对 Base URL。",
    "rate_limited": "调用频率达到服务限制。",
    "timeout": "模型请求超时。",
    "network": "无法连接模型服务，请检查网络与代理。",
    "provider_error": "模型服务请求失败。",
    "invalid_response": "模型返回的内容没有通过响应校验。",
    "invalid_endpoint": "请先保存有效的 HTTPS 或本地接口地址。",
}


@router.post("/settings/ai/test")
async def test_model(request: Request, data: Revision):
    app = request.app
    if app.state.probe_lock.locked():
        raise HTTPException(409, "已有一次模型测试正在运行。")
    async with app.state.probe_lock:
        _, revision, settings = await asyncio.to_thread(_snapshot, app.state.config_path, data.revision)
        result = {"checked_at": datetime.now(timezone.utc).isoformat()}
        try:
            _model_base_url(settings.ai.base_url)
        except ValueError:
            code = "invalid_endpoint"
        else:
            try:
                result.update(await asyncio.to_thread(probe_model, settings.ai), ok=True)
                code = None
            except ModelRequestError as error:
                code = error.code
            except ModelResponseError:
                code = "invalid_response"
        if code:
            result.update(ok=False, code=code, message=_PROBE_ERRORS[code])
        app.state.last_probe = {"revision": revision, "result": result}
        return result


@router.get("/automation")
def automation(request: Request):
    return scheduler_status(request.app.state.config_path)


class SourceName(Revision):
    name: str = Field(min_length=1, max_length=128)


class SourceChange(SourceName):
    url: str = Field(min_length=1, max_length=2048)
    original: str | None = Field(default=None, max_length=128)


class SourceTrial(BaseModel):
    name: str = Field(default="", max_length=128)
    url: str = Field(min_length=1, max_length=2048)


@router.get("/manage/sources")
def source_revision(request: Request):
    _, revision, settings = _snapshot(request.app.state.config_path)
    return {"revision": revision, "sources": [{"name": item.name, "url": item.url} for item in settings.rss_sources]}


@router.post("/manage/sources")
async def create_source(request: Request, data: SourceChange):
    saved = await _update(request, data.revision, lambda text: change_source(text, add=(data.name.strip(), data.url.strip())))
    return {"revision": saved["revision"]}


@router.put("/manage/sources")
async def update_source(request: Request, data: SourceChange):
    if not data.original:
        raise HTTPException(422, "请先选择要修改的来源。")
    saved = await _update(request, data.revision, lambda text: edit_source(text, data.original, data.name.strip(), data.url.strip()))
    return {"revision": saved["revision"]}


@router.delete("/manage/sources")
async def delete_source(request: Request, data: SourceName):
    saved = await _update(request, data.revision, lambda text: change_source(text, remove_name=data.name))
    return {"revision": saved["revision"]}


@router.post("/manage/sources/test")
async def try_source(request: Request, data: SourceTrial):
    app = request.app
    if app.state.source_probe_lock.locked():
        raise HTTPException(409, "已有一次订阅试抓正在运行。")
    async with app.state.source_probe_lock:
        return await asyncio.to_thread(probe_source, app.state.config_path, data)


def probe_source(path, data):
    from tempfile import TemporaryDirectory
    from src.config import RSSSource
    from src.fetcher import FetchError, fetch_source

    _, _, settings = _snapshot(path)
    try:
        with TemporaryDirectory() as directory:
            items = fetch_source(RSSSource(data.name.strip() or "试抓", data.url.strip()),
                                 min(settings.request_timeout, 8),
                                 allow_private_hosts=settings.allow_private_rss_hosts,
                                 max_news_age_hours=settings.max_news_age_hours,
                                 max_response_bytes=settings.rss_max_response_bytes, task_timeout=10,
                                 health_path=str(Path(directory) / "source-test.sqlite3"))
    except FetchError as error:
        reason = str(error).removeprefix("RSS source failed: ")
        detail = {
            "content_type": "地址返回的内容类型不是 RSS 或 Atom。请检查是否填了普通网页。",
            "timeout": "请求超过试抓时限。请检查来源网络或稍后重试。",
            "http_status": "来源拒绝了请求或链接不存在。请检查订阅地址。",
            "invalid_feed": "返回内容无法解析成 RSS 或 Atom。",
            "unsafe_url": "地址不符合采集规则。",
            "network": "本机未能连接这个来源。请检查地址与网络。",
        }.get(reason, "试抓失败，请检查链接、响应格式或网络。")
        raise HTTPException(422, detail) from None
    except (ValueError, OSError):
        raise HTTPException(422, "试抓失败，请检查链接、响应格式或网络。") from None
    return {"count": len(items), "titles": [item.title for item in items[:4]]}
