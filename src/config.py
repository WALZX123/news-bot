import os
import re
import yaml
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import tempfile
import uuid
from .url_safety import normalize_http_url, normalized_private_hosts, validate_network_url
from .secret_vault import load_model_key


class _UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                if key in mapping:
                    raise ValueError("Configuration YAML contains duplicate fields")
                mapping[key] = self.construct_object(value_node, deep=deep)
            except TypeError:
                raise ValueError("Configuration YAML field names must be scalars") from None
        return mapping


@dataclass
class MySQLConfig:
    host: str = "localhost"
    port: int = 3306
    user: str = "news_bot"
    password: str = ""
    database: str = "news_bot"
    charset: str = "utf8mb4"


@dataclass
class DingTalkConfig:
    webhook_url: str = ""
    secret: str = ""


@dataclass
class AIConfig:
    api_key: str = ""
    api_key_ref: str = ""
    base_url: str = ""
    model: str = "deepseek-v4-pro"
    max_tokens: int = 4096
    timeout_seconds: int = 60
    input_max_chars: int = 80000
    max_calls: int = 12
    total_timeout_seconds: int = 180
    total_token_budget: int = 262144


@dataclass
class RSSSource:
    name: str = ""
    url: str = ""


@dataclass
class PushTimeWindow:
    start: int = 0
    end: int = 24


@dataclass
class KeywordConfig:
    include: list[str] = field(default_factory=list)
    require: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    fuzzy_threshold: int = 70


@dataclass
class ReportConfig:
    weekly: bool = True
    monthly: bool = True
    quarterly: bool = True
    yearly: bool = True


@dataclass
class AppConfig:
    mysql: MySQLConfig = field(default_factory=MySQLConfig)
    dingtalk: DingTalkConfig = field(default_factory=DingTalkConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    rss_sources: list[RSSSource] = field(default_factory=list)
    fetch_interval_minutes: int = 120
    max_news_per_run: int = 50
    max_candidates_per_run: int = 100
    request_timeout: int = 15
    push_time_window: PushTimeWindow = field(default_factory=PushTimeWindow)
    incremental_mode: bool = False
    ai_interests: str = ""
    auto_summarize: bool = False
    summary_limit: int = 16
    keywords: KeywordConfig = field(default_factory=KeywordConfig)
    source_weights: dict[str, list[str]] = field(default_factory=dict)
    reports: ReportConfig = field(default_factory=ReportConfig)
    log_level: str = "INFO"
    log_file: str = "news_bot.log"
    config_path: str = ""
    web_password: str = ""
    retention_days: int = 400
    max_news_age_hours: int = 72
    allow_private_rss_hosts: list[str] = field(default_factory=list)
    rss_max_response_bytes: int = 2097152
    fetch_task_timeout: int = 120


def resolve_config_path(path=None) -> str:
    selected = path or os.environ.get("NEWS_BOT_CONFIG") or Path(__file__).resolve().parents[1] / "config.yaml"
    return str(Path(selected).expanduser().resolve())


def _mapping(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    return value


def _construct(cls, value, name):
    value = _mapping(value, name)
    if set(value) - {f.name for f in fields(cls)}:
        raise ValueError(f"{name} contains unknown fields")
    return cls(**value)


def _number(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")


def _string(value, name, *, required=False, limit=4096):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f"{name} must be a valid string")


def _strings(value, name):
    if not isinstance(value, list) or len(value) > 200:
        raise ValueError(f"{name} must be a list of at most 200 strings")
    for item in value:
        _string(item, name, required=True, limit=200)


def validate_config(cfg: AppConfig) -> AppConfig:
    for name, low, high in (("fetch_interval_minutes", 1, 10080), ("max_news_per_run", 0, 500),
                            ("max_candidates_per_run", 1, 500),
                            ("request_timeout", 1, 120), ("retention_days", 400, 3650),
                            ("max_news_age_hours", 1, 8760), ("rss_max_response_bytes", 1024, 10485760),
                            ("fetch_task_timeout", 1, 600)):
        _number(getattr(cfg, name), name, low, high)
    _number(cfg.mysql.port, "mysql.port", 1, 65535)
    _number(cfg.summary_limit, "summary_limit", 1, 40)
    if type(cfg.auto_summarize) is not bool:
        raise ValueError("auto_summarize must be boolean")
    for name in ("host", "user", "database", "charset"):
        _string(getattr(cfg.mysql, name), f"mysql.{name}", required=True)
    for obj, names in ((cfg.mysql, ("password",)), (cfg.dingtalk, ("webhook_url", "secret")),
                       (cfg.ai, ("api_key", "base_url", "model"))):
        for name in names:
            _string(getattr(obj, name), name)
    _string(cfg.ai.model, "ai.model", required=True, limit=200)
    if not isinstance(cfg.ai.api_key_ref, str) or (
            cfg.ai.api_key_ref and not re.fullmatch(r"[a-f0-9]{32}", cfg.ai.api_key_ref)):
        raise ValueError("ai.api_key_ref must be a local credential reference")
    for name, low, high in (("max_tokens", 256, 32768), ("timeout_seconds", 1, 300),
                            ("input_max_chars", 2000, 500000), ("max_calls", 1, 50),
                            ("total_timeout_seconds", 1, 900), ("total_token_budget", 1024, 1000000)):
        _number(getattr(cfg.ai, name), f"ai.{name}", low, high)
    for url in (cfg.dingtalk.webhook_url, cfg.ai.base_url):
        if url:
            normalize_http_url(url)
    _number(cfg.push_time_window.start, "push_time_window.start", 0, 23)
    _number(cfg.push_time_window.end, "push_time_window.end", 0, 24)
    if cfg.push_time_window.start == cfg.push_time_window.end:
        raise ValueError("push_time_window must have a non-empty range")
    if type(cfg.incremental_mode) is not bool:
        raise ValueError("incremental_mode must be boolean")
    for name in ("weekly", "monthly", "quarterly", "yearly"):
        if type(getattr(cfg.reports, name)) is not bool:
            raise ValueError(f"reports.{name} must be boolean")
    for name in ("include", "require", "exclude"):
        _strings(getattr(cfg.keywords, name), f"keywords.{name}")
    _number(cfg.keywords.fuzzy_threshold, "keywords.fuzzy_threshold", 0, 100)
    _mapping(cfg.source_weights, "source_weights")
    if set(cfg.source_weights) - {"tier1", "tier2"}:
        raise ValueError("source_weights contains unknown tiers")
    for values in cfg.source_weights.values():
        _strings(values, "source_weights")
    _strings(cfg.allow_private_rss_hosts, "allow_private_rss_hosts")
    normalized_private_hosts(cfg.allow_private_rss_hosts)
    if not isinstance(cfg.rss_sources, list) or len(cfg.rss_sources) > 100:
        raise ValueError("rss_sources must contain at most 100 sources")
    names = set()
    for source in cfg.rss_sources:
        if not isinstance(source, RSSSource):
            raise ValueError("rss_sources entries must be RSS sources")
        _string(source.name, "rss_sources.name", required=True, limit=128)
        _string(source.url, "rss_sources.url", required=True)
        validate_network_url(source.url, cfg.allow_private_rss_hosts, resolve=False)
        if source.name in names:
            raise ValueError("rss_sources names must be unique")
        names.add(source.name)
    _string(cfg.web_password, "web_password")
    _string(cfg.ai_interests, "ai_interests", limit=10000)
    _string(cfg.log_file, "log_file", required=True)
    if not isinstance(cfg.log_level, str) or cfg.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("log_level is invalid")
    return cfg


def _parse(text, path, *, environment=True):
    if not isinstance(text, str) or len(text.encode('utf-8')) > 1048576:
        raise ValueError("Configuration must be UTF-8 text of at most 1 MiB")
    try:
        raw = _mapping(yaml.load(text, Loader=_UniqueKeyLoader), "configuration")
    except yaml.YAMLError as exc:
        raise ValueError("Configuration YAML is invalid") from exc
    values = dict(raw)
    if "claude" in values:
        if "ai" in values:
            raise ValueError("Configuration contains two model sections")
        values["ai"] = values.pop("claude")
    for name, cls in (("mysql", MySQLConfig), ("dingtalk", DingTalkConfig), ("ai", AIConfig),
                      ("push_time_window", PushTimeWindow), ("keywords", KeywordConfig), ("reports", ReportConfig)):
        if name in values:
            values[name] = _construct(cls, values[name], name)
    if "rss_sources" in values:
        if not isinstance(values["rss_sources"], list):
            raise ValueError("rss_sources must be a list")
        values["rss_sources"] = [_construct(RSSSource, entry, "rss_sources") for entry in values["rss_sources"]]
    cfg = _construct(AppConfig, values, "configuration")
    cfg.config_path = path
    # Historical files contain [""] for disabled keywords. Normalize these
    # sentinels while still rejecting non-string entries during validation.
    for name in ("include", "require", "exclude"):
        words = getattr(cfg.keywords, name)
        if isinstance(words, list):
            setattr(cfg.keywords, name, [word.strip() if isinstance(word, str) else word
                                        for word in words if not isinstance(word, str) or word.strip()])
    if environment:
        for obj, prefix, names in ((cfg.mysql, "MYSQL", ("host", "port", "user", "password", "database")),
                                   (cfg.dingtalk, "DINGTALK", ("webhook_url", "secret")),
                                   (cfg.ai, "AI", ("api_key", "base_url", "model"))):
            for name in names:
                value = os.environ.get(f"{prefix}_{name.upper()}")
                if value is not None:
                    if name == "port":
                        try:
                            value = int(value)
                        except ValueError as exc:
                            raise ValueError("MYSQL_PORT must be an integer") from exc
                    setattr(obj, name, value)
    validate_config(cfg)
    if environment and cfg.ai.api_key_ref and "AI_API_KEY" not in os.environ:
        try:
            cfg.ai.api_key = load_model_key(cfg.ai.api_key_ref, cfg.ai.base_url)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            cfg.ai.api_key = ""
    return cfg


def load_config(config_path=None) -> AppConfig:
    path = resolve_config_path(config_path)
    return _parse(Path(path).read_text(encoding="utf-8"), path)


def save_config(path, text, *, expected_sha256=None) -> AppConfig:
    """Validate then atomically replace; preserve prior bytes in a unique backup.

    The optional digest rejects stale editor submissions. Cooperating processes
    serialize through .lock; a crash may require manual stale-lock recovery.
    """
    target = Path(resolve_config_path(path))
    _parse(text, str(target), environment=False)
    cfg = _parse(text, str(target))
    lock = Path(str(target) + ".lock")
    try:
        lock_fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError("Configuration is being saved; retry after reloading") from exc
    temporary = None
    try:
        os.close(lock_fd)
        previous = target.read_bytes() if target.exists() else b""
        if expected_sha256 is not None and hashlib.sha256(previous).hexdigest() != expected_sha256:
            raise ValueError("Configuration changed; reload before saving")
        if target.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = target.with_name(f"{target.name}.{stamp}.{uuid.uuid4().hex[:8]}.bak")
            with open(backup, "xb") as stream:
                os.chmod(backup, 0o600)
                stream.write(previous)
                stream.flush()
                os.fsync(stream.fileno())
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(text.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        return cfg
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
        os.unlink(lock)
