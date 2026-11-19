from datetime import datetime
from hashlib import sha256
import json

from src.pipeline import normalize_url
from src.storage import db_time, utc
from src.url_safety import normalize_http_url

ARCHIVE_PREFIX = "operator_archived:"
ARCHIVE_PATTERN = ARCHIVE_PREFIX + "%"


class NewsConflict(ValueError):
    pass


def revision(row):
    stable = [row[key] for key in ("id", "title", "summary", "source", "url", "processing_state", "skip_reason", "published_at")]
    published = stable[-1]
    if isinstance(published, datetime):
        stable[-1] = utc(published).isoformat()
    return sha256(json.dumps(stable, ensure_ascii=False).encode()).hexdigest()


def edit_values(data):
    url = normalize_url(normalize_http_url(data["url"].strip()))
    title, source = data["title"].strip(), data["source"].strip()
    if not title or not source:
        raise NewsConflict("请填写标题和来源。")
    date = data.get("published_at")
    if date is not None and date.tzinfo is None:
        raise NewsConflict("发布时间需要明确时区。")
    return url, title, data["summary"].strip(), source, db_time(date) if date else None


def change_news(storage, article_id, expected, action, data=None):
    with storage.pipeline_lock() as acquired:
        if not acquired:
            raise NewsConflict("后台正在处理新闻，请稍后重试。")
        storage.assert_pipeline_lock()
        with storage.cursor(dictionary=True, write=True) as cursor:
            cursor.execute("SELECT * FROM news_items WHERE id=%s", (article_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            if revision(row) != expected:
                raise NewsConflict("此新闻已更新。重新打开详情后再操作。")
            reason = row["skip_reason"] or ""
            archived = reason.startswith(ARCHIVE_PREFIX)
            if action == "restore":
                if not archived or row["batch_id"] is not None or row["processing_state"] != "skipped":
                    raise NewsConflict("这条新闻当前不在回收站。")
                _, state, previous = reason.split(":", 2)
                if state not in {"pending", "skipped"}:
                    raise NewsConflict("这条新闻的归档状态无效。")
                cursor.execute("UPDATE news_items SET processing_state=%s,skip_reason=%s WHERE id=%s",
                               (state, previous, article_id))
            else:
                if archived or row["processing_state"] not in {"pending", "skipped"} or row["batch_id"] is not None:
                    raise NewsConflict("仅可编辑或移除尚未组批的新闻。")
                if action == "archive":
                    marker = ARCHIVE_PREFIX + row["processing_state"] + ":" + reason
                    cursor.execute("UPDATE news_items SET processing_state='skipped',skip_reason=%s WHERE id=%s",
                                   (marker[:64], article_id))
                else:
                    url, title, summary, source, date = edit_values(data)
                    key = sha256(url.encode()).hexdigest()
                    cursor.execute("SELECT id FROM news_items WHERE url_hash=%s AND id<>%s", (key, article_id))
                    if cursor.fetchone():
                        raise NewsConflict("这个原文链接已存在。")
                    cursor.execute("""UPDATE news_items SET title=%s,summary=%s,source=%s,url=%s,url_hash=%s,published_at=%s
                        WHERE id=%s""", (title, summary, source, url, key, date, article_id))
            cursor.execute("SELECT * FROM news_items WHERE id=%s", (article_id,))
            return cursor.fetchone()
