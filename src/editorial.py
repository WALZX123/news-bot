"""Article reading aids and explicit publication, backed by the same delivery ledger."""
from contextlib import closing, contextmanager
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from . import analyzer, sender
from .diagnostics import failure_code
from .storage import db_time, utc
from .url_safety import normalize_http_url


def _workspace(config):
    db = config.mysql
    return sha256(f"{db.host}:{db.port}/{db.database}".encode()).hexdigest()


@contextmanager
def editorial_db(config):
    path = Path(config.config_path).parent / "runtime" / "editorial.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=5)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS summaries (
            workspace TEXT, article INTEGER, fingerprint TEXT, state TEXT, summary TEXT,
            lease REAL, owner TEXT, updated REAL, PRIMARY KEY(workspace,article))""")
        db.execute("""CREATE TABLE IF NOT EXISTS manual_previews (
            id TEXT PRIMARY KEY, workspace TEXT, payload TEXT, expires REAL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS summary_evidence (
            workspace TEXT, article INTEGER, fingerprint TEXT, quotes TEXT NOT NULL,
            PRIMARY KEY(workspace,article))""")
        yield db


def _fingerprint(row):
    return sha256(json.dumps([row["url"], row["title"], row.get("summary")], ensure_ascii=False).encode()).hexdigest()


def _publication_fingerprint(row):
    return sha256(json.dumps([_fingerprint(row), row["source"]], ensure_ascii=False).encode()).hexdigest()


def article_summary(config, row):
    with editorial_db(config) as db:
        saved = db.execute("""SELECT s.state,s.summary,s.lease,e.quotes FROM summaries s
            LEFT JOIN summary_evidence e ON e.workspace=s.workspace AND e.article=s.article
                AND e.fingerprint=s.fingerprint
            WHERE s.workspace=? AND s.article=? AND s.fingerprint=?""",
                           (_workspace(config), row["id"], _fingerprint(row))).fetchone()
    if not saved:
        return {"status": "pending", "summary": None}
    status, summary, lease, quotes = saved
    result = {"status": "failed" if status == "running" and lease <= time.time() else status, "summary": summary}
    if status == "ready" and quotes:
        result["evidence"] = json.loads(quotes)
    return result


def summarize(config, rows, *, force=False, metrics=None):
    if not config.ai.api_key:
        return 0
    workspace = _workspace(config)
    owner = uuid4().hex
    claimed = []
    with editorial_db(config) as db:
        for row in rows:
            if not (row["title"] or "").strip() and not (row["summary"] or "").strip():
                continue
            stamp = time.time()
            cursor = db.execute("""INSERT INTO summaries VALUES (?,?,?,'running',NULL,?,?,?)
                ON CONFLICT(workspace,article) DO UPDATE SET fingerprint=excluded.fingerprint,
                state='running',summary=CASE WHEN summaries.fingerprint=excluded.fingerprint
                    THEN summaries.summary ELSE NULL END,lease=excluded.lease,owner=excluded.owner,updated=excluded.updated
                WHERE summaries.fingerprint!=excluded.fingerprint
                   OR (summaries.state!='ready' AND
                       (summaries.lease<=? OR (? AND summaries.state='failed')))
                   OR (? AND summaries.state='ready' AND NOT EXISTS (
                       SELECT 1 FROM summary_evidence WHERE workspace=summaries.workspace
                         AND article=summaries.article AND fingerprint=summaries.fingerprint))""",
                (workspace, row["id"], _fingerprint(row), stamp + config.ai.total_timeout_seconds + 30,
                 owner, stamp, stamp, force, force))
            if cursor.rowcount:
                claimed.append(row)
    if metrics is not None:
        metrics["attempted"] = len(claimed)
    written = 0
    with analyzer.model_budget(config.ai):
        for index in range(0, len(claimed), 4):
            group = claimed[index:index + 4]
            try:
                result = analyzer.summarize_articles(group, config.ai)
            except analyzer.ModelResponseError as error:
                result = {}
                if metrics is not None:
                    metrics.setdefault("error_codes", []).append(failure_code(error))
            with editorial_db(config) as db:
                for row in group:
                    article = result.get(row["id"])
                    text = article["summary"] if article else None
                    change = db.execute("""UPDATE summaries SET state=?,summary=COALESCE(?,summary),lease=?,updated=?
                         WHERE workspace=? AND article=? AND owner=?""",
                         ("ready" if text else "failed", text, time.time() + 900 if not text else 0,
                          time.time(), workspace, row["id"], owner))
                    if article and change.rowcount:
                        db.execute("""INSERT INTO summary_evidence VALUES (?,?,?,?)
                            ON CONFLICT(workspace,article) DO UPDATE SET
                                fingerprint=excluded.fingerprint,quotes=excluded.quotes""",
                                   (workspace, row["id"], _fingerprint(row), json.dumps(article["evidence"], ensure_ascii=False)))
                        written += 1
    if metrics is not None:
        metrics["failed"] = len(claimed) - written
    return written


def summarize_recent(config, storage, *, metrics=None):
    with storage.cursor(dictionary=True) as cursor:
        cursor.execute("""SELECT id,title,summary,url FROM news_items
            WHERE COALESCE(published_at,created_at)>= %s
              AND COALESCE(skip_reason,'') NOT LIKE %s ORDER BY created_at DESC,id DESC LIMIT 240""",
            (db_time(utc() - timedelta(hours=config.max_news_age_hours)), "operator_archived:%"))
        articles = cursor.fetchall()
    if not articles or not config.ai.api_key:
        return 0
    with editorial_db(config) as db:
        slots = ",".join("?" for _ in articles)
        snapshots = {article: (fingerprint, state, lease) for article, fingerprint, state, lease in
                     db.execute(f"""SELECT article,fingerprint,state,lease FROM summaries
                          WHERE workspace=? AND article IN ({slots})""",
                                (_workspace(config), *(row["id"] for row in articles)))}
    pending = []
    now = time.time()
    for row in articles:
        saved = snapshots.get(row["id"])
        if not saved or saved[0] != _fingerprint(row) or (saved[1] != "ready" and saved[2] <= now):
            pending.append(row)
        if len(pending) == config.summary_limit:
            break
    return summarize(config, pending, metrics=metrics)


class PublicationConflict(RuntimeError):
    pass


def prepare_publication(config, rows, headline):
    docs = []
    for row in rows:
        if row["processing_state"] not in {"pending", "skipped"} or (row.get("skip_reason") or "").startswith("operator_archived:"):
            raise PublicationConflict("选稿中有新闻已经组批或投递，请刷新后重新选择。")
        url = normalize_http_url(row["url"])
        reading = article_summary(config, row)
        cached = reading["summary"] if reading.get("evidence") else None
        docs.append({"title": row["title"][:200], "brief": (cached or row["summary"] or "")[:220],
                     "url": url, "source": row["source"]})
    analysis = {"headline": headline, "categories": {"手动选稿": docs}, "_publication_mode": "manual"}
    payload = sender.delivery_payload(analysis, len(rows))
    if payload["truncated"] or payload["visible_links"] != len(rows):
        raise PublicationConflict("这份简报超出投递长度或含重复原文，请减少选稿后重新预览。")
    analysis["_delivery"] = payload
    analysis["_destination"] = sha256(config.dingtalk.webhook_url.encode()).hexdigest()
    record = {"ids": [value["id"] for value in rows], "analysis": analysis,
              "versions": {str(value["id"]): _publication_fingerprint(value) for value in rows}}
    identifier = str(uuid4())
    with editorial_db(config) as db:
        db.execute("DELETE FROM manual_previews WHERE expires<?", (time.time(),))
        db.execute("INSERT INTO manual_previews VALUES (?,?,?,?)",
                   (identifier, _workspace(config), json.dumps(record, ensure_ascii=False), time.time() + 3600))
    return {"id": identifier, "count": len(rows), **payload}


def publish(config, storage, identifier):
    with storage.pipeline_lock() as acquired:
        if not acquired:
            return {"status": "busy", "batch_id": None}
        storage.ensure_runtime_ready()
        existing = storage.get_batch(identifier)
        if existing and (existing["analysis"] or {}).get("_publication_mode") != "manual":
            raise PublicationConflict("批次身份不匹配，请重新生成预览。")
        if existing and existing["status"] in {"sent", "unknown", "sending"}:
            if existing["status"] == "sending":
                storage.recover_inflight()
            return {"status": "unknown" if existing["status"] == "sending" else existing["status"], "batch_id": identifier}
        if existing:
            record = {"ids": [item["id"] for item in existing["items"]], "analysis": existing["analysis"]}
        else:
            with editorial_db(config) as db:
                row = db.execute("SELECT payload FROM manual_previews WHERE id=? AND workspace=? AND expires>=?",
                                 (identifier, _workspace(config), time.time())).fetchone()
            if not row:
                raise PublicationConflict("预览已过期，请重新预览后发送。")
            record = json.loads(row[0])
        destination = sha256(config.dingtalk.webhook_url.encode()).hexdigest()
        if not config.dingtalk.webhook_url or record["analysis"].get("_destination") != destination:
            raise PublicationConflict("钉钉接收方未配置或已经改变，请核对配置并重新预览。")
        if not existing:
            now = utc()
            slots = ",".join(["%s"] * len(record["ids"]))
            with storage.cursor(dictionary=True) as cursor:
                cursor.execute(f"SELECT id,url,title,summary,source FROM news_items WHERE id IN ({slots})",
                               tuple(record["ids"]))
                current = {str(row["id"]): _publication_fingerprint(row) for row in cursor.fetchall()}
            if current != record.get("versions") or len(current) != len(record["ids"]):
                raise PublicationConflict("选稿内容已更新，请重新预览后发送。")
            try:
                storage.create_batch(record["ids"], now, manual_id=identifier, analysis=record["analysis"])
            except RuntimeError as error:
                raise PublicationConflict("选稿状态已改变，请刷新新闻并重新预览。") from error
        storage.assert_pipeline_lock()
        attempt = storage.begin_delivery(identifier, manual=True)
        storage.assert_pipeline_lock()
        try:
            accepted = sender.send_to_dingtalk(record["analysis"], len(record["ids"]), config.dingtalk)
            outcome = "succeeded" if accepted else "failed"
        except sender.DeliveryUnknownError:
            outcome = "unknown"
        storage.finish_delivery(identifier, attempt, outcome, manual=True)
        return {"status": "sent" if outcome == "succeeded" else outcome, "batch_id": identifier}
