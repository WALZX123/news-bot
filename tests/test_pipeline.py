import unittest
import json
import threading
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

import main
from src.config import AppConfig, DingTalkConfig, PushTimeWindow
from src.fetcher import FetchBatch, NewsItem
from test_storage import DatabaseBoundary


class PipelineContractTests(unittest.TestCase):
    def test_fetch_only_persists_work_until_next_empty_fetch(self):
        db = DatabaseBoundary()
        self.addCleanup(db.close)
        config = AppConfig()
        for name in ('weekly', 'monthly', 'quarterly', 'yearly'):
            setattr(config.reports, name, False)
        now = datetime(2026, 11, 17, 3, tzinfo=timezone.utc)
        item = NewsItem('new event', 'https://example.org/news', 'summary', 'source', now)
        with patch('src.fetcher.fetch_all', return_value=[item]), \
             patch('src.analyzer.analyze_news', return_value={'sentiment':'neutral',
                   'categories': {'快讯': [{'title': item.title, 'brief': item.summary, 'url': item.url}]}}) as model, \
             patch('src.sender.send_to_dingtalk', return_value=True) as sender:
            first = main.run_once(config=config, storage=db.store(), fetch_only=True, now=now)
            self.assertEqual(first['inserted'], 1)
            self.assertEqual(model.call_count, 0)
            with patch('src.fetcher.fetch_all', return_value=[]):
                second = main.run_once(config=config, storage=db.store(), now=now)
            self.assertEqual(second['sent'], 1)
            self.assertEqual(sender.call_args.args[1], 1)
            self.assertEqual(db.store().get_recent_sent_titles(now), ['new event'])

    def test_dependency_failure_becomes_a_task_result(self):
        class UnavailableDatabase:
            def pipeline_lock(self):
                raise OSError('test database unavailable')

        result = main.run_once(config=AppConfig(), storage=UnavailableDatabase(),
                               now=datetime(2026, 11, 17, tzinfo=timezone.utc))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['sent'], 0)


class DurablePipelineTests(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseBoundary()
        self.addCleanup(self.db.close)
        self.store = self.db.store()
        self.now = datetime(2026, 11, 17, 3, tzinfo=timezone.utc)
        self.config = AppConfig(dingtalk=DingTalkConfig('https://example.org/hook'),
                                push_time_window=PushTimeWindow(9,22))
        for kind in ('weekly','monthly','quarterly','yearly'):
            setattr(self.config.reports,kind,False)
        self.network = ExitStack()
        self.addCleanup(self.network.close)
        self.sdk = self.network.enter_context(patch('src.analyzer.ModelClient'))
        self.sdk.return_value.create.side_effect = self.model_response
        self.post = self.network.enter_context(patch('requests.post'))
        self.post.side_effect = lambda *a,**k: SimpleNamespace(status_code=200,json=lambda:{'errcode':0},close=lambda:None)

    def model_response(self, **kwargs):
        payload = json.loads(kwargs['messages'][0]['content'])
        rows = payload['untrusted_news']
        if 'untrusted_interests' in payload:
            output = [{'index':i,'score':0,'reason':'unrelated'} for i in range(len(rows))]
        else:
            output = {'headline':'fixture','top8':[{'rank':i+1,'title':r['title'],'summary':'given fact',
                'importance':'高','evidence_ids':[r['id']]} for i,r in enumerate(rows[:8])],
                'categories':{},'trends':[],'deep_reads':[],'data_highlights':[],
                'sentiment':'中性','controversies':[]}
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(output))],stop_reason='end_turn',
                               usage=SimpleNamespace(input_tokens=50,output_tokens=100))

    def seed(self, title='Novel semiconductor announcement', suffix='1', **kwargs):
        self.store.save_news('https://example.org/news/'+suffix,title,'summary','source',self.now,now=self.now,**kwargs)

    def run_once(self, **kwargs):
        return main.run_once(config=self.config,storage=self.store,now=kwargs.pop('now',self.now),**kwargs)

    def test_analysis_failure_retries_durable_batch_on_empty_feed(self):
        self.seed()
        self.sdk.return_value.create.side_effect = OSError('fixture model unavailable')
        self.assertEqual(self.run_once()['status'],'failed')
        self.assertEqual(self.store.get_unnotified_count(),1)
        self.assertEqual(self.post.call_count,0)
        self.sdk.return_value.create.side_effect = self.model_response
        self.assertEqual(self.run_once()['sent'],0)
        result = self.run_once(now=self.now+timedelta(minutes=5))
        self.assertEqual(result['sent'],1)
        self.assertEqual(self.store.get_recent_sent_titles(self.now),['Novel semiconductor announcement'])

    def test_trace_records_failed_source_and_summary_model_usage(self):
        self.seed()
        self.config.config_path = str(Path(self.db.path).with_name("config.yaml"))
        self.config.auto_summarize = True
        self.config.ai.api_key = "fixture-model"
        row = self.store.get_pending_news()[0]
        content = {"articles": [{"id": row["id"], "summary": "Fixture summary",
                                 "evidence": [{"field": "title", "quote": row["title"]}]}]}
        self.sdk.return_value.create.side_effect = None
        self.sdk.return_value.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps(content))], stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=17, output_tokens=9))
        fetched = FetchBatch(failures=1, failed_sources=[{"source": "Fixture RSS", "code": "timeout"}])
        with patch("src.fetcher.fetch_all", return_value=fetched), patch("src.editorial.utc", return_value=self.now):
            result = self.run_once(fetch_only=True, enrich=True)
        stages = {step["stage"]: step for step in result["stages"]}
        self.assertEqual("partial", stages["collection"]["status"])
        self.assertEqual([{"source": "Fixture RSS", "code": "timeout"}], stages["collection"]["failed_sources"])
        self.assertEqual(1, stages["summaries"]["generated"])
        self.assertGreaterEqual(stages["summaries"]["duration_ms"], 0)
        usage, = result["model_usage"]
        self.assertEqual(("article_summary", 17, 9), (usage["operation"], usage["input_tokens"], usage["output_tokens"]))
        self.assertGreaterEqual(usage["latency_ms"], 0)
        self.post.assert_not_called()

    def test_explicit_rejection_retries_cached_analysis_and_records_one_success(self):
        self.seed()
        self.post.side_effect = lambda *a,**k: SimpleNamespace(status_code=200,json=lambda:{'errcode':310000},close=lambda:None)
        self.assertEqual(self.run_once()['status'],'failed')
        self.post.side_effect = lambda *a,**k: SimpleNamespace(status_code=200,json=lambda:{'errcode':0},close=lambda:None)
        self.assertEqual(self.run_once()['sent'],0)
        self.assertEqual(self.run_once(now=self.now+timedelta(minutes=5))['sent'],1)
        self.assertEqual(self.sdk.return_value.create.call_count,1)
        with self.store.cursor() as cursor:
            cursor.execute('SELECT COUNT(*) FROM sent_logs')
            self.assertEqual(cursor.fetchone()[0],1)

    def test_unknown_delivery_is_visible_and_never_automatically_repeated(self):
        import requests
        self.seed()
        self.post.side_effect = requests.ReadTimeout('fixture timeout')
        self.assertEqual(self.run_once()['status'],'partial')
        self.run_once()
        self.assertEqual(self.post.call_count,1)
        self.assertEqual(len(self.store.list_unknown_batches()),1)

    def test_manual_receipt_confirmation_records_success_without_resending(self):
        import requests
        self.seed()
        self.post.side_effect=requests.ReadTimeout('fixture timeout')
        self.run_once()
        unknown=self.store.list_unknown_batches()[0]
        outcome=self.store.reconcile_unknown(unknown['id'],'confirmed-sent','fixture platform receipt')
        self.assertEqual(outcome['sent_now'],0)
        self.assertEqual(self.store.list_unknown_batches(),[])
        self.assertEqual(self.post.call_count,1)
        with self.assertRaises(RuntimeError):
            self.store.reconcile_unknown(unknown['id'],'confirmed-not-sent','conflicting assertion')

    def test_interrupted_sending_can_be_reconciled_without_running_pipeline(self):
        self.seed()
        batch=self.store.create_batch([self.store.get_pending_news()[0]['id']],self.now)
        self.store.set_batch_analysis(batch['id'],{'sentiment':'neutral'},self.now)
        self.store.begin_delivery(batch['id'],self.now)
        result=self.store.reconcile_unknown(batch['id'],'confirmed-not-sent','verified no platform receipt')
        self.assertEqual(result['status'],'send_failed')
        self.assertEqual(self.post.call_count,0)

    def test_closed_window_defers_model_and_open_window_acknowledges_only_selected(self):
        self.seed(suffix='1')
        self.seed('Different space telescope discovery','2')
        self.config.max_news_per_run = 1
        closed = datetime(2026,11,16,15,tzinfo=timezone.utc)
        self.assertEqual(self.run_once(now=closed)['status'],'waiting')
        self.assertEqual(self.sdk.return_value.create.call_count,0)
        self.assertEqual(self.run_once()['sent'],1)
        self.assertEqual(len(self.store.get_pending_news()),1)
        self.assertEqual(self.post.call_count,1)

    def test_incremental_mode_does_not_match_just_ingested_self(self):
        self.seed('Football tournament winner','old',notified=True)
        self.seed()
        self.config.incremental_mode=True
        self.assertEqual(self.run_once()['sent'],1)

    def test_all_low_interest_news_skips_without_forced_push(self):
        self.seed()
        self.config.ai_interests='technology'
        result=self.run_once()
        self.assertEqual(result['sent'],0)
        self.assertEqual(self.post.call_count,0)
        self.assertEqual(self.store.get_pending_news(),[])

    def test_parallel_runs_only_one_owns_the_batch(self):
        self.seed()
        entered, release = threading.Event(),threading.Event()
        original=self.model_response
        def slow_model(**kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(**kwargs)
        self.sdk.return_value.create.side_effect=slow_model
        outcomes=[]
        worker=threading.Thread(target=lambda:outcomes.append(self.run_once()))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.run_once()['status'],'busy')
        finally:
            release.set()
            worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcomes[0]['sent'],1)
        self.assertEqual(self.post.call_count,1)

    def test_stale_pending_legacy_rows_do_not_become_current_news(self):
        self.store.save_news('https://example.org/stale','Old event','summary','source',
                             datetime(2002,1,1,tzinfo=timezone.utc),now=self.now)
        self.assertEqual(self.run_once()['sent'],0)
        self.assertEqual(self.sdk.return_value.create.call_count,0)
        self.assertEqual(self.store.get_pending_news(),[])

    def test_invalid_legacy_record_isolated_without_poisoning_valid_news(self):
        self.store.save_news('javascript:legacy_bad()','Bad legacy article','summary','source',self.now,now=self.now)
        self.seed()
        self.assertEqual(self.run_once()['sent'],1)
        self.assertEqual(self.store.get_pending_news(),[])

    def test_old_oversized_retry_batch_shrinks_and_preserves_remaining_ids(self):
        for i in range(50):
            self.store.save_news('https://example.org/'+str(i)+'x'*1900,'A'*500+str(i),'中'*500,'source',self.now,now=self.now)
        batch=self.store.create_batch([row['id'] for row in self.store.get_pending_news()],self.now)
        self.store.fail_analysis(batch['id'],self.now)
        result=self.run_once(now=self.now+timedelta(minutes=5))
        self.assertGreater(result['sent'],0)
        self.assertLess(result['sent'],50)
        self.assertEqual(len(self.store.get_pending_news()),50-result['sent'])

    def test_failed_batch_waits_without_blocking_fresh_batch(self):
        self.seed()
        self.post.side_effect = lambda *a,**k: SimpleNamespace(status_code=429,close=lambda:None)
        old = self.run_once()
        self.assertEqual("failed", old["status"])
        self.seed("Completely separate weather topic", "fresh")
        self.post.side_effect = lambda *a,**k: SimpleNamespace(status_code=200,json=lambda:{"errcode":0},close=lambda:None)
        fresh = self.run_once(now=self.now+timedelta(minutes=1))
        self.assertEqual(1, fresh["sent"])
        self.assertNotEqual(old["batch_id"], fresh["batch_id"])
        payload = self.store.get_batch(fresh["batch_id"])["analysis"]["_delivery"]
        self.assertEqual(self.post.call_args.kwargs["json"]["markdown"]["text"], payload["markdown"])
        self.assertEqual(1, payload["visible_links"])

    def test_analysis_retries_stop_at_three_failures(self):
        self.seed()
        batch = self.store.create_batch([self.store.get_pending_news()[0]["id"]], self.now)
        for offset in (0, 5, 25):
            self.store.fail_analysis(batch["id"], self.now+timedelta(minutes=offset))
        self.assertIsNone(self.store.get_retry_batch(now=self.now+timedelta(hours=2)))
        self.assertEqual("analysis_failed_3", self.store.get_batch(batch["id"])["error_code"])

    def test_due_retry_yields_then_gets_its_turn(self):
        self.seed()
        self.post.side_effect = lambda *a,**k: SimpleNamespace(status_code=429,close=lambda:None)
        failed = self.run_once()
        self.seed("Completely separate weather topic", "fresh")
        self.post.side_effect = lambda *a,**k: SimpleNamespace(status_code=200,json=lambda:{"errcode":0},close=lambda:None)
        fresh = self.run_once(now=self.now+timedelta(minutes=6))
        self.assertNotEqual(failed["batch_id"], fresh["batch_id"])
        retry = self.run_once(now=self.now+timedelta(minutes=12))
        self.assertEqual(failed["batch_id"], retry["batch_id"])
        self.assertEqual(1, retry["sent"])

    def test_pending_timezone_migration_refuses_processing(self):
        self.seed()
        with self.store.cursor(write=True) as cur:
            cur.execute('CREATE TABLE schema_meta (meta_key TEXT PRIMARY KEY,meta_value TEXT)')
            cur.execute('INSERT INTO schema_meta VALUES (%s,%s)',('legacy_timezone_v1','{"status":"pending"}'))
        self.assertEqual(self.run_once()['status'],'failed')
        self.assertEqual(self.post.call_count,0)
        self.assertEqual(len(self.store.get_pending_news()),1)

    def test_window_closing_during_analysis_preserves_cached_batch(self):
        from src.storage import utc as real_utc
        self.seed()
        current=[datetime(2026,11,17,13,59,tzinfo=timezone.utc)]
        def model(**kwargs):
            current[0]=datetime(2026,11,17,14,1,tzinfo=timezone.utc)
            return self.model_response(**kwargs)
        self.sdk.return_value.create.side_effect=model
        with patch('src.pipeline.utc',side_effect=lambda value=None:current[0] if value is None else real_utc(value)):
            result=main.run_once(config=self.config,storage=self.store)
        self.assertEqual(result['status'],'waiting')
        self.assertEqual(self.post.call_count,0)
        self.assertEqual(self.run_once(now=datetime(2026,11,18,3,tzinfo=timezone.utc))['sent'],1)
        self.assertEqual(self.sdk.return_value.create.call_count,1)

    def test_stale_worker_cannot_revive_an_already_sent_batch_after_lock_loss(self):
        self.seed()
        entered=[False]
        def response(**kwargs):
            if not entered[0]:
                entered[0]=True
                with self.db.mutex:
                    self.db.locks.clear()
                later=self.now+timedelta(seconds=300)
                nested=main.run_once(config=self.config,storage=self.db.store(),now=later)
                self.assertEqual(nested['sent'],1)
            return self.model_response(**kwargs)
        self.sdk.return_value.create.side_effect=response
        self.assertEqual(self.run_once()['status'],'failed')
        self.assertEqual(self.post.call_count,1)
        self.assertIsNone(self.store.get_retry_batch())

    def test_curated_same_source_and_independent_source_coverage_cases(self):
        for i,(title,source) in enumerate([('芯片新品发布','A'),('芯片新品发布','A'),
                ('芯片新品发布','B'),('足球决赛冠军诞生','C')]):
            self.store.save_news('https://example.org/case/'+str(i),title,'summary',source,self.now,now=self.now)
        result=self.run_once()
        batch=self.store.get_batch(result['batch_id'])
        coverage={item['news_id']:item['independent_source_count'] for item in batch['analysis']['_selection']}
        self.assertEqual([coverage[row['id']] for row in batch['items']],[2,2,2,1])

    def test_only_news_with_visible_links_are_acknowledged(self):
        for number in range(1, 10):
            self.seed(f"Fixture story number {number}", str(number))
        result = self.run_once()
        self.assertEqual((9, 8, 8), (result["selected"], result["sent"], result["visible_links"]))
        self.assertEqual(8, len(result["news_ids"]))
        self.assertEqual(1, len(self.store.get_pending_news()))
        self.assertEqual(8, self.store.get_batch(result["batch_id"])["news_count"])
        second = self.run_once(now=self.now + timedelta(minutes=5))
        self.assertEqual(1, second["sent"])

    def test_digest_with_no_original_link_never_attempts_delivery(self):
        self.seed()
        with patch('src.analyzer.analyze_news', return_value={"headline": "Only editorial text"}):
            result = self.run_once()
        self.assertEqual("failed", result["status"])
        self.assertEqual(0, self.post.call_count)
        self.assertEqual(1, self.store.get_unnotified_count())


if __name__ == '__main__':
    unittest.main()
