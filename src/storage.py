"""MySQL repository; DDL is an explicit administrative operation only.

Instants are UTC; MySQL DATETIME stores naive UTC. Legacy records survive
additive migrations. Interrupted sends require reconciliation, never auto-resend.
"""
import hashlib
import json
import logging
import re
import uuid
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import mysql.connector
from mysql.connector import pooling
from .config import MySQLConfig

logger = logging.getLogger('news_bot.storage')
UTC = timezone.utc


def utc(value=None):
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time())
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def db_time(value=None):
    return utc(value).replace(tzinfo=None)


def json_ready(value):
    if isinstance(value, datetime):
        return utc(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(v) for v in value]
    return value


CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS news_items (
 id INT AUTO_INCREMENT PRIMARY KEY, url_hash VARCHAR(64) NOT NULL UNIQUE,
 url VARCHAR(2048) NOT NULL, title VARCHAR(512) DEFAULT '', summary TEXT,
 source VARCHAR(128) DEFAULT '', published_at DATETIME NULL,
 notified TINYINT(1) NOT NULL DEFAULT 0,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 processing_state VARCHAR(24) NOT NULL DEFAULT 'pending',
 skip_reason VARCHAR(64) DEFAULT '', batch_id VARCHAR(36) NULL,
 INDEX idx_created_at(created_at), INDEX idx_pending(processing_state, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS sent_logs (
 id INT AUTO_INCREMENT PRIMARY KEY, sent_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 news_count INT NOT NULL DEFAULT 0, analysis_text LONGTEXT, sentiment VARCHAR(255) DEFAULT '',
 batch_id VARCHAR(36) NULL, UNIQUE KEY uq_sent_batch(batch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS reports (
 id INT AUTO_INCREMENT PRIMARY KEY, report_type VARCHAR(16) NOT NULL,
 period_start DATE NOT NULL, period_end DATE NOT NULL, title VARCHAR(256) DEFAULT '',
 content LONGTEXT, news_count INT NOT NULL DEFAULT 0,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 INDEX idx_report_type(report_type), INDEX idx_period(period_start,period_end)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS delivery_batches (
 id VARCHAR(36) PRIMARY KEY, status VARCHAR(24) NOT NULL,
 analysis_text LONGTEXT NULL, news_count INT NOT NULL DEFAULT 0,
 error_code VARCHAR(64) DEFAULT '', created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS batch_items (
 batch_id VARCHAR(36) NOT NULL, news_id INT NOT NULL,
 PRIMARY KEY(batch_id,news_id), UNIQUE KEY uq_news_batch(news_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS delivery_attempts (
 id VARCHAR(36) PRIMARY KEY, batch_id VARCHAR(36) NOT NULL,
 status VARCHAR(24) NOT NULL, error_code VARCHAR(64) DEFAULT '',
 started_at DATETIME NOT NULL, finished_at DATETIME NULL, INDEX idx_attempt_batch(batch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS report_periods (
 report_type VARCHAR(16) NOT NULL, period_start DATE NOT NULL,
 period_end DATE NOT NULL, status VARCHAR(24) NOT NULL,
 report_id INT NULL, error_code VARCHAR(64) DEFAULT '', updated_at DATETIME NOT NULL,
 PRIMARY KEY(report_type,period_start,period_end)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS task_jobs (
 id VARCHAR(36) PRIMARY KEY, kind VARCHAR(24) NOT NULL,
 status VARCHAR(24) NOT NULL, result_text LONGTEXT NULL,
 created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, INDEX idx_job_status(status,updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


@contextmanager
def connection_cursor(conn, dictionary=False, write=False):
    cursor = None
    try:
        cursor = conn.cursor(dictionary=dictionary)
        yield cursor
        if write:
            conn.commit()
    except BaseException:
        if write:
            try:
                conn.rollback()
            except Exception:
                logger.error('Database rollback failed')
        raise
    finally:
        try:
            if cursor is not None:
                cursor.close()
        finally:
            conn.close()


class Storage:
    def __init__(self, config: MySQLConfig, *, connection_factory=None):
        self.config = config
        self._factory = connection_factory
        self._local = threading.local()
        self._lock_namespace = 'newsbot:' + hashlib.sha256(
            f'{config.host}:{config.port}/{config.database}'.encode()).hexdigest()[:40]
        self._pool = None
        if connection_factory is None:
            self._pool = pooling.MySQLConnectionPool(
                pool_name='newsbot_' + uuid.uuid4().hex[:12], pool_size=5,
                pool_reset_session=True, host=config.host, port=config.port,
                user=config.user, password=config.password, database=config.database,
                charset=config.charset, autocommit=False, use_pure=True,
                time_zone='+00:00', connection_timeout=10)

    def _get_connection(self):
        return self._factory() if self._factory else self._pool.get_connection()

    @contextmanager
    def cursor(self, *, dictionary=False, write=False):
        with connection_cursor(self._get_connection(), dictionary, write) as cur:
            yield cur

    @classmethod
    def initialize(cls, config, *, legacy_timezone=None):
        if not re.fullmatch(r'[A-Za-z0-9_]{1,64}', config.database):
            raise ValueError('Invalid MySQL database identifier')
        conn = mysql.connector.connect(host=config.host, port=config.port,
            user=config.user, password=config.password, charset=config.charset,
            use_pure=True, connection_timeout=10, time_zone='+00:00')
        with connection_cursor(conn, write=True) as cur:
            cur.execute(f'CREATE DATABASE IF NOT EXISTS `{config.database}` '
                        'CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
        store = cls(config)
        with store.pipeline_lock() as running, store.pipeline_lock('schema') as acquired:
            if not running:
                raise RuntimeError('Cannot migrate while a pipeline is running')
            if not acquired:
                raise RuntimeError('Database migration is already running')
            from .migrations import prepare_legacy_timezone, finalize_legacy_timezone
            prepare_legacy_timezone(store,legacy_timezone)
            store._init_tables()
            finalize_legacy_timezone(store)
        return store

    def _init_tables(self):
        with self.cursor(write=True) as cur:
            for sql in CREATE_TABLES_SQL.split(';'):
                if sql.strip():
                    cur.execute(sql.strip())
            added_state = self._add_column_if_missing(cur, 'news_items', 'processing_state',
                                                      "VARCHAR(24) NOT NULL DEFAULT 'pending'")
            self._add_column_if_missing(cur, 'news_items', 'notified', 'TINYINT(1) NOT NULL DEFAULT 0')
            self._add_column_if_missing(cur, 'news_items', 'skip_reason', "VARCHAR(64) DEFAULT ''")
            self._add_column_if_missing(cur, 'news_items', 'batch_id', 'VARCHAR(36) NULL')
            self._add_column_if_missing(cur, 'sent_logs', 'sentiment', "VARCHAR(255) DEFAULT ''")
            self._add_column_if_missing(cur, 'sent_logs', 'batch_id', 'VARCHAR(36) NULL')
            cur.execute("UPDATE news_items SET processing_state='sent' WHERE notified=1 AND processing_state='pending'")
            self._add_index(cur, 'news_items', 'idx_pending', '(processing_state,id)')
            self._add_index(cur, 'sent_logs', 'uq_sent_batch', '(batch_id)', unique=True)
            # Unique ledger preserves all legacy report rows, even duplicates.
            cur.execute("INSERT IGNORE INTO report_periods "
                        "(report_type,period_start,period_end,status,report_id,updated_at) "
                        "SELECT report_type,period_start,period_end,'succeeded',MIN(id),UTC_TIMESTAMP() "
                        "FROM reports GROUP BY report_type,period_start,period_end")

    @staticmethod
    def _add_column_if_missing(cur, table, column, definition):
        cur.execute('SELECT COUNT(*) FROM information_schema.columns '
                    'WHERE table_schema=DATABASE() AND table_name=%s AND column_name=%s', (table, column))
        if cur.fetchone()[0]:
            return False
        cur.execute(f'ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}')
        return True

    @staticmethod
    def _add_index(cur, table, name, columns, unique=False):
        cur.execute('SELECT COUNT(*) FROM information_schema.statistics '
                    'WHERE table_schema=DATABASE() AND table_name=%s AND index_name=%s', (table, name))
        if not cur.fetchone()[0]:
            cur.execute(f'ALTER TABLE `{table}` ADD {"UNIQUE " if unique else ""}INDEX `{name}` {columns}')

    @contextmanager
    def pipeline_lock(self, purpose='pipeline'):
        conn, cur, acquired = self._get_connection(), None, False
        try:
            cur = conn.cursor()
            cur.execute('SELECT GET_LOCK(%s, 0)', (self._lock_namespace + ':' + purpose,))
            acquired = cur.fetchone()[0] == 1
            if acquired:
                owners = getattr(self._local, 'owners', {})
                owners[purpose] = (cur, self._lock_namespace + ':' + purpose)
                self._local.owners = owners
            yield acquired
        finally:
            getattr(self._local, 'owners', {}).pop(purpose, None)
            try:
                if acquired and cur is not None:
                    try:
                        cur.execute('SELECT RELEASE_LOCK(%s)', (self._lock_namespace + ':' + purpose,))
                        cur.fetchone()
                    except Exception:
                        logger.error('Pipeline lock connection was lost; no automatic reconnect')
            finally:
                try:
                    if cur is not None:
                        cur.close()
                finally:
                    conn.close()

    def assert_pipeline_lock(self, purpose='pipeline'):
        owner = getattr(self._local, 'owners', {}).get(purpose)
        if owner is None:
            raise RuntimeError('Pipeline lock is required')
        cur, name = owner
        cur.execute('SELECT IS_USED_LOCK(%s) = CONNECTION_ID()', (name,))
        row = cur.fetchone()
        if not row or row[0] != 1:
            raise RuntimeError('Pipeline lock ownership was lost')

    def save_news(self, url, title, summary, source, published_at=None, notified=False, *, now=None):
        if not isinstance(url,str) or len(url)>2048 or not isinstance(source,str) or len(source)>128:
            raise ValueError('News URL/source exceeds storage limits')
        with self.cursor(write=True) as cur:
            cur.execute('INSERT INTO news_items '
                '(url_hash,url,title,summary,source,published_at,notified,processing_state,created_at) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE url_hash=url_hash',
                (hashlib.sha256(url.encode()).hexdigest(), url, title[:512], summary, source[:128],
                 db_time(published_at) if published_at else None, int(notified),
                 'sent' if notified else 'pending', db_time(now)))
            return cur.rowcount == 1

    def ingest_news(self, items, *, now=None):
        inserted = 0
        with self.cursor(write=True) as cur:
            for item in items:
                if len(item.url)>2048 or len(item.source)>128:
                    raise ValueError('News URL/source exceeds storage limits')
                cur.execute('INSERT INTO news_items '
                    '(url_hash,url,title,summary,source,published_at,notified,processing_state,created_at) '
                    "VALUES (%s,%s,%s,%s,%s,%s,0,'pending',%s) ON DUPLICATE KEY UPDATE url_hash=url_hash",
                    (hashlib.sha256(item.url.encode()).hexdigest(), item.url, item.title[:512],
                     item.summary, item.source[:128], db_time(item.published_at) if item.published_at else None,
                     db_time(now)))
                inserted += int(cur.rowcount == 1)
        return inserted

    def filter_new_urls(self, urls):
        urls = list(dict.fromkeys(urls))
        if not urls:
            return []
        existing = set()
        with self.cursor(dictionary=True) as cur:
            for offset in range(0, len(urls), 500):
                hashes = [hashlib.sha256(u.encode()).hexdigest() for u in urls[offset:offset+500]]
                cur.execute('SELECT url_hash FROM news_items WHERE url_hash IN (' +
                            ','.join(['%s'] * len(hashes)) + ')', hashes)
                existing.update(r['url_hash'] for r in cur.fetchall())
        return [u for u in urls if hashlib.sha256(u.encode()).hexdigest() not in existing]

    def is_duplicate(self, url):
        return not self.filter_new_urls([url])

    def get_pending_news(self, limit=1000):
        with self.cursor(dictionary=True) as cur:
            cur.execute("SELECT * FROM news_items WHERE processing_state='pending' ORDER BY id LIMIT %s",
                        (min(max(int(limit), 1), 1000),))
            return cur.fetchall()

    def skip_news(self, ids, reason):
        if ids:
            with self.cursor(write=True) as cur:
                cur.execute("UPDATE news_items SET processing_state='skipped',skip_reason=%s "
                            "WHERE processing_state='pending' AND id IN (" + ','.join(['%s'] * len(ids)) + ')',
                            [reason[:64]] + list(ids))

    def get_recent_sent_titles(self, now=None):
        with self.cursor(dictionary=True) as cur:
            cur.execute("SELECT title FROM news_items WHERE processing_state='sent' AND created_at >= %s",
                        (db_time(utc(now)-timedelta(days=7)),))
            return [row['title'] for row in cur.fetchall()]

    def create_batch(self, ids, now=None, *, manual_id=None, analysis=None):
        batch = manual_id or str(uuid.uuid4())
        status = 'manual_ready' if manual_id else 'analyzing'
        serialized = json.dumps(json_ready(analysis), ensure_ascii=False) if analysis else None
        with self.cursor(write=True) as cur:
            cur.execute('INSERT INTO delivery_batches '
                        '(id,status,news_count,created_at,updated_at,analysis_text) VALUES (%s,%s,%s,%s,%s,%s)',
                        (batch, status, len(ids), db_time(now), db_time(now), serialized))
            for news_id in ids:
                cur.execute('INSERT INTO batch_items (batch_id,news_id) VALUES (%s,%s)', (batch, news_id))
                cur.execute("UPDATE news_items SET processing_state='batched',batch_id=%s "
                            "WHERE id=%s AND (processing_state='pending' OR "
                            "(%s=1 AND processing_state='skipped' AND batch_id IS NULL AND COALESCE(skip_reason,'') NOT LIKE %s))",
                            (batch, news_id, int(bool(manual_id)), "operator_archived:%"))
                if cur.rowcount != 1:
                    raise RuntimeError('Candidate was already claimed')
        return self.get_batch(batch)

    def get_batch(self, batch_id):
        with self.cursor(dictionary=True) as cur:
            cur.execute('SELECT * FROM delivery_batches WHERE id=%s', (batch_id,))
            batch = cur.fetchone()
            if batch:
                cur.execute('SELECT n.* FROM news_items n JOIN batch_items b ON n.id=b.news_id '
                            'WHERE b.batch_id=%s ORDER BY n.id', (batch_id,))
                batch['items'] = cur.fetchall()
                batch['analysis'] = json.loads(batch['analysis_text']) if batch['analysis_text'] else None
            return batch

    def get_retry_batch(self, *, now=None, lease_seconds=300):
        clock = utc(now)
        with self.cursor(dictionary=True) as cur:
            cur.execute("""SELECT b.id FROM delivery_batches b
                LEFT JOIN (SELECT batch_id, COUNT(*) AS failures FROM delivery_attempts
                           WHERE status='failed' GROUP BY batch_id) a ON a.batch_id=b.id
                WHERE b.status='ready' OR (b.status='analyzing' AND b.updated_at<%s)
                   OR (b.status='analysis_failed' AND (
                       (b.error_code IN ('analysis_failed','analysis_failed_1') AND b.updated_at<=%s)
                       OR (b.error_code='analysis_failed_2' AND b.updated_at<=%s)))
                   OR (b.status='send_failed' AND (
                       b.error_code='manual_reconciliation'
                       OR (COALESCE(a.failures,0)<2 AND b.updated_at<=%s)
                       OR (a.failures=2 AND b.updated_at<=%s)))
                ORDER BY b.created_at,b.id LIMIT 1""",
                tuple(db_time(clock-timedelta(seconds=seconds)) for seconds in
                      (lease_seconds, 300, 1200, 300, 1200)))
            row = cur.fetchone()
        return self.get_batch(row['id']) if row else None

    def retry_should_yield(self, batch_id):
        with self.cursor() as cur:
            cur.execute("SELECT id FROM delivery_batches ORDER BY updated_at DESC,id DESC LIMIT 1")
            latest = cur.fetchone()
            return bool(latest and latest[0] == batch_id)

    def resize_analysis_batch(self, batch_id, keep_ids, now=None):
        """Repack an unsent oversized batch; unselected rows remain pending."""
        keep_ids=list(dict.fromkeys(keep_ids))
        with self.cursor(write=True) as cur:
            cur.execute('SELECT status,analysis_text,error_code FROM delivery_batches WHERE id=%s', (batch_id,))
            row=cur.fetchone()
            if not row or row[0] not in ('analyzing','analysis_failed') or row[1] is not None:
                raise RuntimeError('Only unanalyzed batches may be resized')
            cur.execute('SELECT news_id FROM batch_items WHERE batch_id=%s', (batch_id,))
            original={r[0] for r in cur.fetchall()}
            if not set(keep_ids)<=original:
                raise ValueError('Replacement batch IDs must be a subset')
            released=original-set(keep_ids)
            for news_id in released:
                cur.execute("UPDATE news_items SET processing_state='pending',batch_id=NULL "
                            "WHERE id=%s AND batch_id=%s AND processing_state='batched'", (news_id,batch_id))
                cur.execute('DELETE FROM batch_items WHERE batch_id=%s AND news_id=%s', (batch_id,news_id))
            cur.execute("UPDATE delivery_batches SET news_count=%s,status=%s,error_code=%s,updated_at=%s "
                        "WHERE id=%s AND status IN ('analyzing','analysis_failed')",
                        (len(keep_ids), row[0] if keep_ids else 'canceled',
                         row[2] if keep_ids else 'repacked_budget',db_time(now),batch_id))
            if cur.rowcount!=1:
                raise RuntimeError('Batch changed while resizing')
        return self.get_batch(batch_id) if keep_ids else None

    def set_batch_analysis(self, batch_id, analysis, now=None, *, included_ids=None):
        with self.cursor(write=True) as cur:
            cur.execute("UPDATE delivery_batches SET status='ready',analysis_text=%s,error_code='',updated_at=%s "
                        "WHERE id=%s AND status IN ('analyzing','analysis_failed')",
                        (json.dumps(json_ready(analysis), ensure_ascii=False), db_time(now), batch_id))
            if cur.rowcount != 1:
                raise RuntimeError('Analysis worker no longer owns an eligible batch')
            if included_ids is not None:
                cur.execute('SELECT news_id FROM batch_items WHERE batch_id=%s', (batch_id,))
                claimed = {row[0] for row in cur.fetchall()}
                visible = set(included_ids)
                if not visible or not visible <= claimed:
                    raise ValueError('Published articles must be a non-empty subset of the batch')
                for news_id in claimed - visible:
                    cur.execute("UPDATE news_items SET processing_state='pending',batch_id=NULL "
                                "WHERE id=%s AND batch_id=%s AND processing_state='batched'", (news_id, batch_id))
                    if cur.rowcount != 1:
                        raise RuntimeError('A batch article changed before it could be released')
                    cur.execute('DELETE FROM batch_items WHERE batch_id=%s AND news_id=%s', (batch_id, news_id))
                cur.execute('UPDATE delivery_batches SET news_count=%s WHERE id=%s', (len(visible), batch_id))

    def fail_analysis(self, batch_id, now=None):
        with self.cursor(write=True) as cur:
            cur.execute("UPDATE delivery_batches SET status='analysis_failed',error_code=CASE error_code "
                        "WHEN 'analysis_failed' THEN 'analysis_failed_2' WHEN 'analysis_failed_1' THEN 'analysis_failed_2' "
                        "WHEN 'analysis_failed_2' THEN 'analysis_failed_3' WHEN 'analysis_failed_3' THEN 'analysis_failed_3' "
                        "ELSE 'analysis_failed_1' END,"
                        "updated_at=%s WHERE id=%s AND status IN ('analyzing','analysis_failed')", (db_time(now), batch_id))

    def begin_delivery(self, batch_id, now=None, *, manual=False):
        from .sender import delivery_payload
        attempt = str(uuid.uuid4())
        with self.cursor(write=True) as cur:
            eligible = ('manual_ready', 'manual_send_failed') if manual else ('ready', 'send_failed')
            cur.execute("SELECT analysis_text,news_count FROM delivery_batches WHERE id=%s AND status IN (%s,%s)",
                        (batch_id, *eligible))
            row = cur.fetchone()
            if not row:
                raise RuntimeError('Batch is not eligible for sending')
            analysis = json.loads(row[0])
            analysis['_delivery'] = delivery_payload(analysis, row[1])
            cur.execute("UPDATE delivery_batches SET status='sending',updated_at=%s,analysis_text=%s "
                        "WHERE id=%s AND status IN (%s,%s)",
                        (db_time(now), json.dumps(analysis, ensure_ascii=False), batch_id, *eligible))
            if cur.rowcount != 1:
                raise RuntimeError('Batch is not eligible for sending')
            cur.execute('INSERT INTO delivery_attempts (id,batch_id,status,started_at) VALUES (%s,%s,%s,%s)',
                        (attempt, batch_id, 'started', db_time(now)))
        return attempt

    def finish_delivery(self, batch_id, attempt_id, status, *, now=None, manual=False):
        if status not in ('succeeded', 'failed', 'unknown'):
            raise ValueError('Invalid delivery outcome')
        batch_status = {'succeeded':'sent', 'failed':'send_failed', 'unknown':'unknown'}[status]
        if manual and status == 'failed':
            batch_status = 'manual_send_failed'
        with self.cursor(write=True) as cur:
            cur.execute("SELECT status FROM delivery_attempts WHERE id=%s AND batch_id=%s",
                        (attempt_id,batch_id))
            active = cur.fetchone()
            if not active or active[0] != 'started':
                raise RuntimeError('Delivery attempt is not active for this batch')
            cur.execute("UPDATE delivery_batches SET status=%s,error_code=%s,updated_at=%s "
                        "WHERE id=%s AND status='sending'",
                        (batch_status, '' if status=='succeeded' else status, db_time(now), batch_id))
            if cur.rowcount != 1:
                raise RuntimeError('Delivery outcome already recorded')
            cur.execute('UPDATE delivery_attempts SET status=%s,error_code=%s,finished_at=%s WHERE id=%s',
                        (status, '' if status=='succeeded' else status, db_time(now), attempt_id))
            if status == 'succeeded':
                cur.execute('UPDATE news_items SET notified=1,processing_state=%s WHERE batch_id=%s', ('sent', batch_id))
                cur.execute('SELECT analysis_text,news_count FROM delivery_batches WHERE id=%s', (batch_id,))
                row = cur.fetchone()
                analysis = json.loads(row[0])
                cur.execute('INSERT INTO sent_logs (sent_at,news_count,analysis_text,sentiment,batch_id) '
                            'VALUES (%s,%s,%s,%s,%s)',
                            (db_time(now), row[1], row[0], str(analysis.get('sentiment',''))[:255], batch_id))

    def recover_inflight(self, now=None):
        # Only under pipeline_lock, so any sending record belongs to a dead run.
        with self.cursor(write=True) as cur:
            cur.execute("UPDATE delivery_batches SET status='unknown',error_code='interrupted_send',updated_at=%s "
                        "WHERE status='sending'", (db_time(now),))
            cur.execute("UPDATE delivery_attempts SET status='unknown',error_code='interrupted_send',finished_at=%s "
                        "WHERE status='started'", (db_time(now),))

    def log_sent(self, news_count, analysis_text, sentiment=''):
        with self.cursor(write=True) as cur:
            cur.execute('INSERT INTO sent_logs (sent_at,news_count,analysis_text,sentiment) VALUES (%s,%s,%s,%s)',
                        (db_time(), news_count, analysis_text, sentiment[:255]))

    def get_last_sentiment(self):
        with self.cursor() as cur:
            cur.execute("SELECT sentiment FROM sent_logs WHERE sentiment!='' ORDER BY sent_at DESC LIMIT 1")
            row = cur.fetchone()
            return row[0] if row else ''

    def get_unnotified_count(self):
        with self.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM news_items WHERE processing_state IN ('pending','batched')")
            return cur.fetchone()[0]

    def mark_all_notified(self):
        raise RuntimeError('Bulk acknowledgement removed; acknowledge an explicit delivery batch')

    def get_news_for_period(self, start_date, end_date, limit=2000):
        start, end = db_time(start_date), db_time(end_date)
        count = self.count_news_for_period(start, end)
        limit = min(max(1, int(limit)), 2000)
        with self.cursor(dictionary=True) as cur:
            sql = ('SELECT id,title,url,summary,source,published_at,created_at FROM news_items '
                   'WHERE COALESCE(published_at,created_at)>=%s AND COALESCE(published_at,created_at)<%s '
                   'ORDER BY COALESCE(published_at,created_at),id')
            if count <= limit:
                cur.execute(sql + ' LIMIT %s', (start, end, limit))
                return json_ready(cur.fetchall())
            positions = [1+round(i*(count-1)/(limit-1)) for i in range(limit)] if limit>1 else [1+count//2]
            cur.execute('SELECT id,title,url,summary,source,published_at,created_at FROM '
                        '(SELECT id,title,url,summary,source,published_at,created_at, '
                        'ROW_NUMBER() OVER (ORDER BY COALESCE(published_at,created_at),id) AS rownum '
                        'FROM news_items WHERE COALESCE(published_at,created_at)>=%s '
                        'AND COALESCE(published_at,created_at)<%s) AS numbered '
                        'WHERE rownum IN (' + ','.join(['%s']*len(positions)) + ') ORDER BY rownum',
                        [start,end]+positions)
            return json_ready(cur.fetchall())

    def count_news_for_period(self, start_date, end_date):
        with self.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM news_items WHERE COALESCE(published_at,created_at)>=%s '
                        'AND COALESCE(published_at,created_at)<%s', (db_time(start_date), db_time(end_date)))
            return cur.fetchone()[0]

    def get_last_report_date(self, report_type):
        with self.cursor() as cur:
            cur.execute("SELECT MAX(period_end) FROM report_periods WHERE report_type=%s AND status='succeeded'", (report_type,))
            row = cur.fetchone()
            return row[0] if row else None

    def get_first_news_time(self):
        with self.cursor() as cur:
            cur.execute('SELECT MIN(COALESCE(published_at,created_at)) FROM news_items')
            row = cur.fetchone()
            return utc(row[0]) if row and row[0] is not None else None

    def get_completed_report_periods(self, report_type):
        with self.cursor() as cur:
            cur.execute("SELECT period_start,period_end FROM report_periods "
                        "WHERE report_type=%s AND status='succeeded'", (report_type,))
            return {(date.fromisoformat(str(a)), date.fromisoformat(str(b))) for a,b in cur.fetchall()}

    def get_report_period(self, report_type, start, end):
        with self.cursor(dictionary=True) as cur:
            cur.execute("SELECT status,error_code,report_id FROM report_periods WHERE report_type=%s "
                        "AND period_start=%s AND period_end=%s", (report_type, start, end))
            return cur.fetchone()

    def claim_report(self, report_type, start, end, now=None, *, lease_seconds=300):
        with self.cursor(write=True) as cur:
            cur.execute('INSERT IGNORE INTO report_periods '
                        '(report_type,period_start,period_end,status,updated_at) VALUES (%s,%s,%s,%s,%s)',
                        (report_type, start, end, 'generating', db_time(now)))
            if cur.rowcount == 1:
                return True
            cur.execute('SELECT status,updated_at FROM report_periods WHERE report_type=%s AND period_start=%s AND period_end=%s',
                        (report_type, start, end))
            row = cur.fetchone()
            if row and row[0]=='succeeded':
                return False
            if row and row[0]=='generating' and utc(row[1])>=utc(now)-timedelta(seconds=lease_seconds):
                return False
            cur.execute("UPDATE report_periods SET status='generating',error_code='',updated_at=%s WHERE report_type=%s "
                        'AND period_start=%s AND period_end=%s', (db_time(now), report_type, start, end))
            return True

    def fail_report(self, report_type, start, end, now=None, *, error_code='report_failed'):
        with self.cursor(write=True) as cur:
            cur.execute("UPDATE report_periods SET status='failed',error_code=%s,updated_at=%s "
                        "WHERE report_type=%s AND period_start=%s AND period_end=%s AND status='generating'",
                        (error_code[:64], db_time(now), report_type, start, end))

    def save_report(self, report_type, period_start, period_end, title, content, news_count, *, now=None):
        with self.cursor(write=True) as cur:
            cur.execute('SELECT status FROM report_periods WHERE report_type=%s AND period_start=%s AND period_end=%s FOR UPDATE',
                        (report_type,period_start,period_end))
            current=cur.fetchone()
            if not current or current[0]!='generating':
                raise RuntimeError('Report period is no longer eligible for completion')
            cur.execute('INSERT INTO reports (report_type,period_start,period_end,title,content,news_count,created_at) '
                        'VALUES (%s,%s,%s,%s,%s,%s,%s)',
                        (report_type, period_start, period_end, title[:256], content, news_count, db_time(now)))
            report_id = cur.lastrowid
            cur.execute("UPDATE report_periods SET status='succeeded',report_id=%s,error_code='',updated_at=%s "
                        'WHERE report_type=%s AND period_start=%s AND period_end=%s',
                        (report_id, db_time(now), report_type, period_start, period_end))
        return report_id

    def cleanup_old(self, days=400, *, now=None):
        if days<400:
            raise ValueError('Retention must cover at least 400 days')
        cutoff = db_time(utc(now)-timedelta(days=days))
        with self.cursor(write=True) as cur:
            cur.execute("DELETE FROM news_items WHERE created_at<%s AND processing_state IN ('sent','skipped') "
                        "AND (batch_id IS NULL OR batch_id IN (SELECT id FROM delivery_batches WHERE status='sent'))", (cutoff,))
            deleted = cur.rowcount
            cur.execute("DELETE FROM task_jobs WHERE updated_at<%s AND status NOT IN ('queued','running')", (cutoff,))
            return deleted

    def recover_jobs(self, *, now=None):
        with self.cursor(write=True) as cur:
            cur.execute("UPDATE task_jobs SET status='recovery_required',result_text=%s "
                        "WHERE status IN ('queued','running') AND updated_at<%s",
                        (json.dumps({'message':'任务租约已过期，请核对任务及发送批次状态'}), db_time(utc(now)-timedelta(seconds=900))))
            return cur.rowcount

    def create_job(self, kind, *, now=None):
        with self.pipeline_lock('jobs') as acquired:
            if not acquired:
                return None
            self.recover_jobs(now=now)
            with self.cursor(write=True) as cur:
                cur.execute("SELECT COUNT(*) FROM task_jobs WHERE status IN ('queued','running')")
                if cur.fetchone()[0]>=2:
                    return None
                job_id = str(uuid.uuid4())
                cur.execute('INSERT INTO task_jobs (id,kind,status,created_at,updated_at) VALUES (%s,%s,%s,%s,%s)',
                            (job_id, kind[:24], 'queued', db_time(now), db_time(now)))
                return job_id

    def update_job(self, job_id, status, result=None):
        if status not in ('queued','running','succeeded','failed','partial','waiting','busy','skipped','recovery_required'):
            raise ValueError('Invalid job status')
        with self.cursor(write=True) as cur:
            cur.execute('UPDATE task_jobs SET status=%s,result_text=%s,updated_at=%s WHERE id=%s',
                        (status, json.dumps(json_ready(result or {}), ensure_ascii=False), db_time(), job_id))

    def get_job(self, job_id):
        with self.cursor(dictionary=True) as cur:
            cur.execute('SELECT * FROM task_jobs WHERE id=%s', (job_id,))
            row = cur.fetchone()
            if row:
                row = json_ready(row)
                row['result'] = json.loads(row.pop('result_text') or '{}')
                row['message'] = row['result'].get('message','')
            return row

    def list_unknown_batches(self, limit=100):
        with self.cursor(dictionary=True) as cur:
            cur.execute("SELECT id,status,news_count,error_code,created_at,updated_at FROM delivery_batches "
                        "WHERE status IN ('unknown','sending') ORDER BY created_at LIMIT %s",
                        (min(max(int(limit),1),100),))
            rows = cur.fetchall()
        for row in rows:
            row['news_ids'] = [item['id'] for item in self.get_batch(row['id'])['items']]
        return json_ready(rows)

    def reconcile_unknown(self, batch_id, outcome, receipt_note):
        """Record an operator's external receipt check, without sending anything."""
        if outcome not in ('confirmed-sent','confirmed-not-sent'):
            raise ValueError('An explicit confirmed delivery outcome is required')
        if not isinstance(receipt_note,str) or not 3<=len(receipt_note.strip())<=500:
            raise ValueError('Provide a receipt reference or verification note (3-500 characters)')
        with self.pipeline_lock() as acquired:
            if not acquired:
                raise RuntimeError('Wait for the running pipeline before reconciliation')
            self.ensure_runtime_ready()
            self.assert_pipeline_lock()
            # With the global pipeline lock held, no live compliant sender owns
            # a leftover 'sending' record. Reconciliation never invokes POST.
            self.recover_inflight()
            with self.cursor(write=True) as cur:
                cur.execute('SELECT status,analysis_text,news_count FROM delivery_batches WHERE id=%s FOR UPDATE',(batch_id,))
                row=cur.fetchone()
                if not row or row[0]!='unknown':
                    raise RuntimeError('Only an unknown batch can be reconciled')
                analysis=json.loads(row[1])
                analysis['_delivery_reconciliation']={'outcome':outcome,'receipt_note':receipt_note.strip(),'at':utc().isoformat()}
                text=json.dumps(analysis,ensure_ascii=False)
                retry_status = 'manual_send_failed' if analysis.get('_publication_mode') == 'manual' else 'send_failed'
                status='sent' if outcome=='confirmed-sent' else retry_status
                cur.execute("UPDATE delivery_batches SET status=%s,analysis_text=%s,error_code='manual_reconciliation',updated_at=%s WHERE id=%s AND status='unknown'",
                            (status,text,db_time(),batch_id))
                if cur.rowcount!=1:
                    raise RuntimeError('Batch changed during reconciliation')
                if outcome=='confirmed-sent':
                    cur.execute("UPDATE news_items SET notified=1,processing_state='sent' WHERE batch_id=%s",(batch_id,))
                    cur.execute('INSERT INTO sent_logs (sent_at,news_count,analysis_text,sentiment,batch_id) VALUES (%s,%s,%s,%s,%s)',
                                (db_time(),row[2],text,str(analysis.get('sentiment',''))[:255],batch_id))
                cur.execute('INSERT INTO delivery_attempts (id,batch_id,status,error_code,started_at,finished_at) VALUES (%s,%s,%s,%s,%s,%s)',
                            (str(uuid.uuid4()),batch_id,'reconciled_sent' if status=='sent' else 'reconciled_not_sent','manual_reconciliation',db_time(),db_time()))
            return {'batch_id':batch_id,'status':status,'sent_now':0,'message':'已保存人工核对结果；此命令没有发送消息'}

    def check_database(self):
        """Read-only schema/receipt summary; no collection or migration."""
        required = {'news_items':{'processing_state','skip_reason','batch_id'},
                    'sent_logs':{'batch_id'}, 'delivery_batches':{'status','analysis_text'},
                    'batch_items':{'batch_id','news_id'}, 'delivery_attempts':{'status'},
                    'report_periods':{'status','period_start','period_end'}, 'task_jobs':{'status','result_text'}}
        missing, counts = [], {}
        with self.cursor() as cur:
            for table, columns in required.items():
                cur.execute('SELECT column_name FROM information_schema.columns '
                            'WHERE table_schema=DATABASE() AND table_name=%s', (table,))
                actual = {row[0] for row in cur.fetchall()}
                missing.extend(f'{table}.{column}' for column in sorted(columns-actual))
                if actual:
                    cur.execute(f'SELECT COUNT(*) FROM `{table}`')
                    counts[table] = cur.fetchone()[0]
        try:
            self.ensure_runtime_ready()
        except (ValueError,RuntimeError):
            missing.append('legacy_timezone_migration_incomplete')
        return {'ready':not missing,'missing':missing,'counts':counts,
                'unknown_batches':self.list_unknown_batches() if not missing else []}

    def ensure_runtime_ready(self):
        with self.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() "
                        "AND table_name='schema_meta'")
            if not cur.fetchone()[0]:
                return
            cur.execute('SELECT meta_value FROM schema_meta WHERE meta_key=%s',('legacy_timezone_v1',))
            row=cur.fetchone()
            if row:
                state=json.loads(row[0])
                if not isinstance(state,dict) or state.get('status')!='converted':
                    raise RuntimeError('Legacy timezone migration incomplete; rerun --init-db')
