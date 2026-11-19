"""Timezone migration uses the real SQL through a local DB dialect boundary."""
import json
import sqlite3
import unittest
from datetime import datetime, timedelta

from src.config import MySQLConfig
from src.storage import Storage
from test_storage import Connection, Cursor, DatabaseBoundary
from src.migrations import prepare_legacy_timezone, finalize_legacy_timezone


class MigrationCursor(Cursor):
    def execute(self, sql, args=()):
        if (getattr(self.conn.boundary, 'fail_migration', False)
                and sql.startswith('UPDATE `sent_logs`')):
            raise sqlite3.OperationalError('injected migration transaction failure')
        if 'information_schema.tables' in sql:
            sql = "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('news_items','sent_logs','reports','schema_meta')"
            args = ()
        elif 'information_schema.columns' in sql:
            self.special = [(int(any(row[1] == 'processing_state' for row in
                                    self.conn.conn.execute('PRAGMA table_info(news_items)'))),)]
            return
        sql = sql.replace(' FOR UPDATE', '').replace('TIMESTAMPADD(MINUTE,', 'legacy_shift(')
        if sql.startswith('CREATE TABLE IF NOT EXISTS schema_meta'):
            sql = sql.split(' ENGINE=')[0]
        return super().execute(sql, args)


class MigrationConnection(Connection):
    def cursor(self, dictionary=False):
        return MigrationCursor(self, self.conn.cursor(), dictionary)


class LegacyTimezoneTests(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseBoundary()
        self.addCleanup(self.db.close)
        def connect():
            conn = sqlite3.connect(self.db.path, detect_types=sqlite3.PARSE_DECLTYPES)
            conn.create_function('legacy_shift', 2, lambda minutes, value:
                (datetime.fromisoformat(value) + timedelta(minutes=minutes)).isoformat(' ') if value else None)
            return MigrationConnection(self.db, conn)
        self.store = Storage(MySQLConfig(database='offline_migration'), connection_factory=connect)
        with self.store.cursor(write=True) as cur:
            cur.execute('ALTER TABLE news_items DROP COLUMN processing_state')
            cur.execute("INSERT INTO news_items (url_hash,url,title,published_at,created_at) VALUES (%s,%s,%s,%s,%s)",
                        ('a', 'https://example.org/a', 'legacy', '2026-11-17 03:00:00', '2026-11-17 11:00:00'))
            cur.execute('INSERT INTO sent_logs (sent_at) VALUES (%s)', ('2026-11-17 11:00:00',))
            cur.execute('INSERT INTO reports (created_at) VALUES (%s)', ('2026-11-17 11:00:00',))

    def test_legacy_timezone_is_required_before_any_schema_change(self):
        with self.assertRaisesRegex(ValueError, 'legacy_timezone'):
            prepare_legacy_timezone(self.store)
        with self.store.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='schema_meta'")
            self.assertEqual(cur.fetchone()[0], 0)

    def test_east_eight_converts_only_database_generated_timestamps_once(self):
        prepare_legacy_timezone(self.store, '+08:00')
        finalize_legacy_timezone(self.store)
        finalize_legacy_timezone(self.store)
        with self.store.cursor() as cur:
            cur.execute('SELECT published_at,created_at FROM news_items')
            self.assertEqual(cur.fetchone(), (datetime(2026, 11, 17, 3), datetime(2026, 11, 17, 3)))
            for table, column in (('sent_logs', 'sent_at'), ('reports', 'created_at')):
                cur.execute(f'SELECT {column} FROM {table}')
                self.assertEqual(cur.fetchone()[0], datetime(2026, 11, 17, 3))
            cur.execute('SELECT meta_value FROM schema_meta WHERE meta_key=%s', ('legacy_timezone_v1',))
            self.assertEqual(json.loads(cur.fetchone()[0])['status'], 'converted')

    def test_ddl_interruption_resumes_from_committed_timezone_intent(self):
        prepare_legacy_timezone(self.store, '+08:00')
        with self.store.cursor(write=True) as cur:
            cur.execute("ALTER TABLE news_items ADD COLUMN processing_state TEXT DEFAULT 'pending'")
            cur.execute("INSERT INTO delivery_batches (id,created_at) VALUES (%s,%s)",
                        ('new', '2026-11-17 03:00:00'))
        # A resumed invocation no longer sees the old column layout. The durable
        # metadata, rather than layout inference, must still finish conversion.
        state = prepare_legacy_timezone(self.store)
        self.assertEqual(state['status'], 'pending')
        self.assertEqual(state['timezone'], '+08:00')
        finalize_legacy_timezone(self.store)
        self.assertEqual(prepare_legacy_timezone(self.store)['status'], 'converted')
        with self.store.cursor() as cur:
            cur.execute('SELECT created_at FROM news_items')
            self.assertEqual(cur.fetchone()[0], datetime(2026, 11, 17, 3))
            cur.execute('SELECT created_at FROM delivery_batches')
            self.assertEqual(cur.fetchone()[0], datetime(2026, 11, 17, 3))

    def test_conversion_failure_rolls_back_all_columns_and_preserves_pending_intent(self):
        prepare_legacy_timezone(self.store, '+08:00')
        self.db.fail_migration = True
        with self.assertRaises(sqlite3.OperationalError):
            finalize_legacy_timezone(self.store)
        with self.store.cursor() as cur:
            cur.execute('SELECT created_at FROM news_items')
            self.assertEqual(cur.fetchone()[0], datetime(2026, 11, 17, 11))
        self.assertEqual(prepare_legacy_timezone(self.store)['status'], 'pending')
        self.db.fail_migration = False
        finalize_legacy_timezone(self.store)
        with self.store.cursor() as cur:
            cur.execute('SELECT created_at FROM news_items')
            self.assertEqual(cur.fetchone()[0], datetime(2026, 11, 17, 3))

    def test_utc_is_explicit_and_keeps_already_utc_timestamps(self):
        self.assertEqual(prepare_legacy_timezone(self.store, 'UTC')['timezone'], '+00:00')
        finalize_legacy_timezone(self.store)
        self.assertEqual(prepare_legacy_timezone(self.store, '-00:00')['status'], 'converted')
        with self.store.cursor() as cur:
            cur.execute('SELECT created_at FROM news_items')
            self.assertEqual(cur.fetchone()[0], datetime(2026, 11, 17, 11))

    def test_western_fractional_offset_can_cross_a_utc_date_boundary(self):
        prepare_legacy_timezone(self.store, '-13:30')
        finalize_legacy_timezone(self.store)
        with self.store.cursor() as cur:
            cur.execute('SELECT created_at FROM news_items')
            self.assertEqual(cur.fetchone()[0], datetime(2026, 11, 18, 0, 30))

    def test_invalid_or_conflicting_timezone_cannot_change_persisted_intent(self):
        for zone in ('Asia/Shanghai', '+08', '+08:60', '+14:01', '-15:00', 8, '', '+8:00'):
            with self.subTest(zone=zone), self.assertRaises(ValueError):
                prepare_legacy_timezone(self.store, zone)
        prepare_legacy_timezone(self.store, '+08:00')
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            prepare_legacy_timezone(self.store, '+00:00')
        self.assertEqual(prepare_legacy_timezone(self.store)['timezone'], '+08:00')

    def test_new_utc_schema_does_not_require_or_apply_a_legacy_timezone(self):
        with self.store.cursor(write=True) as cur:
            cur.execute("ALTER TABLE news_items ADD COLUMN processing_state TEXT DEFAULT 'pending'")
        self.assertIsNone(prepare_legacy_timezone(self.store))
        self.assertIsNone(finalize_legacy_timezone(self.store))
        with self.store.cursor() as cur:
            cur.execute('SELECT created_at FROM news_items')
            self.assertEqual(cur.fetchone()[0], datetime(2026, 11, 17, 11))


if __name__ == '__main__':
    unittest.main()
