from contextlib import ExitStack, asynccontextmanager, closing
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Thread
import time
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from src.config import load_config
from workbench.backend import service


class ModelFixture(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append((self.path, body))
        if self.headers.get("Authorization") != "Bearer fixture-model-key":
            self.send_response(401)
            result = {"error": {"message": "private upstream detail"}}
        else:
            self.send_response(200)
            result = {
                "id": "fixture", "model": body["model"],
                "choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 24, "completion_tokens": 6},
            }
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())


class OperationsTest(TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        directory = Path(stack.enter_context(TemporaryDirectory()))
        self.path = directory / "config.yaml"
        self.model = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), ModelFixture))
        self.model.calls = []
        Thread(target=self.model.serve_forever, daemon=True).start()
        stack.callback(self.model.shutdown)
        self.path.write_text(f"""# Preserve this comment.
mysql:
  password: fixture-mysql-key
ai:
  api_key: fixture-model-key
  base_url: http://127.0.0.1:{self.model.server_port}
  model: example-model
dingtalk:
  webhook_url: https://example.org/secret-webhook
  secret: fixture-ding-key
ai_interests: |
  Interest text
reports:
  weekly: false
""", encoding="utf-8")
        stack.enter_context(patch.dict(os.environ, NEWS_WORKBENCH_KEY="fixture-internal",
                                       NEWS_BOT_CONFIG=str(self.path), LOCALAPPDATA=str(directory)))
        vault_fixture = {}

        def protected(data, *, encrypt):
            if encrypt:
                blob = uuid4().bytes
                vault_fixture[blob] = data
                return blob
            return vault_fixture[data]

        stack.enter_context(patch("src.secret_vault._protect", side_effect=protected))
        stack.enter_context(patch.object(service, "runtime", directory / "workbench"))
        self.client = stack.enter_context(TestClient(service.app, headers={"X-Workbench-Key": "fixture-internal"}))

    def settings(self):
        response = self.client.get("/settings")
        self.assertEqual(200, response.status_code)
        for secret in ("fixture-model-key", "fixture-mysql-key", "secret-webhook", "fixture-ding-key"):
            self.assertNotIn(secret, response.text)
        return response.json()

    def test_test_call_uses_saved_client_and_does_not_expose_keys(self):
        revision = self.settings()["revision"]
        response = self.client.post("/settings/ai/test", json={"revision": revision})
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(1, len(self.model.calls))
        path, request = self.model.calls[0]
        self.assertEqual("/chat/completions", path)
        self.assertEqual(32, request["max_tokens"])
        self.assertEqual("example-model", request["model"])
        self.assertTrue(self.settings()["last_probe"]["ok"])
        self.assertFalse(self.client.get("/automation").json()["active"])

    def test_atomic_structured_save_redaction_and_version_conflict(self):
        settings = self.settings()
        body = {key: settings["ai"][key] for key in ("base_url", "model", "max_tokens", "timeout_seconds")}
        body.update(revision=settings["revision"], api_key="fixture-replacement-key")
        response = self.client.put("/settings/ai", json=body)
        self.assertEqual(200, response.status_code, response.text)
        self.assertNotIn("fixture-replacement-key", response.text)
        self.assertEqual("fixture-replacement-key", load_config(self.path).ai.api_key)
        self.assertTrue(response.json()["ai"]["protected_key"])
        self.assertNotIn("fixture-replacement-key", self.path.read_text(encoding="utf-8"))
        files = list((self.path.parent / "NewsBot" / "secrets").glob("*.dpapi"))
        self.assertEqual(1, len(files))
        self.assertNotIn("fixture-replacement-key", files[0].read_text(encoding="utf-8"))
        self.assertIn("# Preserve this comment.", self.path.read_text(encoding="utf-8"))
        self.assertEqual(1, len(list(self.path.parent.glob("config.yaml.*.bak"))))
        stale = self.client.put("/settings/ai", json=body)
        self.assertEqual(409, stale.status_code)
        bad = self.client.put("/settings/ai", json={**body, "api_key": {"hidden": "DO_NOT_ECHO"}})
        self.assertEqual(422, bad.status_code)
        self.assertNotIn("DO_NOT_ECHO", bad.text)
        probe = self.client.post("/settings/ai/test", json={"revision": response.json()["revision"]}).json()
        self.assertEqual("unauthorized", probe["code"])
        self.assertNotIn("private upstream detail", json.dumps(probe))

    def test_advanced_and_structured_schedule_preserve_existing_fields(self):
        settings = self.settings()
        form = settings["schedule"]
        form.update(fetch_interval_minutes=17, window_start=8, window_end=21, ai_interests="新兴趣",
                    revision=settings["revision"])
        response = self.client.put("/settings/automation", json=form)
        self.assertEqual(200, response.status_code, response.text)
        changed = load_config(self.path)
        self.assertEqual(17, changed.fetch_interval_minutes)
        self.assertEqual("新兴趣", changed.ai_interests)
        self.assertEqual(21, changed.push_time_window.end)
        self.assertFalse(changed.reports.weekly)
        settings = response.json()
        advanced = self.client.put("/settings", json={
            "revision": settings["revision"], "yaml": settings["yaml"].replace("weekly: false", "weekly: true"),
        })
        self.assertEqual(200, advanced.status_code, advanced.text)
        self.assertTrue(load_config(self.path).reports.weekly)
        self.assertEqual("fixture-model-key", load_config(self.path).ai.api_key)

    def test_preview_is_queued_and_polls_without_opening_a_delivery(self):
        class Cursor:
            async def execute(self, statement):
                self.statement = statement

            async def fetchall(self):
                if not self.statement.startswith("SELECT"):
                    raise AssertionError("Preview allows only reads")
                return []

        @asynccontextmanager
        async def database():
            yield Cursor()

        with patch.object(service, "news_db", database), patch(
            "src.sender.send_to_dingtalk", side_effect=AssertionError("Preview must not send")
        ) as send:
            response = self.client.post("/preview")
            self.assertEqual(202, response.status_code)
            for _ in range(60):
                status = self.client.get("/preview").json()
                if status["status"] != "running":
                    break
                time.sleep(.02)
            self.assertEqual("empty", status["status"])
            send.assert_not_called()

    def test_editorial_delivery_and_full_mode_require_explicit_confirmation(self):
        with patch.object(service, "publish", side_effect=AssertionError("Unconfirmed publication")), patch.object(
                service, "request_mode", side_effect=AssertionError("Unconfirmed automation")):
            preview_id = "12345678-1234-1234-1234-123456781234"
            self.assertEqual(422, self.client.post("/editorial/send",
                             json={"id": preview_id, "confirmed": False}).status_code)
            self.assertEqual(422, self.client.post("/editorial/automation",
                             json={"action": "full"}).status_code)

    def test_drafts_can_be_updated_archived_and_restored(self):
        created = self.client.post("/drafts", json={})
        self.assertEqual(201, created.status_code, created.text)
        row = created.json()
        path = f"/drafts/{row['id']}"
        updated = self.client.put(path, json={"title": "手工选题", "body": "简报正文", "revision": row["revision"]})
        self.assertEqual(200, updated.status_code, updated.text)
        old_version = {"revision": row["revision"]}
        self.assertEqual(409, self.client.request("DELETE", path, json=old_version).status_code)
        self.assertEqual(204, self.client.request("DELETE", path,
                         json={"revision": updated.json()["revision"]}).status_code)
        self.assertEqual([], self.client.get("/drafts").json())
        archived, = self.client.get("/drafts?archived=true").json()
        self.assertTrue(archived["archived_at"])
        self.assertEqual(204, self.client.post(path + "/restore",
                         json={"revision": archived["revision"]}).status_code)
        self.assertEqual("简报正文", self.client.get(path).json()["body"])

    def test_source_management_and_trial_keep_other_settings(self):
        path = "/manage/sources"
        first = self.client.get(path).json()
        body = {"name": "来源甲", "url": "https://example.org/atom.xml"}
        before = self.path.read_bytes()
        with patch("src.fetcher.fetch_source", return_value=[]) as fetch:
            trial = self.client.post(path + "/test", json=body)
        self.assertEqual({"count": 0, "titles": []}, trial.json())
        fetch.assert_called_once()
        self.assertEqual(before, self.path.read_bytes())
        created = self.client.post(path, json={**body, "revision": first["revision"]})
        self.assertEqual(200, created.status_code, created.text)
        self.assertEqual(409, self.client.post(path, json={**body, "revision": first["revision"]}).status_code)
        edited = self.client.put(path, json={**body, "original": "来源甲", "name": "来源乙",
                                  "revision": created.json()["revision"]})
        self.assertEqual(200, edited.status_code, edited.text)
        self.assertEqual([{"name": "来源乙", "url": body["url"]}], self.client.get(path).json()["sources"])
        removed = self.client.request("DELETE", path, json={"name": "来源乙", "revision": edited.json()["revision"]})
        self.assertEqual(200, removed.status_code, removed.text)
        self.assertEqual([], self.client.get(path).json()["sources"])
        self.assertIn("# Preserve this comment.", self.path.read_text(encoding="utf-8"))

    def test_sources_report_recent_contribution_and_observed_reliability(self):
        path = "/manage/sources"
        initial = self.client.get(path).json()
        for name in ("已有采集", "新订阅"):
            response = self.client.post(path, json={"revision": initial["revision"], "name": name,
                                        "url": f"https://example.org/feeds/{'old' if name == '已有采集' else 'new'}"})
            self.assertEqual(200, response.status_code, response.text)
            initial = response.json()

        class Cursor:
            async def execute(self, statement, params):
                with closing(sqlite3.connect(":memory:")) as database:
                    database.row_factory = sqlite3.Row
                    database.executescript("""
                        CREATE TABLE news_items(source, created_at, processing_state, skip_reason);
                        INSERT INTO news_items VALUES
                        ('已有采集', datetime('now', '-8 days'), 'sent', ''),
                        ('已有采集', datetime('now', '-2 days'), 'sent', ''),
                        ('已有采集', datetime('now', '-1 day'), 'pending', NULL),
                        ('已有采集', datetime('now', '-1 day'), 'skipped', 'operator_archived:pending:');
                    """)
                    values = tuple(value.isoformat(" ") if isinstance(value, datetime) else value for value in params)
                    self.rows = [dict(row) for row in database.execute(statement.replace("%s", "?"), values)]

            async def fetchall(self):
                return self.rows

        @asynccontextmanager
        async def database():
            yield Cursor()

        with patch.object(service, "news_db", database), patch.object(
                service, "source_health", return_value={"已有采集": {"success": 3, "fail": 1}}):
            response = self.client.get("/sources")
            self.assertEqual(200, response.status_code, response.text)
            first, new = response.json()
        self.assertEqual((3, 2, 1, 3, 1), (first["count"], first["recent_count"],
                         first["recent_sent"], first["successes"], first["failures"]))
        self.assertEqual((0, 0, None), (new["recent_count"], new["recent_sent"], new["successes"]))

    def test_related_news_returns_actual_article_and_confines_lookup(self):
        anchor = {"id": 1, "title": "OpenAI 发布 GPT-6.2", "summary": "摘录", "source": "测试源",
                  "url": "https://example.org/one", "processing_state": "pending", "skip_reason": "",
                  "published_at": datetime(2026, 11, 18, 8), "created_at": datetime(2026, 11, 18, 8)}
        related = {**anchor, "id": 2, "source": "另一来源", "title": "Introducing GPT-6.2 Astra and Sol",
                   "url": "https://example.org/two"}
        test = self

        class Cursor:
            async def execute(self, statement, values):
                self.anchor_id = values[0]
                if "LIMIT" in statement:
                    test.assertIn("NOT LIKE", statement)
                    test.assertEqual(300, values[-1])
                    test.assertEqual((48 * 2) * 3600, (values[3] - values[2]).total_seconds())

            async def fetchone(self):
                return anchor if self.anchor_id == 1 else None

            async def fetchall(self):
                return [related, {**related, "id": 3, "title": "OpenAI updates GPT-6.2 usage limits"}]

        @asynccontextmanager
        async def database():
            yield Cursor()

        with patch.object(service, "news_db", database):
            response = self.client.get("/news/1/related")
            self.assertEqual(200, response.status_code, response.text)
            match, = response.json()["items"]
            self.assertEqual(related["id"], match["article"]["id"])
            self.assertEqual("model_action", match["reason"])
            self.assertEqual(404, self.client.get("/news/999/related").status_code)
            self.assertEqual(403, self.client.get("/news/1/related", headers={"X-Workbench-Key": "incorrect"}).status_code)

    def test_report_retry_uses_a_separate_report_job(self):
        class Cursor:
            async def execute(self, statement):
                self.statement = statement
            async def fetchall(self):
                return [{"report_type": "weekly", "period_start": "2026-08-24",
                         "period_end": "2026-08-31", "status": "failed", "error_code": "model.invalid_evidence",
                         "updated_at": datetime(2026, 9, 2)}]

        @asynccontextmanager
        async def database():
            yield Cursor()

        period = {"report_type": "weekly", "period_start": "2026-08-24", "period_end": "2026-08-31"}
        with patch.object(service, "news_db", database), patch.object(
                service, "editorial_storage", return_value=object()), patch.object(
                service, "retry_report", return_value={"status": "succeeded", "report_id": 12}) as model, patch(
                "src.sender.send_to_dingtalk", side_effect=AssertionError("Report retry must not send")) as send:
            item, = self.client.get("/report-jobs").json()["items"]
            self.assertEqual("model.invalid_evidence", item["error_code"])
            response = self.client.post("/report-jobs/retry", json=period)
            self.assertEqual(202, response.status_code, response.text)
            for _ in range(30):
                job = self.client.get("/report-jobs").json()["job"]
                if job["status"] != "running":
                    break
                time.sleep(.02)
            self.assertEqual(12, job["report_id"])
            self.assertEqual("weekly", model.call_args.args[2])
            self.assertEqual("2026-08-24", model.call_args.args[3].isoformat())
            send.assert_not_called()
