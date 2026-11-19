from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import sqlite3
import subprocess
import sys
from contextlib import asynccontextmanager, closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Literal
from urllib.parse import urlsplit

from pymysql.err import MySQLError
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .database import NewsDatabase

ROOT = Path(__file__).resolve().parents[1]
BOT = Path(os.environ.get("NEWS_BOT_HOME", ROOT.parent)).resolve()
sys.path.insert(0, str(BOT))
from .news_management import ARCHIVE_PATTERN, NewsConflict, change_news, edit_values, revision
from .related import EVENT_WINDOW, MAX_CANDIDATES, article_time, related_stories
from src.config import load_config, resolve_config_path
from src.diagnostics import failure_code
from src.fetcher import get_source_health
from src.pipeline import preview_digest
from src.reports import retry_report
from src.sender import delivery_payload
from src.editorial import PublicationConflict, article_summary, prepare_publication, publish, summarize
from src.storage import Storage
from src.automation import request_mode, request_stop, scheduler_status
from .settings import router as settings_router

logger = logging.getLogger("news_workbench")
runtime = Path(os.environ.get("NEWS_WORKBENCH_RUNTIME", ROOT / "runtime")).resolve()
shanghai = timezone(timedelta(hours=8))


@asynccontextmanager
async def lifespan(application: FastAPI):
    path = resolve_config_path(os.environ.get("NEWS_BOT_CONFIG") or BOT / "config.yaml")
    try:
        settings = await asyncio.to_thread(load_config, path)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("Cannot read bot configuration: %s", type(exc).__name__)
        raise RuntimeError("无法读取机器人配置。") from None
    database = NewsDatabase(settings.mysql)
    await asyncio.to_thread(initialize_drafts)
    await database.start()
    application.state.settings = settings
    application.state.config_path = path
    application.state.config_revision = None
    application.state.settings_lock = asyncio.Lock()
    application.state.probe_lock = asyncio.Lock()
    application.state.source_probe_lock = asyncio.Lock()
    application.state.last_probe = None
    application.state.preview = {"status": "idle"}
    application.state.preview_task = None
    application.state.article_jobs = set()
    application.state.collect_task = None
    application.state.collect_job = {"status": "idle"}
    application.state.report_job = {"status": "idle"}
    application.state.report_task = None
    application.state.editorial_storage = None
    application.state.editorial_lock = asyncio.Lock()
    application.state.automation_process = None
    application.state.database = database
    application.state.cache = {
        "summary": (asyncio.Lock(), None, 0.0),
        "sources": (asyncio.Lock(), None, 0.0),
    }
    try:
        yield
    finally:
        await database.close()


app = FastAPI(lifespan=lifespan, title="新闻工作台数据服务", docs_url=None, redoc_url=None, openapi_url=None)

async def authorize(x_workbench_key: str = Header(default="")):
    key = os.environ.get("NEWS_WORKBENCH_KEY", "")
    if not key or not hmac.compare_digest(x_workbench_key.encode(), key.encode()):
        raise HTTPException(403, "请通过本地工作台访问")

app.include_router(settings_router, dependencies=[Depends(authorize)])

@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, _error: RequestValidationError):
    # Pydantic's default response includes the invalid input, which may be a credential.
    return JSONResponse(status_code=422, content={"detail": "请求字段或取值无效，请检查输入。"})

@app.exception_handler(MySQLError)
@app.exception_handler(TimeoutError)
async def database_error(_request: Request, exc: Exception):
    logger.warning("MySQL request failed: %s", type(exc).__name__)
    return JSONResponse(status_code=503, content={"detail": "暂时无法读取新闻库，请检查本机 MySQL 和 机器人配置。"})

@app.exception_handler(sqlite3.Error)
async def local_store_error(_request: Request, exc: sqlite3.Error):
    logger.warning("Draft store request failed: %s", type(exc).__name__)
    return JSONResponse(status_code=503, content={"detail": "草稿库暂时不可用，请稍后重试。"})

def config():
    return app.state.settings


def news_db():
    return app.state.database.cursor()


async def cached(name, ttl, load, *, refresh=False):
    loop = asyncio.get_running_loop()
    cache = app.state.cache
    lock, _, _ = cache[name]
    requested = loop.time()
    async with lock:
        _, value, expires = cache[name]
        if value is not None and loop.time() < expires and (not refresh or expires - ttl >= requested):
            return value
        revision = app.state.config_revision
        value = await load()
        if revision == app.state.config_revision:
            cache[name] = (lock, value, loop.time() + ttl)
        return value


def iso(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()
    return value

def safe_url(value):
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() in {"https", "http"} and parts.hostname and not parts.username and not parts.password:
            return value
    except ValueError:
        pass
    return None

def article(row):
    return {**row, "url": safe_url(row["url"]), "summary": row.get("summary") or "",
            "title": row.get("title") or "未命名新闻", "source": row.get("source") or "来源未标注",
            "published_at": iso(row["published_at"]), "created_at": iso(row["created_at"]), "revision": revision(row)}

COLUMNS = "id,title,summary,source,url,processing_state,skip_reason,published_at,created_at"

@app.get("/alive", dependencies=[Depends(authorize)])
def alive():
    return {"ok": True}

@app.get("/health", dependencies=[Depends(authorize)])
async def health():
    async with news_db() as cursor:
        await cursor.execute("SELECT 1 AS connected")
        return {"ok": (await cursor.fetchone())["connected"] == 1, "source": "mysql", "mode": "local"}

@app.get("/summary", dependencies=[Depends(authorize)])
async def summary(refresh: bool = False):
    return await cached("summary", 3, load_summary, refresh=refresh)


async def load_summary():
    cfg = config()
    now = datetime.now(timezone.utc)
    today = now.astimezone(shanghai).date()
    last_week = datetime.combine(today - timedelta(days=6), datetime.min.time(), shanghai)
    bound = last_week.astimezone(timezone.utc).replace(tzinfo=None)
    earliest = (now - timedelta(hours=cfg.max_news_age_hours)).replace(tzinfo=None)
    latest_news = (now + timedelta(minutes=5)).replace(tzinfo=None)
    async with news_db() as cursor:
        await cursor.execute("""SELECT COUNT(*) AS total,
            COUNT(CASE WHEN processing_state='pending' AND
                       COALESCE(published_at,created_at) BETWEEN %s AND %s THEN 1 END) AS pending,
            COUNT(CASE WHEN (processing_state='pending' AND COALESCE(published_at,created_at)<%s)
                         OR (processing_state='skipped' AND skip_reason='outside_news_age') THEN 1 END) AS expired,
            COUNT(CASE WHEN processing_state='batched' THEN 1 END) AS batched,
            COUNT(CASE WHEN processing_state='skipped' THEN 1 END) AS skipped,
            MAX(created_at) AS last_ingested,
            (SELECT COUNT(*) FROM sent_logs) AS sent,
            (SELECT COUNT(*) FROM reports) AS reports
            FROM news_items WHERE COALESCE(skip_reason,'') NOT LIKE %s""", (earliest, latest_news, earliest, ARCHIVE_PATTERN))
        counts = await cursor.fetchone()
        await cursor.execute(f"SELECT {COLUMNS} FROM news_items WHERE COALESCE(skip_reason,'') NOT LIKE %s ORDER BY created_at DESC,id DESC LIMIT 6",
                             (ARCHIVE_PATTERN,))
        recent = [article(row) for row in await cursor.fetchall()]
        await cursor.execute("""SELECT DATE(created_at + INTERVAL 8 HOUR) AS day, COUNT(*) AS count
            FROM news_items WHERE created_at >= %s AND COALESCE(skip_reason,'') NOT LIKE %s GROUP BY day ORDER BY day""",
                             (bound, ARCHIVE_PATTERN))
        series = {row["day"]: row["count"] for row in await cursor.fetchall()}
        latest = counts["last_ingested"]
        archive = []
        if latest:
            end = latest.replace(tzinfo=timezone.utc).astimezone(shanghai).date()
            start = end - timedelta(days=6)
            archive_counts = series
            if end != today:
                from_utc = datetime.combine(start, datetime.min.time(), shanghai).astimezone(timezone.utc).replace(tzinfo=None)
                until_utc = datetime.combine(end + timedelta(days=1), datetime.min.time(), shanghai).astimezone(timezone.utc).replace(tzinfo=None)
                await cursor.execute("""SELECT DATE(created_at + INTERVAL 8 HOUR) AS day, COUNT(*) AS count
                    FROM news_items WHERE created_at >= %s AND created_at < %s AND COALESCE(skip_reason,'') NOT LIKE %s
                    GROUP BY day ORDER BY day""", (from_utc, until_utc, ARCHIVE_PATTERN))
                archive_counts = {row["day"]: row["count"] for row in await cursor.fetchall()}
            archive = [{"day":start+timedelta(days=i),"count":archive_counts.get(start+timedelta(days=i),0)} for i in range(7)]
    return {"counts": {**counts,"last_ingested":iso(counts["last_ingested"]), "sources":len(cfg.rss_sources)},
            "recent":recent,"series":[{"day":today-timedelta(days=6-i),"count":series.get(today-timedelta(days=6-i),0)} for i in range(7)],
            "archive":archive,
            "configuration":{"interval":cfg.fetch_interval_minutes,"start":cfg.push_time_window.start,
                              "end":cfg.push_time_window.end,"interest":cfg.ai_interests}}

@app.get("/news", dependencies=[Depends(authorize)])
async def news(q: str = Query("",max_length=150), state: str = Query("all"), source: str = Query("",max_length=128),
         page: int = Query(1,ge=1,le=10000), size: int = Query(12,ge=1,le=50)):
    clauses = ["skip_reason LIKE %s" if state == "archived" else "COALESCE(skip_reason, '') NOT LIKE %s"]
    args = [ARCHIVE_PATTERN]
    if state == "archived":
        pass
    elif state in {"fresh_pending","expired"}:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        earliest = now - timedelta(hours=config().max_news_age_hours)
        if state == "expired":
            clauses.append("""((processing_state='pending' AND COALESCE(published_at,created_at)<%s)
                               OR (processing_state='skipped' AND skip_reason='outside_news_age'))""")
            args.append(earliest)
        else:
            clauses.append("(processing_state='pending' AND COALESCE(published_at,created_at) BETWEEN %s AND %s)")
            args.extend((earliest, now + timedelta(minutes=5)))
    elif state != "all":
        if state not in {"pending","batched","sent","skipped"}:
            raise HTTPException(400,"未知新闻状态")
        clauses.append("processing_state=%s")
        args.append(state)
    if source:
        clauses.append("source=%s")
        args.append(source)
    if q.strip():
        clauses.append("(LOCATE(%s, title) > 0 OR LOCATE(%s, summary) > 0)")
        args.extend([q.strip(),q.strip()])
    conditions = " WHERE " + " AND ".join(clauses) if clauses else ""
    async with news_db() as cursor:
        await cursor.execute("SELECT COUNT(*) AS total FROM news_items"+conditions,tuple(args))
        total = (await cursor.fetchone())["total"]
        items = []
        offset = (page - 1) * size
        if offset < total:
            await cursor.execute(f"SELECT {COLUMNS} FROM news_items"+conditions+
                           " ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s",tuple(args+[size,offset]))
            items = [article(row) for row in await cursor.fetchall()]
    return {"items":items,"total":total,"page":page,"size":size}


@app.get("/news/{article_id}/related", dependencies=[Depends(authorize)])
async def news_related(article_id: int):
    async with news_db() as cursor:
        await cursor.execute(f"SELECT {COLUMNS} FROM news_items WHERE id=%s AND COALESCE(skip_reason,'') NOT LIKE %s",
                             (article_id, ARCHIVE_PATTERN))
        anchor = await cursor.fetchone()
        if anchor is None:
            raise HTTPException(404, "新闻不存在或已移至回收站。")
        published = article_time(anchor)
        if published is None:
            raise HTTPException(409, "该新闻缺少可用于比对的时间。")
        stamp = published.replace(tzinfo=None)
        start, end = stamp - EVENT_WINDOW, stamp + EVENT_WINDOW
        await cursor.execute(f"""SELECT {COLUMNS} FROM news_items
            WHERE id<>%s AND COALESCE(skip_reason,'') NOT LIKE %s
            AND (published_at BETWEEN %s AND %s OR (published_at IS NULL AND created_at BETWEEN %s AND %s))
            ORDER BY ABS(TIMESTAMPDIFF(SECOND, COALESCE(published_at,created_at), %s)), id DESC LIMIT %s""",
                             (article_id, ARCHIVE_PATTERN, start, end, start, end, stamp, MAX_CANDIDATES))
        candidates = await cursor.fetchall()
    matches = await asyncio.to_thread(related_stories, anchor, candidates)
    return {"items": [{"article": article(row), "reason": reason} for row, reason in matches]}


class NewsVersion(BaseModel):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class NewsInput(BaseModel):
    title: str = Field(min_length=1,max_length=512)
    summary: str = Field(default="",max_length=10000)
    url: str = Field(min_length=1,max_length=2048)
    source: str = Field(min_length=1,max_length=128)
    published_at: datetime | None = None


class NewsEdit(NewsInput, NewsVersion):
    pass


@app.post("/manage/news", status_code=201, dependencies=[Depends(authorize)])
async def insert_news(data: NewsInput):
    store = await editorial_storage(await editorial_config())
    try:
        url, title, text, source, published = edit_values(data.model_dump())
        inserted = await asyncio.to_thread(store.save_news,url,title,text,source,published)
    except ValueError:
        raise HTTPException(422,"请核对标题、来源、原文链接和发布时间。") from None
    if not inserted:
        raise HTTPException(409,"这个原文链接已在新闻库或回收站。")
    invalidate_news()
    return {"created": True}


def invalidate_news():
    for name, (lock, _, _) in app.state.cache.items():
        app.state.cache[name] = (lock, None, 0.0)


async def mutate_news(article_id, data, action):
    store = await editorial_storage(await editorial_config())
    try:
        row = await asyncio.to_thread(change_news, store, article_id, data.revision, action, data.model_dump())
    except NewsConflict as error:
        raise HTTPException(409,str(error)) from None
    except ValueError:
        raise HTTPException(422,"请核对原文链接和发布时间。") from None
    if row is None:
        raise HTTPException(404,"新闻不存在。")
    invalidate_news()
    return article({key: row[key] for key in COLUMNS.split(",")})


@app.put("/manage/news/{article_id}", dependencies=[Depends(authorize)])
async def edit_news(article_id: int, data: NewsEdit):
    return await mutate_news(article_id, data, "edit")


@app.delete("/manage/news/{article_id}", dependencies=[Depends(authorize)])
async def archive_news(article_id: int, data: NewsVersion):
    return await mutate_news(article_id, data, "archive")


@app.post("/manage/news/{article_id}/restore", dependencies=[Depends(authorize)])
async def restore_news(article_id: int, data: NewsVersion):
    return await mutate_news(article_id, data, "restore")

@app.get("/sources", dependencies=[Depends(authorize)])
async def sources(refresh: bool = False, include_counts: bool = True):
    if not include_counts:
        return [{"name": source.name} for source in config().rss_sources]
    return await cached("sources", 5, load_sources, refresh=refresh)


async def source_counts():
    since = (datetime.now(timezone.utc) - timedelta(days=7)).replace(tzinfo=None)
    async with news_db() as cursor:
        await cursor.execute("""SELECT source, COUNT(*) AS count, MAX(created_at) AS latest,
            COUNT(CASE WHEN created_at >= %s THEN 1 END) AS recent_count,
            COUNT(CASE WHEN created_at >= %s AND processing_state = 'sent' THEN 1 END) AS recent_sent
            FROM news_items WHERE COALESCE(skip_reason,'') NOT LIKE %s GROUP BY source""",
                             (since, since, ARCHIVE_PATTERN))
        return {row["source"]: row for row in await cursor.fetchall()}


def source_health():
    path = Path(app.state.config_path).parent / "runtime" / "source-health.sqlite3"
    try:
        return get_source_health(path)
    except sqlite3.Error:
        logger.warning("Source health database is unavailable")
        return {}


async def load_sources():
    counts, observed = await asyncio.gather(source_counts(), asyncio.to_thread(source_health))
    results = []
    for source in config().rss_sources:
        news = counts.get(source.name, {})
        health = observed.get(source.name, {})
        results.append({
            "name": source.name, "url": safe_url(source.url),
            "count": news.get("count", 0), "latest": iso(news.get("latest")),
            "recent_count": news.get("recent_count", 0), "recent_sent": news.get("recent_sent", 0),
            "checked": health.get("last_attempt"), "error": health.get("last_error"),
            "entries": health.get("entries"), "returned": health.get("returned"),
            "successes": health.get("success"), "failures": health.get("fail"),
        })
    return results

@app.get("/batches", dependencies=[Depends(authorize)])
async def batches():
    async with news_db() as cursor:
        await cursor.execute("""SELECT b.id,b.status,b.news_count,b.error_code,b.created_at,b.updated_at,
            (SELECT COUNT(*) FROM delivery_attempts a WHERE a.batch_id=b.id) AS attempts
            ,(SELECT COUNT(*) FROM delivery_attempts a WHERE a.batch_id=b.id AND a.status='failed') AS failures
            FROM delivery_batches b ORDER BY
                CASE WHEN b.status='unknown' THEN 0 ELSE 1 END, b.created_at DESC,b.id DESC LIMIT 100""")
        return [{**row,"created_at":iso(row["created_at"]),"updated_at":iso(row["updated_at"])} for row in await cursor.fetchall()]

@app.get("/batches/{batch_id}", dependencies=[Depends(authorize)])
async def batch_detail(batch_id: str):
    async with news_db() as cursor:
        await cursor.execute("SELECT id,status,news_count,error_code,analysis_text FROM delivery_batches WHERE id=%s",(batch_id,))
        row = await cursor.fetchone()
        if row is not None:
            await cursor.execute("""SELECT status,error_code,started_at,finished_at FROM delivery_attempts
                WHERE batch_id=%s ORDER BY started_at DESC LIMIT 100""", (batch_id,))
            attempts = [{key: iso(value) for key, value in item.items()} for item in await cursor.fetchall()]
    if row is None:
        raise HTTPException(404, "批次不存在")
    try:
        analysis = json.loads(row.pop("analysis_text") or "")
    except ValueError:
        analysis = {}
    valid = analysis if isinstance(analysis, dict) else {}
    valid.pop("_destination", None)
    preview = delivery_payload(valid, row["news_count"]) if valid else None
    if preview:
        preview = {**preview, "recorded": isinstance(valid.get("_delivery"), dict)}
    return {**row,"analysis":valid,"attempts":attempts,"preview":preview}


async def editorial_config():
    settings = await asyncio.to_thread(load_config, app.state.config_path)
    if settings.mysql != app.state.database.settings:
        raise HTTPException(409, "数据库连接配置已变化，请重启工作台再操作。")
    return settings


async def editorial_storage(settings):
    async with app.state.editorial_lock:
        if app.state.editorial_storage is None:
            app.state.editorial_storage = await asyncio.to_thread(Storage, settings.mysql)
        return app.state.editorial_storage


async def selected_articles(ids):
    async with news_db() as cursor:
        slots = ','.join(['%s'] * len(ids))
        await cursor.execute(f"SELECT {COLUMNS} FROM news_items WHERE id IN ({slots})", tuple(ids))
        rows = {row["id"]: row for row in await cursor.fetchall()}
    if len(rows) != len(ids):
        raise HTTPException(409, "部分新闻已移除，请重新选稿。")
    return [rows[article_id] for article_id in ids]


@app.get("/editorial/summary/{article_id}", dependencies=[Depends(authorize)])
async def read_article_summary(article_id: int):
    settings = await editorial_config()
    row, = await selected_articles([article_id])
    result = await asyncio.to_thread(article_summary, settings, row)
    return {"status": "running", "summary": None} if article_id in app.state.article_jobs else result


async def _summarize_article(settings, row):
    try:
        await asyncio.to_thread(summarize, settings, [row], force=True)
    except Exception as error:
        logger.warning("Article summary failed (%s)", type(error).__name__)
    finally:
        app.state.article_jobs.discard(row["id"])


@app.post("/editorial/summary/{article_id}", status_code=202, dependencies=[Depends(authorize)])
async def create_article_summary(article_id: int):
    settings = await editorial_config()
    if not settings.ai.api_key:
        raise HTTPException(409, "请先配置 AI 密钥。")
    row, = await selected_articles([article_id])
    if article_id not in app.state.article_jobs:
        if len(app.state.article_jobs) >= 2:
            raise HTTPException(429, "摘要任务正在处理，请稍后重试。")
        app.state.article_jobs.add(article_id)
        asyncio.create_task(_summarize_article(settings, row))
    return {"status": "running", "summary": None}


class PublicationPreview(BaseModel):
    article_ids: list[int] = Field(min_length=1, max_length=20)
    headline: str = Field(default="", max_length=160)


class PublicationSend(BaseModel):
    id: str = Field(pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
    confirmed: bool = Field(strict=True)


@app.post("/editorial/preview", dependencies=[Depends(authorize)])
async def editorial_preview(data: PublicationPreview):
    settings = await editorial_config()
    ids = list(dict.fromkeys(data.article_ids))
    rows = await selected_articles(ids)
    try:
        return await asyncio.to_thread(prepare_publication, settings, rows, " ".join(data.headline.split()))
    except PublicationConflict as error:
        raise HTTPException(409, str(error)) from None
    except ValueError:
        raise HTTPException(422, "选稿中有失效的原文地址。") from None


@app.post("/editorial/send", dependencies=[Depends(authorize)])
async def editorial_send(data: PublicationSend):
    if not data.confirmed:
        raise HTTPException(422, "请核对预览后确认发送。")
    settings = await editorial_config()
    storage = await editorial_storage(settings)
    try:
        result = await asyncio.to_thread(publish, settings, storage, data.id)
    except PublicationConflict as error:
        raise HTTPException(409, str(error)) from None
    except Exception as error:
        logger.warning("Publication status needs verification (%s)", type(error).__name__)
        raise HTTPException(503, "投递结果暂不可确认，请先检查推送批次。") from None
    if result["status"] == "busy":
        raise HTTPException(409, "后台任务正在处理新闻，稍后可使用本次预览发送。")
    return result


async def _collect_editorial(settings):
    try:
        from main import run_once
        storage = await editorial_storage(settings)
        result = await asyncio.to_thread(run_once, config=settings, storage=storage, fetch_only=True, enrich=True)
        app.state.collect_job = {k: result[k] for k in (
            "status", "message", "fetched", "inserted", "errors", "summaries", "stages", "model_usage"
        ) if k in result}
    except Exception as error:
        logger.warning("Manual collection failed (%s)", type(error).__name__)
        app.state.collect_job = {"status": "failed", "message": "采集失败，请检查数据库和来源配置。"}


@app.get("/editorial/collect", dependencies=[Depends(authorize)])
async def collect_status():
    return app.state.collect_job


@app.post("/editorial/collect", status_code=202, dependencies=[Depends(authorize)])
async def collect_editorial():
    settings = await editorial_config()
    if app.state.collect_task is not None and not app.state.collect_task.done():
        raise HTTPException(409, "已经有一次手动采集正在运行。")
    app.state.collect_job = {"status": "running"}
    app.state.collect_task = asyncio.create_task(_collect_editorial(settings))
    return app.state.collect_job


class AutomationCommand(BaseModel):
    action: str = Field(pattern=r"^(collect|full|stop)$")
    confirmed: bool = False


@app.post("/editorial/automation", status_code=202, dependencies=[Depends(authorize)])
async def automation_command(data: AutomationCommand):
    if data.action == "full" and not data.confirmed:
        raise HTTPException(422, "请确认开启自动分析和钉钉推送。")
    path = app.state.config_path
    async with app.state.editorial_lock:
        status = await asyncio.to_thread(scheduler_status, path)
        if data.action == "stop":
            await asyncio.to_thread(request_stop, path)
        elif status["active"]:
            if not await asyncio.to_thread(request_mode, path, data.action):
                raise HTTPException(409, "后台正在启动或停止，请等状态稳定后重试。")
        else:
            worker = app.state.automation_process
            if worker is None or worker.poll() is not None:
                args = [sys.executable, "-B", str(BOT / "main.py"), "--config", path]
                if data.action == "collect":
                    args.append("--scheduler-collect")
                env = {key: value for key, value in os.environ.items() if key != "NEWS_WORKBENCH_KEY"}
                flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                app.state.automation_process = await asyncio.to_thread(
                    subprocess.Popen, args, cwd=BOT, env=env,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=flags, start_new_session=os.name != "nt",
                )
    return {"accepted": True}


async def _prepare_preview(settings):
    try:
        async with news_db() as cursor:
            await cursor.execute(f"SELECT {COLUMNS} FROM news_items WHERE processing_state='pending' ORDER BY id LIMIT 1000")
            rows = await cursor.fetchall()
            titles = []
            if settings.incremental_mode:
                await cursor.execute("SELECT title FROM news_items WHERE processing_state='sent' AND created_at>=%s",
                                     ((datetime.now(timezone.utc)-timedelta(days=7)).replace(tzinfo=None),))
                titles = [row["title"] for row in await cursor.fetchall()]
        app.state.preview = await asyncio.to_thread(preview_digest, settings, rows, titles, datetime.now(timezone.utc))
    except Exception as error:
        logger.warning("Preview generation failed (%s)", type(error).__name__)
        app.state.preview = {"status": "failed", "message": "预览生成失败，请检查模型连接或后台日志。"}


@app.get("/preview", dependencies=[Depends(authorize)])
async def preview_status():
    return app.state.preview


@app.post("/preview", status_code=202, dependencies=[Depends(authorize)])
async def start_preview():
    if app.state.preview_task and not app.state.preview_task.done():
        raise HTTPException(409, "已有简报预览正在生成。")
    if config().mysql != app.state.database.settings:
        raise HTTPException(409, "请重启工作台以应用当前数据库配置。")
    app.state.preview = {"status": "running"}
    app.state.preview_task = asyncio.create_task(_prepare_preview(config()))
    return app.state.preview

@app.get("/reports", dependencies=[Depends(authorize)])
async def reports():
    async with news_db() as cursor:
        await cursor.execute("""SELECT id,title,report_type,period_start,period_end,news_count,created_at
            FROM reports ORDER BY period_end DESC,id DESC LIMIT 100""")
        return [{**row,"created_at":iso(row["created_at"])} for row in await cursor.fetchall()]


class ReportPeriod(BaseModel):
    report_type: Literal["weekly", "monthly", "quarterly", "yearly"]
    period_start: date
    period_end: date


@app.get("/report-jobs", dependencies=[Depends(authorize)])
async def report_jobs():
    async with news_db() as cursor:
        await cursor.execute("""SELECT report_type,period_start,period_end,status,error_code,updated_at
            FROM report_periods WHERE status IN ('failed','generating')
            ORDER BY updated_at DESC,period_start DESC LIMIT 50""")
        items = [{**row, "updated_at": iso(row["updated_at"])} for row in await cursor.fetchall()]
    return {"items": items, "job": app.state.report_job}


async def _retry_report(settings, period, job):
    began = monotonic()
    try:
        store = await editorial_storage(settings)
        outcome = await asyncio.to_thread(retry_report, settings, store, period.report_type,
                                          period.period_start, period.period_end)
    except Exception as error:
        outcome = {"status": "failed", "code": failure_code(error)}
        logger.warning("Report retry failed (%s)", outcome["code"])
    app.state.report_job = {**job, **outcome, "duration_ms": round((monotonic() - began) * 1000)}


@app.post("/report-jobs/retry", status_code=202, dependencies=[Depends(authorize)])
async def rerun_report(period: ReportPeriod):
    settings = await editorial_config()
    if not settings.ai.api_key:
        raise HTTPException(409, "请先配置 AI 密钥。")
    if app.state.report_task and not app.state.report_task.done():
        raise HTTPException(409, "已有一期报告正在重跑。")
    job = {**period.model_dump(mode="json"), "status": "running",
           "started_at": datetime.now(timezone.utc).isoformat()}
    app.state.report_job = job
    app.state.report_task = asyncio.create_task(_retry_report(settings, period, job))
    return job


@app.get("/reports/{report_id}", dependencies=[Depends(authorize)])
async def report_detail(report_id: int):
    async with news_db() as cursor:
        await cursor.execute("SELECT id,title,content FROM reports WHERE id=%s",(report_id,))
        row=await cursor.fetchone()
    if row is None:
        raise HTTPException(404,"报告不存在")
    try:
        content=json.loads(row.pop("content") or "{}")
    except ValueError:
        content={}
    return {**row,"content":content if isinstance(content,dict) else {}}

def initialize_drafts():
    runtime.mkdir(parents=True, exist_ok=True)
    with draft_db() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("""CREATE TABLE IF NOT EXISTS drafts (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
            article_ids TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, archived_at TEXT)""")
        if "archived_at" not in {row["name"] for row in connection.execute("PRAGMA table_info(drafts)")}:
            connection.execute("ALTER TABLE drafts ADD COLUMN archived_at TEXT")


@contextmanager
def draft_db():
    with closing(sqlite3.connect(runtime / "drafts.sqlite3", timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            yield connection


def draft_row(row):
    if row is None:
        raise HTTPException(404,"草稿不存在")
    item=dict(row)
    item["article_ids"]=json.loads(item["article_ids"])
    return item

class DraftCreate(BaseModel):
    article_ids: list[int] = Field(default_factory=list,max_length=20)

class DraftUpdate(BaseModel):
    title: str = Field(min_length=1,max_length=160)
    body: str = Field(max_length=25000)
    revision: int = Field(ge=1)


class DraftVersion(BaseModel):
    revision: int = Field(ge=1)

def single_line(value):
    return " ".join(value.split())

@app.get("/drafts", dependencies=[Depends(authorize)])
def drafts(archived: bool = False):
    with draft_db() as connection:
        condition = "IS NOT NULL" if archived else "IS NULL"
        rows = connection.execute(f"""SELECT id,title,article_ids,updated_at,revision,archived_at FROM drafts
            WHERE archived_at {condition} ORDER BY updated_at DESC,id DESC LIMIT 100""")
        return [draft_row(row) for row in rows]

@app.get("/drafts/{draft_id}", dependencies=[Depends(authorize)])
def get_draft(draft_id: int):
    with draft_db() as connection:
        return draft_row(connection.execute("SELECT * FROM drafts WHERE id=?",(draft_id,)).fetchone())

@app.post("/drafts", status_code=201, dependencies=[Depends(authorize)])
async def create_draft(data: DraftCreate):
    ids=list(dict.fromkeys(data.article_ids))
    rows=[]
    if ids:
        async with news_db() as cursor:
            await cursor.execute(f"SELECT {COLUMNS} FROM news_items WHERE id IN ({','.join(['%s']*len(ids))})"
                                 " AND COALESCE(skip_reason,'') NOT LIKE %s", (*ids, ARCHIVE_PATTERN))
            found={row["id"]:article(row) for row in await cursor.fetchall()}
        if len(found)!=len(ids):
            raise HTTPException(409,"部分新闻已不在新闻库，请刷新后重试。")
        rows=[found[i] for i in ids]
    title="新闻简报草稿"
    sections=[]
    for number,row in enumerate(rows,1):
        url = row["url"] or "暂无原文链接"
        sections.append(f"## {number}. {single_line(row['title'])}\n\n{single_line(row['summary'])}\n\n来源：{single_line(row['source'])}\n原文：{url}")
    body="\n\n".join(sections)
    return await asyncio.to_thread(insert_draft, title, body, ids)


def insert_draft(title, body, ids):
    with draft_db() as connection:
        cursor=connection.execute("INSERT INTO drafts(title,body,article_ids,updated_at) VALUES(?,?,?,?)",
                                  (title,body,json.dumps(ids),datetime.now(timezone.utc).isoformat()))
        return draft_row(connection.execute("SELECT * FROM drafts WHERE id=?",(cursor.lastrowid,)).fetchone())

@app.put("/drafts/{draft_id}", dependencies=[Depends(authorize)])
def update_draft(draft_id: int, data: DraftUpdate):
    if not data.title.strip():
        raise HTTPException(422,"请输入简报标题")
    with draft_db() as connection:
        cursor=connection.execute("""UPDATE drafts SET title=?,body=?,updated_at=?,revision=revision+1
            WHERE id=? AND revision=? AND archived_at IS NULL""",(data.title.strip(),data.body,datetime.now(timezone.utc).isoformat(),draft_id,data.revision))
        row=connection.execute("SELECT * FROM drafts WHERE id=?",(draft_id,)).fetchone()
        if cursor.rowcount!=1 and row is not None:
            raise HTTPException(409,"草稿已经更新，请重新打开后再保存。")
        return draft_row(row)


def change_draft_archive(draft_id, expected, archive):
    condition = "IS NULL" if archive else "IS NOT NULL"
    now = datetime.now(timezone.utc).isoformat()
    with draft_db() as connection:
        updated = connection.execute(f"""UPDATE drafts SET archived_at=?, updated_at=?, revision=revision+1
            WHERE id=? AND revision=? AND archived_at {condition}""",
            (now if archive else None, now, draft_id, expected))
        if not updated.rowcount:
            if not connection.execute("SELECT 1 FROM drafts WHERE id=?",(draft_id,)).fetchone():
                raise HTTPException(404,"草稿不存在。")
            raise HTTPException(409,"简报状态已更新，请刷新后再操作。")
    return Response(status_code=204)


@app.delete("/drafts/{draft_id}", status_code=204, dependencies=[Depends(authorize)])
def archive_draft(draft_id: int, data: DraftVersion):
    return change_draft_archive(draft_id, data.revision, True)


@app.post("/drafts/{draft_id}/restore", status_code=204, dependencies=[Depends(authorize)])
def restore_draft(draft_id: int, data: DraftVersion):
    return change_draft_archive(draft_id, data.revision, False)
