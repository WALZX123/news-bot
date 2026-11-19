from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from src.config import AIConfig, AppConfig, DingTalkConfig
from src.editorial import PublicationConflict, article_summary, editorial_db, prepare_publication, publish, summarize
from src import analyzer, sender
from src.storage import utc
from test_storage import DatabaseBoundary
from workbench.backend.news_management import NewsConflict, change_news, revision


class EditorialTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = DatabaseBoundary()
        self.addCleanup(self.database.close)
        self.storage = self.database.store()
        self.config = AppConfig(config_path=str(Path(directory.name) / "config.yaml"),
                                ai=AIConfig(api_key="local-fixture"),
                                dingtalk=DingTalkConfig("https://example.org/fixture"))
        for number in (1, 2):
            self.storage.save_news(f"https://example.org/{number}", f"Title {number}", "Feed excerpt", "Fixture", utc())
        self.rows = self.storage.get_pending_news()

    def test_article_summary_is_cached_and_follows_the_captured_text(self):
        story = self.rows[0]
        evidence = [{"field": "excerpt", "quote": "Feed excerpt"}]
        output = {story["id"]: {"summary": "AI text", "evidence": evidence}}
        with patch("src.analyzer.summarize_articles", return_value=output) as provider:
            self.assertEqual(1, summarize(self.config, [story]))
            self.assertEqual(0, summarize(self.config, [story], force=True))
            provider.assert_called_once()
        self.assertEqual({"status": "ready", "summary": "AI text", "evidence": evidence}, article_summary(self.config, story))
        self.assertEqual("pending", article_summary(self.config, {**story, "summary": "Revised excerpt"})["status"])

    def test_invented_quote_is_rejected_and_retry_can_cache_real_quote(self):
        story = self.rows[0]
        item = {"id": story["id"], "summary": "AI text",
                "evidence": [{"field": "excerpt", "quote": "Invented statement"}]}
        with patch("src.analyzer._request_json", return_value={"articles": [item]}):
            self.assertEqual(0, summarize(self.config, [story]))
            self.assertEqual("failed", article_summary(self.config, story)["status"])
            item["evidence"][0]["quote"] = "Feed excerpt"
            self.assertEqual(1, summarize(self.config, [story], force=True))
        self.assertEqual(item["evidence"], article_summary(self.config, story)["evidence"])

    def test_old_summary_can_be_read_and_enriched_on_request(self):
        story = self.rows[0]
        evidence = [{"field": "excerpt", "quote": "Feed excerpt"}]
        with patch("src.analyzer.summarize_articles", return_value={
            story["id"]: {"summary": "stored summary", "evidence": evidence}
        }):
            self.assertEqual(1, summarize(self.config, [story]))
            with editorial_db(self.config) as db:
                db.execute("DELETE FROM summary_evidence")
            self.assertEqual({"status": "ready", "summary": "stored summary"}, article_summary(self.config, story))
            self.assertEqual(0, summarize(self.config, [story]))
            self.assertEqual(1, summarize(self.config, [story], force=True))
        self.assertEqual(evidence, article_summary(self.config, story)["evidence"])

    def test_manual_publication_reuses_exact_sorted_preview_and_is_idempotent(self):
        selected = self.rows[::-1]
        preview = prepare_publication(self.config, selected, "Editorial selection")
        self.assertLess(preview["markdown"].index("Title 2"), preview["markdown"].index("Title 1"))
        with patch("src.sender.send_to_dingtalk", return_value=True) as send:
            result = publish(self.config, self.storage, preview["id"])
            repeat = publish(self.config, self.storage, preview["id"])
            send.assert_called_once()
            analysis, count, _ = send.call_args.args
        self.assertEqual("sent", result["status"])
        self.assertEqual(result, repeat)
        self.assertEqual(preview["markdown"], sender.delivery_payload(analysis, count)["markdown"])

    def test_failed_manual_batch_stays_out_of_automatic_retry_and_can_be_retried(self):
        preview = prepare_publication(self.config, self.rows, "Manual")
        with patch("src.sender.send_to_dingtalk", side_effect=[False, True]) as send:
            self.assertEqual("failed", publish(self.config, self.storage, preview["id"])["status"])
            self.assertEqual("manual_send_failed", self.storage.get_batch(preview["id"])["status"])
            self.assertIsNone(self.storage.get_retry_batch())
            self.assertEqual("sent", publish(self.config, self.storage, preview["id"])["status"])
            self.assertEqual(2, send.call_count)

    def test_changed_destination_requires_a_fresh_review(self):
        preview = prepare_publication(self.config, self.rows, "Manual")
        self.config.dingtalk.webhook_url = "https://example.org/different"
        with patch("src.sender.send_to_dingtalk") as send:
            with self.assertRaises(PublicationConflict):
                publish(self.config, self.storage, preview["id"])
            send.assert_not_called()

    def test_edit_and_archive_invalidate_unpublished_work(self):
        row = self.rows[0]
        preview = prepare_publication(self.config, [row], "Manual")
        version = revision(row)
        edited = change_news(self.storage, row["id"], version, "edit", {**row, "title": "Edited title", "published_at": None})
        self.assertEqual("Edited title", edited["title"])
        with patch("src.sender.send_to_dingtalk") as send:
            with self.assertRaises(PublicationConflict):
                publish(self.config, self.storage, preview["id"])
            with self.assertRaises(NewsConflict):
                change_news(self.storage, row["id"], version, "archive")
            latest = prepare_publication(self.config, [edited], "Updated")
            archived = change_news(self.storage, row["id"], revision(edited), "archive")
            self.assertNotIn(row["id"], [item["id"] for item in self.storage.get_pending_news()])
            with self.assertRaises(PublicationConflict):
                prepare_publication(self.config, [archived], "Archive")
            with self.assertRaises(PublicationConflict):
                publish(self.config, self.storage, latest["id"])
            send.assert_not_called()
        restored = change_news(self.storage, row["id"], revision(archived), "restore")
        self.assertEqual("pending", restored["processing_state"])
        self.assertEqual("Edited title", restored["title"])
