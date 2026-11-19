import base64
import hashlib
import hmac
import logging
import time
import urllib.parse
import html
import re

import requests

from .config import DingTalkConfig
from .url_safety import normalize_http_url, UnsafeURLError

logger = logging.getLogger("news_bot.sender")

DINGTALK_MAX_CHARS = 20000
# Local conservative byte policy; actual tenant/platform limit needs live acceptance.
DINGTALK_MAX_BYTES = 18000


class DeliveryUnknownError(RuntimeError):
    """The POST may have arrived; reconcile manually before resending."""


def _build_signed_url(webhook_url: str, secret: str) -> str:
    timestamp = str(round(time.time() * 1000))
    sign_str = f"{timestamp}\n{secret}"
    sign = base64.b64encode(
        hmac.new(secret.encode(), sign_str.encode(), hashlib.sha256).digest()
    ).decode()
    parsed = urllib.parse.urlsplit(webhook_url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key not in {"timestamp", "sign"}]
    query.extend([("timestamp", timestamp), ("sign", sign)])
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))


def _safe_text(value):
    value = html.escape(str(value), quote=False)
    return re.sub(r"([\\`*\[\]{}()#!|_])", r"\\\1", value)


def _markdown_url(value):
    try:
        url = normalize_http_url(value)
        # Parentheses and spaces in URLs must not escape Markdown link syntax.
        return url.replace("(", "%28").replace(")", "%29").replace("<", "%3C").replace(">", "%3E")
    except UnsafeURLError:
        return ""


def _original_link(item):
    url = _markdown_url(item.get("url", ""))
    return f"[原文]({url})" if url else ""


def _original_links(item):
    return " ".join(filter(None, (_original_link({"url": url}) for url in item.get("urls") or [item.get("url", "")])))


def _reference_links(references, index):
    return _original_links({"urls": references[index]}) if index < len(references) else ""


def _visible_urls(markdown):
    return set(re.findall(r"\[原文\]\((https?://[^\r\n)]+)\)", markdown))


def visible_article_ids(payload, articles):
    urls = _visible_urls(payload["markdown"])
    return [article["id"] for article in articles if _markdown_url(article["url"]) in urls]


SEP = "\n  \n"  # DingTalk-compatible section separator (two trailing spaces + newline)


def _format_markdown(analysis: dict, news_count: int) -> str:
    parts = []

    # 标题
    parts.append("# 每日新闻深度简报\n")
    headline = analysis.get("headline", "")
    if headline:
        parts.append(f"> {_safe_text(headline)}")
        parts.append(SEP)

    # TOP 要闻
    top8 = analysis.get("top8", analysis.get("top5", []))
    if top8:
        parts.append("## 今日要闻 TOP {}\n".format(len(top8)))
        importance_map = {"极高": "极重要", "高": "重要", "中": ""}
        for item in top8:
            rank = item.get("rank", "")
            title = item.get("title", "")
            summary = item.get("summary", "")
            source = item.get("source", "")
            importance = item.get("importance", "")
            tag = f"【{importance_map[importance]}】" if importance in importance_map and importance_map[importance] else ""
            prefix = f"{rank}. {tag}" if rank else f"- {tag}"
            parts.append(f"**{prefix} {_safe_text(title)}** {_original_links(item)}")
            if summary:
                parts.append(f"> {_safe_text(summary)}")
            if source:
                parts.append(f"> 来源：{_safe_text(source)}")
            parts.append("")
        parts.append(SEP)

    # 分类汇总
    categories = analysis.get("categories", {})
    if categories:
        parts.append("## 分类速览\n")
        for cat_name, cat_items in categories.items():
            if not cat_items:
                continue
            parts.append(f"### {_safe_text(cat_name)}（{len(cat_items)}条）")
            for item in cat_items:
                title = item.get("title", "")
                brief = item.get("brief", "") or item.get("summary", "") or ""
                parts.append(f"- **{_safe_text(title)}**：{_safe_text(brief)} {_original_links(item)}")
            parts.append("")
        parts.append(SEP)

    # 趋势洞察
    trends = analysis.get("trends", [])
    if trends:
        parts.append("## 趋势与洞察（AI 研判）\n")
        references = analysis.get("trend_sources", [])
        for i, t in enumerate(trends, 1):
            parts.append(f"{i}. {_safe_text(t)} {_reference_links(references, i - 1)}".rstrip())
            parts.append("")
        parts.append(SEP)

    # 数据亮点
    data_highlights = analysis.get("data_highlights", [])
    if data_highlights:
        parts.append("## 关键数据\n")
        references = analysis.get("data_highlight_sources", [])
        for index, d in enumerate(data_highlights):
            parts.append(f"- {_safe_text(d)} {_reference_links(references, index)}".rstrip())
        parts.append(SEP)

    # 深度推荐
    deep_reads = analysis.get("deep_reads", [])
    if deep_reads:
        parts.append("## 值得深读\n")
        for item in deep_reads:
            title = item.get("title", "")
            reason = item.get("reason", "")
            source = item.get("source", "")
            parts.append(f"- **{_safe_text(title)}** {_original_links(item)}")
            if reason:
                parts.append(f"  > 推荐：{_safe_text(reason)}")
            if source:
                parts.append(f"  > 来源：{_safe_text(source)}")
        parts.append(SEP)

    # 情感基调
    sentiment = analysis.get("sentiment", "")
    if sentiment:
        parts.append("## 今日情绪基调（AI 研判）\n")
        parts.append(f"> {_safe_text(sentiment)}")
        parts.append(SEP)

    # 争议话题
    controversies = analysis.get("controversies", [])
    if controversies:
        parts.append("## 舆论争议\n")
        references = analysis.get("controversy_sources", [])
        for index, c in enumerate(controversies):
            parts.append(f"- {_safe_text(c)} {_reference_links(references, index)}".rstrip())
        parts.append(SEP)

    # 尾部
    one_liner = analysis.get("one_liner", "")
    if one_liner and not headline:
        parts.append(f"> {_safe_text(one_liner)}")
        parts.append("")
    parts.append(f"共分析 {news_count} 条新闻 | 由 AI 生成 | 仅供学习交流")

    return "\n".join(parts)


def _smart_truncate(markdown_text: str, max_chars: int = 20000, *, max_bytes=None) -> str:
    """Reserve the mandatory footer before trimming a bounded UTF-8 prefix."""
    limit = min(max_bytes if max_bytes is not None else DINGTALK_MAX_BYTES, max_chars)
    if len(markdown_text) <= max_chars and len(markdown_text.encode("utf-8")) <= limit:
        return markdown_text
    match = re.search(r"(共分析 [^\n]*由 AI 生成[^\n]*)\s*$", markdown_text)
    footer = match.group(1) if match else "由 AI 生成 | 仅供学习交流"
    suffix = "\n\n> 消息过长，部分内容已省略；完整简报保存在本地。\n\n" + footer
    reserve = len(suffix.encode("utf-8"))
    if reserve > limit:
        return footer.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
    body = markdown_text[:match.start()] if match else markdown_text
    prefix = body.encode("utf-8")[:limit - reserve].decode("utf-8", errors="ignore")
    # Prefer complete lines when possible; keep a prefix of one oversized line.
    if "\n" in prefix and len(prefix.rsplit("\n", 1)[0]) > len(prefix) // 2:
        prefix = prefix.rsplit("\n", 1)[0]
    return prefix + suffix


def delivery_payload(analysis: dict, news_count: int) -> dict:
    saved = analysis.get("_delivery")
    if isinstance(saved, dict) and isinstance(saved.get("markdown"), str):
        return saved
    full = _format_markdown(analysis, news_count)
    markdown = _smart_truncate(full, DINGTALK_MAX_CHARS)
    return {"markdown": markdown, "visible_links": len(_visible_urls(markdown)), "truncated": full != markdown}


def send_to_dingtalk(analysis: dict, news_count: int, config: DingTalkConfig) -> bool:
    markdown_text = delivery_payload(analysis, news_count)["markdown"]
    try:
        url = normalize_http_url(config.webhook_url)
    except UnsafeURLError:
        logger.error("DingTalk endpoint is invalid")
        return False
    if config.secret:
        url = _build_signed_url(url, config.secret)
    payload = {"msgtype": "markdown", "markdown": {"title": "每日新闻简报", "text": markdown_text}}
    response = None
    try:
        # One attempt. Core owns retry state and manual reconciliation of unknown.
        response = requests.post(url, json=payload, timeout=(5, 15), allow_redirects=False)
    except requests.ConnectTimeout:
        logger.warning("DingTalk connection timed out before delivery")
        return False
    except requests.RequestException:
        logger.warning("DingTalk delivery unknown (transport)")
        raise DeliveryUnknownError("Delivery unknown; reconcile this batch before retrying") from None
    try:
        status = response.status_code
        if not 200 <= status < 300:
            logger.warning("DingTalk rejected HTTP status %d", status)
            if status >= 500:
                raise DeliveryUnknownError("Delivery unknown; server receipt unavailable")
            return False
        try:
            data = response.json()
        except Exception:
            logger.warning("DingTalk delivery unknown (invalid receipt)")
            raise DeliveryUnknownError("Delivery unknown; invalid receipt") from None
        if not isinstance(data, dict) or type(data.get("errcode")) is not int:
            logger.warning("DingTalk delivery unknown (invalid receipt schema)")
            raise DeliveryUnknownError("Delivery unknown; invalid receipt schema")
        if data["errcode"] != 0:
            logger.warning("DingTalk rejected message (business code %d)", data["errcode"])
            return False
        logger.info("DingTalk acknowledged delivery")
        return True
    finally:
        try:
            response.close()
        except Exception:
            # Cleanup cannot turn an acknowledged delivery into a retry.
            logger.warning("DingTalk response cleanup failed")
