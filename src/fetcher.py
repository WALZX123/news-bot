"""Bounded RSS collection with pinned addresses and durable source health.

Each source runs in a killable child including DNS, headers, streaming and XML
parsing. The parent enforces a shared deadline. RSS connects directly rather
than using environment proxies that could re-resolve a checked hostname.
"""
import calendar
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import html
import http.client
import logging
import multiprocessing
import os
from pathlib import Path
import re
import socket
import sqlite3
import time
from urllib.parse import urljoin, urlsplit

import feedparser
from .config import RSSSource
from .url_safety import UnsafeURLError, normalize_http_url, resolve_network_addresses

logger = logging.getLogger('news_bot.fetcher')
USER_AGENT = 'NewsBot/1.1 (RSS-only; Personal-Learning)'
_rounds = 0


class FetchError(RuntimeError):
    """Safe category-only failure, without a URL or credential."""

    def __init__(self, message, *, code="rss_failed"):
        super().__init__(message)
        self.code = code


class FetchBatch(list):
    """A compatible article list with the observed source failure count."""
    def __init__(self, items=(), *, failures=0, failed_sources=()):
        super().__init__(items)
        self.failures = failures
        self.failed_sources = list(failed_sources)


def _health_path(path=None):
    if path or os.environ.get('NEWS_BOT_HEALTH_DB'):
        return Path(path or os.environ['NEWS_BOT_HEALTH_DB']).resolve()
    configured = os.environ.get('NEWS_BOT_CONFIG')
    root = Path(configured).resolve().parent if configured else Path(__file__).resolve().parents[1]
    return root / 'runtime' / 'source-health.sqlite3'


def get_source_health(health_path=None):
    path = _health_path(health_path)
    if not path.exists():
        return {}
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_health'").fetchone():
            return {}
        return {row['name']: {key: row[key] for key in row.keys() if key != 'name'}
                for row in conn.execute('SELECT * FROM source_health')}


def _record_health(source, metrics, now, health_path):
    path = _health_path(health_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    success = int(not metrics['error'])
    with closing(sqlite3.connect(path, timeout=2)) as conn, conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS source_health (
            name TEXT PRIMARY KEY, success INTEGER NOT NULL, fail INTEGER NOT NULL,
            empty INTEGER NOT NULL, request_success INTEGER NOT NULL, parse_success INTEGER NOT NULL,
            last_error TEXT NOT NULL, last_attempt TEXT NOT NULL, returned INTEGER NOT NULL,
            entries INTEGER NOT NULL)''')
        conn.execute('''INSERT INTO source_health VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET success=success+excluded.success,
            fail=fail+excluded.fail, empty=empty+excluded.empty,
            request_success=request_success+excluded.request_success,
            parse_success=parse_success+excluded.parse_success,
            last_error=excluded.last_error, last_attempt=excluded.last_attempt,
            returned=excluded.returned, entries=excluded.entries''',
            (source.name, success, 1-success, int(metrics['parsed'] and not metrics['entries']),
             int(metrics['requested']), int(metrics['parsed']), metrics['error'], now.isoformat(),
             len(metrics['items']), metrics['entries']))


@dataclass
class NewsItem:
    title: str
    url: str
    summary: str
    source: str
    published_at: datetime | None = None
    source_count: int = 1
    source_weight: int = 1
    interest_score: int = 5

    def to_dict(self):
        return {'title': self.title, 'url': self.url, 'summary': self.summary, 'source': self.source,
                'published_at': self.published_at.isoformat() if self.published_at else '',
                'source_count': self.source_count}


def _parse_date(struct_time):
    try:
        return datetime.fromtimestamp(calendar.timegm(struct_time), timezone.utc) if struct_time else None
    except (TypeError, ValueError, OverflowError):
        return None


def _utc(now):
    value = now or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _read_feed(source, timeout, allow_private_hosts, max_response_bytes, deadline, metrics):
    url = source.url
    for hop in range(6):
        if time.monotonic() >= deadline:
            raise TimeoutError()
        url, addresses = resolve_network_addresses(url, allow_private_hosts)
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        remaining = max(.01, min(timeout, deadline - time.monotonic()))
        connection_type = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
        connection = connection_type(parsed.hostname, port, timeout=remaining)
        # HTTPSConnection wraps this socket with the original server_hostname,
        # preserving SNI and certificate checks without resolving it again.
        def pinned_connection(address, timeout=remaining, source_address=None, **kwargs):
            for ip in addresses:
                try:
                    return socket.create_connection((ip, port), timeout, source_address)
                except OSError:
                    if ip == addresses[-1]:
                        raise
        connection._create_connection = pinned_connection
        try:
            connection.request('GET', parsed.path + ('?' + parsed.query if parsed.query else ''),
                               headers={'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader('Location')
                if not location or hop == 5:
                    raise FetchError('redirect')
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise FetchError('http_status')
            metrics['requested'] = True
            content_type = response.getheader('Content-Type', '').split(';', 1)[0].strip().lower()
            if content_type and content_type not in {'application/rss+xml', 'application/atom+xml', 'text/xml',
                    'application/xml', 'application/rdf+xml', 'text/plain', 'application/octet-stream'}:
                raise FetchError('content_type')
            if response.getheader('Content-Encoding', 'identity').lower() not in {'', 'identity'}:
                raise FetchError('content_encoding')
            declared = response.getheader('Content-Length')
            if declared is not None:
                try:
                    if not 0 <= int(declared) <= max_response_bytes:
                        raise FetchError('byte_limit')
                except ValueError:
                    raise FetchError('content_length') from None
            chunks, count = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError()
                chunk = response.read1(min(16384, max_response_bytes - count + 1))
                if not chunk:
                    break
                count += len(chunk)
                if count > max_response_bytes:
                    raise FetchError('byte_limit')
                chunks.append(chunk)
            if declared is not None and count != int(declared):
                raise FetchError('content_length')
            body = b''.join(chunks)
            if b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
                raise FetchError('xml_declaration')
            return feedparser.parse(body)
        finally:
            connection.close()
    raise FetchError('redirect')


def _source_worker(pipe, source, timeout, allow_private_hosts, max_news_age_hours,
                   max_response_bytes, deadline, now):
    metrics = dict(requested=False, parsed=False, entries=0, error='', items=[])
    try:
        feed = _read_feed(source, timeout, allow_private_hosts, max_response_bytes, deadline, metrics)
        if not feed.version or feed.bozo:
            raise FetchError('invalid_feed')
        metrics.update(parsed=True, entries=len(feed.entries))
        seen = set()
        for entry in feed.entries[:10]:
            title = html.unescape(re.sub(r'<[^>]+>', '', entry.get('title', ''))).strip()[:1000]
            try:
                url = normalize_http_url(entry.get('link', '').strip())
            except UnsafeURLError:
                continue
            published = _parse_date(entry.get('published_parsed')) or _parse_date(entry.get('updated_parsed'))
            if published and (published < now - timedelta(hours=max_news_age_hours)
                              or published > now + timedelta(minutes=5)):
                continue
            if not title or url in seen:
                continue
            summary = html.unescape(re.sub(r'<[^>]+>', '', entry.get('summary', entry.get('description', '')))).strip()[:500]
            metrics['items'].append(NewsItem(title, url, summary, source.name, published))
            seen.add(url)
    except Exception as exc:
        metrics['error'] = ('url_policy' if isinstance(exc, UnsafeURLError) else
                            'timeout' if isinstance(exc, (TimeoutError, socket.timeout)) else
                            str(exc) if isinstance(exc, FetchError) else 'transport_or_parse')
        metrics['items'] = []
    try:
        pipe.send(metrics)
    finally:
        pipe.close()


def fetch_source(source: RSSSource, timeout=15, *, allow_private_hosts=None,
                 max_news_age_hours=72, max_response_bytes=2097152, task_timeout=120,
                 now=None, health_path=None, _deadline=None):
    """Fetch one source with a hard deadline; a valid empty feed is successful."""
    if (not isinstance(source, RSSSource) or not isinstance(source.name, str)
            or not source.name.strip() or len(source.name) > 128):
        raise ValueError('RSS source must have a name')
    for value, low, high in ((timeout, .01, 120), (task_timeout, .01, 600),
                            (max_news_age_hours, 1, 8760), (max_response_bytes, 1, 10485760)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise ValueError('RSS resource limit is invalid')
    now = _utc(now)
    deadline = min(_deadline or float('inf'), time.monotonic() + task_timeout)
    metrics = dict(requested=False, parsed=False, entries=0, error='timeout', items=[])
    context = multiprocessing.get_context('spawn')
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_source_worker, args=(send, source, timeout, allow_private_hosts,
        max_news_age_hours, max_response_bytes, deadline, now), daemon=True)
    try:
        if time.monotonic() < deadline:
            process.start()
            send.close()
            if receive.poll(max(0, deadline - time.monotonic())):
                try:
                    metrics = receive.recv()
                except EOFError:
                    metrics['error'] = 'worker_failed'
    finally:
        send.close()
        receive.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=.25)
            if process.is_alive():
                process.kill()
                process.join(timeout=.25)
            process.close()
    try:
        _record_health(source, metrics, now, health_path)
    except (OSError, sqlite3.Error):
        logger.error('RSS source health persistence failed')
        raise FetchError('RSS source health persistence failed', code="health_storage") from None
    if metrics['error']:
        logger.warning('RSS source failed (%s)', metrics['error'])
        raise FetchError('RSS source failed: ' + metrics['error'], code=metrics['error'])
    return metrics['items']


def _fetch_single(source, timeout):
    try:
        return fetch_source(source, timeout)
    except FetchError:
        return []


def fetch_all(sources, timeout=15, *, allow_private_hosts=None, max_news_age_hours=72,
              max_response_bytes=2097152, task_timeout=120, now=None, health_path=None):
    global _rounds
    if not sources:
        return FetchBatch()
    if len(sources) > 100:
        raise ValueError('At most 100 RSS sources are allowed')
    deadline = time.monotonic() + task_timeout
    executor = ThreadPoolExecutor(max_workers=min(len(sources), 10))
    futures = {executor.submit(fetch_source, source, timeout, allow_private_hosts=allow_private_hosts,
        max_news_age_hours=max_news_age_hours, max_response_bytes=max_response_bytes,
        task_timeout=task_timeout, now=now, health_path=health_path, _deadline=deadline): index
        for index, source in enumerate(sources)}
    batches, errors = {}, {}
    try:
        for future in as_completed(futures, timeout=task_timeout + .6):
            try:
                batches[futures[future]] = future.result()
            except FetchError as error:
                errors[futures[future]] = error.code
    except FuturesTimeout:
        logger.warning('RSS batch deadline exceeded')
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    by_url, source_sets = {}, {}
    for index in sorted(batches):
        for item in batches[index]:
            by_url.setdefault(item.url, item)
            source_sets.setdefault(item.url, set()).add(item.source)
    for url, item in by_url.items():
        item.source_count = len(source_sets[url])
    _rounds += 1
    logger.info('RSS batch completed: %d unique articles', len(by_url))
    if _rounds % 10 == 0:
        _print_health_report(health_path)
    failures = [{"source": source.name, "code": errors.get(index, "timeout")}
                for index, source in enumerate(sources) if index not in batches]
    return FetchBatch(by_url.values(), failures=len(failures), failed_sources=failures)


def _print_health_report(health_path=None):
    health = get_source_health(health_path)
    logger.info('RSS health: %d sources, %d successes, %d failures, %d valid empty feeds',
                len(health), sum(row['success'] for row in health.values()),
                sum(row['fail'] for row in health.values()), sum(row['empty'] for row in health.values()))
