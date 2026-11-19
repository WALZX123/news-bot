import unittest
import json
from types import SimpleNamespace
from datetime import date, datetime, timezone
from unittest.mock import patch

from src import analyzer, reports
from src.config import AppConfig, ReportConfig, AIConfig
from test_storage import DatabaseBoundary


class CalendarPeriodTests(unittest.TestCase):
    def test_calendar_month_quarter_year_and_shanghai_midnight(self):
        self.assertEqual(reports.completed_period('monthly', datetime(2026, 2, 28, 16, tzinfo=timezone.utc)),
                         (date(2026, 2, 1), date(2026, 3, 1)))
        self.assertEqual(reports.completed_period('quarterly', datetime(2026, 4, 1, tzinfo=timezone.utc)),
                         (date(2026, 1, 1), date(2026, 4, 1)))
        self.assertEqual(reports.completed_period('yearly', datetime(2025, 1, 1, tzinfo=timezone.utc)),
                         (date(2024, 1, 1), date(2025, 1, 1)))
        self.assertEqual(reports.completed_period('weekly', datetime(2026, 11, 16, tzinfo=timezone.utc)),
                         (date(2026, 11, 9), date(2026, 11, 16)))


class ReportWorkflowTests(unittest.TestCase):
    def test_report_limits_surplus_events_and_keeps_verifiable_references(self):
        output = {"title": "", "summary": "Fixture", "outlook": "", "data_summary": "", "domain_analysis": {},
                  "top_events": [{"rank": index, "event": f"Event {index}", "significance": "Evidence",
                                  "evolution": "", "evidence_ids": ["n0"]} for index in range(1, 11)],
                  "trends": [f"Trend {index}" for index in range(10)]}
        with patch("src.analyzer._request_json", return_value=output):
            result = analyzer.generate_report([{"title": "Fixture", "url": "https://example.org/event"}],
                                              "weekly", "2026-08-24", "2026-08-31", AIConfig())
        self.assertEqual(list(range(1, 9)), [event["rank"] for event in result["top_events"]])
        self.assertTrue(all(event["url"] == "https://example.org/event" for event in result["top_events"]))
        self.assertEqual(8, len(result["trends"]))

    def test_report_fills_display_fields_and_drops_only_unverifiable_events(self):
        raw = {"summary": "Fixture", "domain_analysis": [{"domain": "AI", "analysis": "Observed"}],
               "top_events": [
            {"rank": 5, "title": "Referenceable", "evidence_ids": ["n0"], "url": "https://invalid.example"},
            {"event": "Missing evidence", "evidence_ids": ["unknown"]},
        ]}
        with patch("src.analyzer._request_json", return_value=raw):
            news = [{"title": "Fixture", "url": "https://example.org/event"}]
            result = analyzer.generate_report(news, "weekly", "2026-08-24", "2026-08-31", AIConfig())
            self.assertEqual(1, result["_omitted_events"])
            event, = result["top_events"]
            self.assertEqual(1, event["rank"])
            self.assertEqual("https://example.org/event", event["url"])
            self.assertEqual("", result["outlook"])
            self.assertEqual({"AI": "Observed"}, result["domain_analysis"])
            raw["top_events"].pop(0)
            with self.assertRaises(analyzer.ModelResponseError):
                analyzer.generate_report(news, "weekly", "2026-08-24", "2026-08-31", AIConfig())

    def test_failed_period_has_durable_reason_and_can_be_retried_alone(self):
        db = DatabaseBoundary()
        self.addCleanup(db.close)
        store = db.store()
        config = AppConfig(reports=ReportConfig(False, True, False, False))
        clock = datetime(2026, 11, 2, tzinfo=timezone.utc)
        periods = [(date(2026, 9, 1), date(2026, 10, 1)), (date(2026, 10, 1), date(2026, 11, 1))]
        for start, _ in periods:
            for item in range(60):
                store.save_news(f"https://example.org/{start.month}/{item}", f"Event {item}", "Feed excerpt",
                                "Fixture", datetime(2026, start.month, 15, tzinfo=timezone.utc))
        with patch("src.analyzer.generate_report", side_effect=analyzer.ModelResponseError(
                "private model payload fixture", code="invalid_evidence")):
            attempts = reports.run_reports(config, store, clock)
        self.assertEqual(2, attempts["errors"])
        for start, end in periods:
            self.assertEqual("model.invalid_evidence", store.get_report_period("monthly", start, end)["error_code"])
        chosen_start, chosen_end = periods[1]
        with patch("src.analyzer.generate_report", return_value={"summary": "Fixture"}) as model:
            result = reports.retry_report(config, store, "monthly", chosen_start, chosen_end, now=clock)
            self.assertEqual("succeeded", result["status"])
            self.assertEqual(48, result["sampled_news"])
            self.assertEqual("skipped", reports.retry_report(config, store, "monthly", chosen_start, chosen_end, now=clock)["status"])
            model.assert_called_once()
        self.assertEqual("failed", store.get_report_period("monthly", *periods[0])["status"])
        self.assertEqual(result["report_id"], store.get_report_period("monthly", *periods[1])["report_id"])

    def test_two_source_report_can_shrink_to_one_record_under_valid_budget(self):
        db=DatabaseBoundary()
        self.addCleanup(db.close)
        store=db.store()
        for i in range(10):
            store.save_news('https://example.org/'+str(i),'event'+str(i),'summary',
                            'A' if i%2 else 'B',now=datetime(2026,10,15,tzinfo=timezone.utc))
        cfg=AppConfig(reports=ReportConfig(False,True,False,False),
                      ai=AIConfig(max_tokens=256,total_token_budget=1627))
        output={'title':'fixture','summary':'given facts','top_events':[],'domain_analysis':{},
                'trends':[],'outlook':'insufficient material','data_summary':'no added claims'}
        response=SimpleNamespace(content=[SimpleNamespace(text=json.dumps(output))],stop_reason='end_turn',
                                 usage=SimpleNamespace(input_tokens=1,output_tokens=1))
        with patch('src.analyzer.ModelClient') as sdk:
            sdk.return_value.create.return_value=response
            result=reports.run_reports(cfg,store,datetime(2026,11,2,tzinfo=timezone.utc))
        self.assertEqual(result['generated'],1)
        with store.cursor() as cur:
            cur.execute('SELECT content FROM reports')
            self.assertEqual(json.loads(cur.fetchone()[0])['_coverage']['sampled_news'],1)

    def test_missed_months_catch_up_from_date_ledger_and_do_not_repeat(self):
        db = DatabaseBoundary()
        self.addCleanup(db.close)
        store = db.store()
        store.claim_report('monthly', date(2026, 8, 1), date(2026, 9, 1))
        store.save_report('monthly', date(2026, 8, 1), date(2026, 9, 1), 'old', '{}', 10)
        for month in (9, 10, 11):
            for i in range(10):
                store.save_news(f'https://example.org/{month}/{i}', f'Event {month}/{i}', 'summary',
                                'source', datetime(2026, month, 15, tzinfo=timezone.utc))
        config = AppConfig(reports=ReportConfig(False, True, False, False))
        with patch('src.analyzer.generate_report', return_value={'title': 'fixture', 'summary': 'given facts'}) as model:
            result = reports.run_reports(config, store, datetime(2026, 12, 2, tzinfo=timezone.utc))
            self.assertEqual(result['generated'], 3)
            periods = [(call.args[2], call.args[3]) for call in model.call_args_list]
            self.assertEqual(periods, [('2026-09-01', '2026-10-01'), ('2026-10-01', '2026-11-01'),
                                       ('2026-11-01', '2026-12-01')])
            self.assertEqual(reports.run_reports(config, store, datetime(2026, 12, 3, tzinfo=timezone.utc))['generated'], 0)


if __name__ == '__main__':
    unittest.main()
