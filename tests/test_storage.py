"""Real repository SQL with a local DB-API/dialect substitute, no network DB.

The SQLite test adapter translates only driver/dialect differences. Production
code always uses MySQL, including session-level advisory locks.
"""
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src.config import MySQLConfig
from src.storage import Storage, connection_cursor

sqlite3.register_adapter(datetime,lambda value:value.isoformat(' '))
sqlite3.register_adapter(date,lambda value:value.isoformat())
sqlite3.register_converter('TIMESTAMP',lambda value:datetime.fromisoformat(value.decode()))
sqlite3.register_converter('DATE',lambda value:date.fromisoformat(value.decode()))

SCHEMA = '''
CREATE TABLE news_items(id INTEGER PRIMARY KEY AUTOINCREMENT,url_hash TEXT UNIQUE,url TEXT,title TEXT,summary TEXT,source TEXT,published_at TIMESTAMP,notified INTEGER DEFAULT 0,created_at TIMESTAMP,processing_state TEXT DEFAULT 'pending',skip_reason TEXT DEFAULT '',batch_id TEXT);
CREATE TABLE delivery_batches(id TEXT PRIMARY KEY,status TEXT,analysis_text TEXT,news_count INTEGER,error_code TEXT DEFAULT '',created_at TIMESTAMP,updated_at TIMESTAMP);
CREATE TABLE batch_items(batch_id TEXT,news_id INTEGER UNIQUE,PRIMARY KEY(batch_id,news_id));
CREATE TABLE delivery_attempts(id TEXT PRIMARY KEY,batch_id TEXT,status TEXT,error_code TEXT DEFAULT '',started_at TIMESTAMP,finished_at TIMESTAMP);
CREATE TABLE sent_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,sent_at TIMESTAMP,news_count INTEGER,analysis_text TEXT,sentiment TEXT,batch_id TEXT UNIQUE);
CREATE TABLE reports(id INTEGER PRIMARY KEY AUTOINCREMENT,report_type TEXT,period_start DATE,period_end DATE,title TEXT,content TEXT,news_count INTEGER,created_at TIMESTAMP);
CREATE TABLE report_periods(report_type TEXT,period_start DATE,period_end DATE,status TEXT,report_id INTEGER,error_code TEXT DEFAULT '',updated_at TIMESTAMP,PRIMARY KEY(report_type,period_start,period_end));
CREATE TABLE task_jobs(id TEXT PRIMARY KEY,kind TEXT,status TEXT,result_text TEXT,created_at TIMESTAMP,updated_at TIMESTAMP);
'''


class DatabaseBoundary:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name)/'test.db')
        self.mutex = threading.Lock()
        self.locks = {}
        self.fail_insert_url = None
        conn = sqlite3.connect(self.path)
        conn.executescript(SCHEMA)
        conn.close()

    def connect(self):
        return Connection(self, sqlite3.connect(self.path, timeout=2, detect_types=sqlite3.PARSE_DECLTYPES))

    def store(self):
        return Storage(MySQLConfig(database='offline_unit_tests'), connection_factory=self.connect)

    def close(self):
        self.temp.cleanup()


class Connection:
    def __init__(self, boundary, conn):
        self.boundary, self.conn, self.held = boundary, conn, set()

    def cursor(self, dictionary=False):
        return Cursor(self, self.conn.cursor(), dictionary)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        with self.boundary.mutex:
            for name in self.held:
                self.boundary.locks.pop(name, None)
        self.conn.close()


class Cursor:
    def __init__(self, conn, cur, dictionary):
        self.conn, self.cur, self.dictionary = conn, cur, dictionary
        self.special = None

    def execute(self, sql, args=()):
        self.special = None
        if "information_schema.tables" in sql and "table_name='schema_meta'" in sql:
            self.cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='schema_meta'")
            return
        if sql.startswith('SELECT GET_LOCK'):
            with self.conn.boundary.mutex:
                name = args[0]
                allowed = name not in self.conn.boundary.locks
                if allowed:
                    self.conn.boundary.locks[name] = self.conn
                    self.conn.held.add(name)
                self.special = [(int(allowed),)]
            return
        if sql.startswith('SELECT RELEASE_LOCK'):
            with self.conn.boundary.mutex:
                self.conn.boundary.locks.pop(args[0], None)
                self.conn.held.discard(args[0])
                self.special = [(1,)]
            return
        if sql.startswith('SELECT IS_USED_LOCK'):
            self.special = [(int(self.conn.boundary.locks.get(args[0]) is self.conn),)]
            return
        if 'INSERT' in sql and 'news_items' in sql and self.conn.boundary.fail_insert_url is not None and self.conn.boundary.fail_insert_url in args:
            raise sqlite3.OperationalError('injected write failure')
        translated=sql.replace('INSERT IGNORE','INSERT OR IGNORE').replace('%s','?')
        translated=translated.replace(' ON DUPLICATE KEY UPDATE url_hash=url_hash',' ON CONFLICT(url_hash) DO NOTHING')
        translated=translated.replace(' FOR UPDATE','')
        self.cur.execute(translated, args)

    @property
    def rowcount(self):
        return self.cur.rowcount

    @property
    def lastrowid(self):
        return self.cur.lastrowid

    def _row(self, row):
        if row is None or not self.dictionary:
            return row
        return dict(zip([col[0] for col in self.cur.description], row))

    def fetchone(self):
        return self.special.pop(0) if self.special else self._row(self.cur.fetchone())

    def fetchall(self):
        return [self._row(row) for row in self.cur.fetchall()]

    def close(self):
        self.cur.close()


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseBoundary()
        self.addCleanup(self.db.close)
        self.store = self.db.store()

    def test_connection_closes_when_cursor_creation_and_rollback_fail(self):
        class Broken:
            closed = False
            def cursor(self, **kwargs):
                raise LookupError('cursor unavailable')
            def rollback(self):
                raise OSError('rollback unavailable')
            def close(self):
                self.closed = True
        conn = Broken()
        with self.assertRaisesRegex(LookupError, 'cursor unavailable'):
            with connection_cursor(conn, write=True):
                self.fail('cursor creation should fail')
        self.assertTrue(conn.closed)

    def test_constructor_has_no_ddl_side_effects(self):
        with patch('src.storage.pooling.MySQLConnectionPool') as pool:
            Storage(MySQLConfig())
            pool.return_value.get_connection.assert_not_called()

    def test_session_lock_coordinates_independent_storage_instances(self):
        other = self.db.store()
        with self.store.pipeline_lock() as first:
            with other.pipeline_lock() as second:
                self.assertTrue(first)
                self.assertFalse(second)
        with other.pipeline_lock() as third:
            self.assertTrue(third)

    def test_jobs_are_persistent_and_bounded_across_instances(self):
        first = self.store.create_job('fetch')
        other = self.db.store()
        second = other.create_job('run')
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(other.create_job('run'))
        other.update_job(first, 'succeeded', {'message':'done', 'sent':0})
        self.assertEqual(self.store.get_job(first)['message'], 'done')
        self.assertIsNotNone(self.store.create_job('run'))


if __name__ == '__main__':
    unittest.main()
