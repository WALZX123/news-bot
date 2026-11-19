"""Public Flask routes, with database / network / model boundaries replaced."""
import os
import tempfile
import re
import html
import hashlib
import json
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from werkzeug.security import generate_password_hash

from web import create_app

NODE_RUNTIME = shutil.which('node')
RUNTIME_ENV = os.environ.copy()

class FakeStorage:
    jobs = {}
    job_updates = []
    instances = []
    lock = threading.Lock()
    busy = False
    def __init__(self, config):
        self.instances.append(self)
        self.config = config
        self.rows = []
        self.cursor_error = False
        self.last_connection = None
        self.queries = []

    def _get_connection(self):
        self.last_connection = FakeConnection(self)
        return self.last_connection

    def create_job(self, kind):
        with self.lock:
            if len([row for row in self.jobs.values() if row["status"] in ("queued", "running")]) >= 2:
                return None
            job_id = str(len(self.jobs) + 1)
            self.jobs[job_id] = {"id": job_id, "status": "queued", "message": "任务已排队。"}
            return job_id

    def update_job(self, job_id, status, result=None):
        with self.lock:
            self.job_updates.append((job_id, status))
            self.jobs[job_id].update(status=status, result=result, message=(result or {}).get("message", ""))

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def recover_jobs(self):
        return 0

    @contextmanager
    def pipeline_lock(self):
        yield not self.busy

    def recover_inflight(self, now):
        pass

    def assert_pipeline_lock(self):
        if self.busy:
            raise RuntimeError('lease lost')

    def ensure_runtime_ready(self):
        pass

    def ingest_news(self, items, now=None):
        return len(items)

    def get_pending_news(self):
        return []

    def get_retry_batch(self):
        return None

    def cleanup_old(self, **kwargs):
        return 0


class FakeConnection:
    def __init__(self, storage):
        self.storage = storage
        self.closed = False

    def cursor(self, **kwargs):
        if self.storage.cursor_error:
            raise RuntimeError("private database error")
        return FakeCursor(self.storage)

    def close(self):
        self.closed = True


class FakeCursor:
    def __init__(self, storage):
        self.storage = storage
        self.query = ""
        self.params = ()

    def execute(self, query, params=()):
        self.query, self.params = query, params
        self.storage.queries.append((query, params))

    def fetchone(self):
        if "COUNT(" in self.query:
            return {"cnt": len(self.storage.rows)}
        if "MAX(sent_at)" in self.query:
            return {"last_run": None}
        return self.storage.rows[0] if self.storage.rows else None

    def fetchall(self):
        if "SELECT id" in self.query and "LIMIT %s OFFSET %s" in self.query:
            size, offset = self.params[-2:]
            return self.storage.rows[offset:offset + size]
        return []

    def close(self):
        pass


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "selected.yaml"
        self.raw = {"web_password": generate_password_hash("selected-password"), "rss_sources": []}
        FakeStorage.jobs = {}
        FakeStorage.job_updates = []
        FakeStorage.instances = []
        FakeStorage.busy = False
        self.write_config()
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.db = patch("src.storage.Storage", FakeStorage)
        self.db.start()
        self.addCleanup(self.db.stop)

    def write_config(self):
        self.path.write_text(yaml.safe_dump(self.raw), encoding="utf-8")

    def client(self):
        self.app = create_app(str(self.path))
        self.app.config["TESTING"] = True
        return self.app.test_client()

    def token(self, client):
        client.get("/login")
        with client.session_transaction() as sess:
            return sess.get("csrf_token", "missing-old-token")

    def login(self, client, **kwargs):
        return client.post("/login", data={"password": "selected-password", "csrf_token": self.token(client), **kwargs})

    def auth_token(self, client):
        with client.session_transaction() as sess:
            return sess["csrf_token"]

    def test_selected_password_and_safe_preserved_return_location(self):
        client = self.client()
        page = client.get("/login?next=/news/?q=hello").get_data(as_text=True)
        self.assertIn('name="next" value="/news/?q=hello"', page)
        response = self.login(client, next="/news/?q=hello")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/news/?q=hello")
        cookie = response.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_external_encoded_and_backslash_return_paths_stay_local(self):
        for value in ("https://example.org/", "//example.org", "/%2fexample.org", "/%255cexample.org", "/\\example.org", "/%0aLocation:evil"):
            with self.subTest(value=value):
                client = self.client()
                self.assertEqual(self.login(client, next=value).location, "/")

    def test_csrf_origin_and_logout_guards(self):
        client = self.client()
        self.assertEqual(client.post("/login", data={"password": "selected-password"}).status_code, 403)
        token = self.token(client)
        self.assertEqual(client.post("/login", data={"password": "selected-password", "csrf_token": token}, headers={"Origin": "https://example.org"}).status_code, 403)
        self.assertEqual(client.post("/login", data={"password": "selected-password", "csrf_token": token}, headers={"Origin": "http://["}).status_code, 403)
        self.assertEqual(self.login(client).status_code, 302)
        self.assertEqual(client.get("/logout").status_code, 405)
        self.assertEqual(client.post("/logout").status_code, 403)
        self.assertEqual(client.post("/logout", data={"csrf_token": self.auth_token(client)}, headers={"Origin": "http://localhost"}).status_code, 302)

    def test_guessing_rate_limit_and_password_change_revokes_cookie(self):
        client = self.client()
        token = self.token(client)
        for _ in range(5):
            self.assertEqual(client.post("/login", data={"password": "wrong", "csrf_token": token}).status_code, 401)
        self.assertEqual(client.post("/login", data={"password": "wrong", "csrf_token": token}).status_code, 429)
        client = self.client()
        self.assertEqual(self.login(client).status_code, 302)
        self.raw["web_password"] = generate_password_hash("new-password")
        self.write_config()
        self.assertEqual(client.get("/actions/status?job_id=old").status_code, 401)

    def test_page_bounds_fail_before_database_access(self):
        client = self.client()
        self.login(client)
        for route in ("/news/", "/history/", "/reports/"):
            for query in ("per_page=0", "per_page=-1", "page=-1", "page=bad", "page=999999999999999999", "per_page=101"):
                with self.subTest(route=route, query=query):
                    self.assertEqual(client.get(route + "?" + query).status_code, 400)

    def test_rendered_pages_guard_script_data_links_and_local_dependencies(self):
        source_name = "quote'\\\"\n</script><script>example()</script>"
        self.raw["rss_sources"] = [{"name": source_name, "url": "https://example.org/rss"}]
        self.write_config()
        client = self.client()
        self.login(client)
        sources = client.get("/sources/").get_data(as_text=True)
        self.assertNotIn("onclick=", sources)
        self.assertNotIn("</script><script>example()", sources)
        self.assertIn('action="/sources/remove"', sources)
        storage = self.app.config["news_bot_storage"]
        storage.rows = [{"id": 1, "title": "example", "url": "javascript:example()", "summary": "text", "source": "test", "published_at": datetime(2026, 11, 17, 0, 0), "created_at": datetime(2026, 11, 17, 0, 0), "notified": False}]
        detail = client.get("/news/1").get_data(as_text=True)
        self.assertNotIn('href="javascript:', detail)
        self.assertIn("2026-11-17 08:00", detail)
        storage.rows = []
        dashboard = client.get("/").get_data(as_text=True)
        self.assertIn("/static/vendor/chart/chart.umd.min.js", dashboard)
        self.assertIn('src="/static/dashboard.js"', dashboard)
        with client.get('/static/dashboard.js') as script_response:
            dashboard_script = script_response.get_data(as_text=True)
        self.assertIn("DOMContentLoaded", dashboard_script)
        self.assertNotIn("innerHTML", dashboard_script)
        self.assertNotIn("innerHTML", dashboard)
        self.assertNotIn('src="https://', dashboard)

    def test_csp_matches_inline_scripts_and_dependencies_are_local_and_verified(self):
        client = self.client()
        self.login(client)
        for route in ('/', '/sources/', '/config/edit', '/reports/', '/login'):
            response = client.get(route)
            body = response.get_data(as_text=True)
            policy = response.headers['Content-Security-Policy']
            self.assertNotIn('cdn.jsdelivr.net', policy)
            self.assertNotRegex(body, r'\son\w+=')
            self.assertNotRegex(body, r'(?:src|href)="https://cdn')
            for attributes in re.findall(r'<script([^>]*)>', body):
                if not re.search(r'\bsrc=', attributes):
                    nonce = re.search(r'nonce="([^"]+)"', attributes)
                    self.assertIsNotNone(nonce)
                    self.assertIn("'nonce-" + nonce[1] + "'", policy)
            if route != '/login':
                self.assertRegex(body, r'<form method="POST" action="/logout">\s*<input type="hidden" name="csrf_token"')
        vendor = Path(__file__).resolve().parents[1] / 'web/static/vendor'
        manifest = json.loads((vendor / 'manifest.json').read_text(encoding='utf-8-sig'))
        for entry in manifest:
            data = (vendor / entry['path']).read_bytes()
            self.assertEqual(len(data), entry['bytes'])
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry['sha256'])
            with client.get('/static/vendor/' + entry['path']) as asset_response:
                self.assertEqual(asset_response.status_code, 200)

    def test_edit_redacts_secrets_preserves_them_and_refreshes_selected_config(self):
        self.raw.update(ai={'api_key': 'private-key-do-not-display'},
                        dingtalk={'webhook_url': 'https://example.org/?token=private-hook', 'secret': 'private-signature'},
                        mysql={'password': 'private-db-password'})
        self.write_config()
        client = self.client()
        self.login(client)
        page = client.get('/config/edit').get_data(as_text=True)
        for value in ('private-key-do-not-display', 'private-hook', 'private-signature', 'private-db-password', self.raw['web_password']):
            self.assertNotIn(value, page)
        content = html.unescape(re.search(r'<textarea[^>]*>(.*?)</textarea>', page, re.S)[1])
        edited = yaml.safe_load(content)
        edited['fetch_interval_minutes'] = 17
        response = client.post('/config/edit', data={'csrf_token': self.auth_token(client),
                               'version': re.search(r'name="version" value="([^"]+)"', page)[1],
                               'yaml_content': yaml.safe_dump(edited)})
        self.assertEqual(response.status_code, 302)
        saved = yaml.safe_load(self.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['fetch_interval_minutes'], 17)
        self.assertEqual(saved['web_password'], self.raw['web_password'])
        self.assertEqual(saved['mysql']['password'], 'private-db-password')
        self.assertEqual(saved['ai']['api_key'], 'private-key-do-not-display')
        view = client.get('/config/').get_data(as_text=True)
        self.assertIn('17 分钟', view)
        self.assertIn('调度器', view)
        self.assertTrue(list(self.path.parent.glob(self.path.name + '.*.bak')))

    def test_news_date_filters_use_half_open_beijing_days_and_reject_invalid_dates(self):
        client = self.client()
        self.login(client)
        self.assertEqual(client.get('/news/?date_from=2026-11-17&date_to=2026-11-17').status_code, 200)
        queries = self.app.config['news_bot_storage'].queries
        query, params = next((q, p) for q, p in queries if 'SELECT id' in q)
        self.assertIn('created_at < %s', query)
        self.assertEqual(params[:2], [datetime(2026, 11, 16, 16), datetime(2026, 11, 17, 16)])
        for query in ('date_from=bad', 'date_from=2026-02-30', 'date_from=2026-11-18&date_to=2026-11-17'):
            self.assertEqual(client.get('/news/?' + query).status_code, 400)

    def test_dashboard_script_handles_terminal_errors_retry_and_dependency_order(self):
        client = self.client()
        with client.get('/static/dashboard.js') as response:
            script = response.get_data(as_text=True)
        node = NODE_RUNTIME
        if not node:
            self.skipTest('Node is needed for the offline browser-boundary script test')
        harness = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const script = fs.readFileSync(0, 'utf8');
function element(dataset = {}) {
  return {dataset, handlers: {}, disabled: false, hidden: true, textContent: '', className: '',
    addEventListener(event, handler) {this.handlers[event] = handler;},
    set innerHTML(value) {throw new Error('Unsafe HTML assignment: ' + value);}};
}
async function scenario(status, errorMode) {
  const source = element();
  source.textContent = JSON.stringify({csrf_token:'test-token',daily_labels:['11-17'],daily_values:[1],source_total_rows:[]});
  const state = element(), retry = element(), buttons = [element({jobAction:'fetch'}), element({jobAction:'analyze'})];
  const elements = {'dashboard-data':source,'job-status':state,'job-retry':retry,'dailyChart':element(),'sourcePieChart':element()};
  let ready, charts = 0, polls = 0, nextTimer = 0;
  const timers = new Map(), requests = [];
  const context = {AbortController, Date, console,
    setTimeout(fn, ms) {timers.set(++nextTimer,{fn,ms});return nextTimer;},
    clearTimeout(id) {timers.delete(id);},
    document: {addEventListener(event, fn) {assert.equal(event,'DOMContentLoaded');ready=fn;},
      getElementById(id) {return elements[id];},querySelectorAll() {return buttons;},
      documentElement:{getAttribute(){return 'light';}}},
    async fetch(url, options) {
      requests.push({url,options});
      if (url === '/actions/fetch') return {ok:true,status:202,headers:{get(){return 'application/json';}},async json(){return {job_id:'task-1',status:'queued'};}};
      polls++;
      if (errorMode && polls === 1) {
        if (errorMode === 'network') throw new Error('offline');
        if (errorMode === 'html') return {ok:true,status:200,headers:{get(){return 'text/html';}}};
        return {ok:false,status:Number(errorMode),headers:{get(){return 'application/json';}},async json(){return {message:'query failed'};}};
      }
      const current = status === 'queued' && polls === 1 ? 'queued' : status === 'queued' ? 'succeeded' : status;
      return {ok:true,status:200,headers:{get(){return 'application/json';}},async json(){return {status:current,message:'<img src=x onerror=example()>'};}};
    }};
  vm.createContext(context);
  vm.runInContext(script, context);
  assert.equal(charts,0);
  context.Chart = function(){charts++;}; // dependency becomes available before DOM ready
  ready();
  assert.equal(charts,2);
  await buttons[0].handlers.click();
  await new Promise(setImmediate);
  assert.equal(requests[0].options.headers['X-CSRF-Token'],'test-token');
  if (errorMode) {
    assert.match(state.className,/text-danger/);
    assert.equal(retry.hidden,false);
    assert.ok(buttons.every(button => !button.disabled));
    const submitted = requests.filter(request => request.url === '/actions/fetch').length;
    await buttons[0].handlers.click();
    assert.equal(requests.filter(request => request.url === '/actions/fetch').length, submitted);
    retry.handlers.click();
    await new Promise(setImmediate);
  } else if (status === 'queued') {
    assert.ok(buttons.every(button => button.disabled));
    const timer = Array.from(timers.values()).find(item => item.ms === 2000);
    assert.ok(timer);
    timer.fn();
    await new Promise(setImmediate);
  }
  assert.equal(state.textContent,'<img src=x onerror=example()>');
  const terminal = status === 'queued' ? 'succeeded' : status;
  assert.match(state.className, new RegExp('text-' + (terminal === 'succeeded' ? 'success' : terminal === 'failed' ? 'danger' : 'warning')));
  assert.ok(buttons.every(button => !button.disabled));
  assert.equal(retry.hidden,true);
}
(async function(){
  for (const status of ['succeeded','failed','partial','waiting','busy','skipped','recovery_required','unknown','queued']) await scenario(status);
  for (const error of ['network','html','401','404','503']) await scenario('succeeded',error);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run([node, '-e', harness], input=script, text=True,
                                capture_output=True, timeout=20, encoding='utf-8', env=RUNTIME_ENV)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_jobs_use_real_pipeline_are_bounded_and_survive_new_app_instance(self):
        client = self.client()
        self.login(client)
        release = threading.Event()
        entered = threading.Event()
        def fetch(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test fetch deadline')
            return []
        with patch('src.fetcher.fetch_all', side_effect=fetch), \
             patch('src.analyzer.analyze_news', side_effect=AssertionError('fetch-only cannot call AI')), \
             patch('src.sender.send_to_dingtalk', side_effect=AssertionError('fetch-only cannot send')):
            try:
                first = client.post('/actions/fetch', headers={'X-CSRF-Token':self.auth_token(client)})
                self.assertEqual(first.status_code, 202)
                self.assertTrue(entered.wait(1))
                second = client.post('/actions/fetch', headers={'X-CSRF-Token':self.auth_token(client)})
                self.assertEqual(second.status_code, 202)
                self.assertEqual(client.post('/actions/fetch', headers={'X-CSRF-Token':self.auth_token(client)}).status_code, 429)
                self.assertEqual(client.post('/actions/analyze').status_code, 403)
                self.assertEqual(client.get('/actions/status?job_id=missing').status_code, 404)
            finally:
                release.set()
            for job in (first.json['job_id'], second.json['job_id']):
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    result = client.get('/actions/status?job_id=' + job).json
                    if result['status'] not in ('queued', 'running'):
                        break
                    time.sleep(.01)
                self.assertEqual(result['status'], 'succeeded')
                self.assertEqual(result['result']['inserted'], 0)
            # A second app reads the same persisted job boundary, not a module dict.
            other = self.client()
            self.login(other)
            self.assertEqual(other.get('/actions/status?job_id=' + first.json['job_id']).json['status'], 'succeeded')

    def test_collection_failure_is_not_success_or_remote_html(self):
        client = self.client()
        self.login(client)
        with patch('src.fetcher.fetch_all', side_effect=RuntimeError('<secret>private-key</secret>')):
            response = client.post('/actions/fetch', headers={'X-CSRF-Token':self.auth_token(client)})
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                result = client.get('/actions/status?job_id=' + response.json['job_id'])
                if result.json['status'] not in ('queued', 'running'):
                    break
                time.sleep(.01)
            self.assertIn(result.json['status'], ('failed','partial'))
            self.assertNotIn('private-key', result.get_data(as_text=True))

    def test_running_job_renews_lease_then_stops_heartbeat_before_final_status(self):
        client = self.client()
        self.login(client)
        release = threading.Event()
        def fetch(*args, **kwargs):
            release.wait(2)
            return []
        with patch('src.fetcher.fetch_all', side_effect=fetch), \
             patch('web.routes.actions.JOB_HEARTBEAT_SECONDS', .01, create=True):
            try:
                response = client.post('/actions/fetch', headers={'X-CSRF-Token':self.auth_token(client)})
                job_id = response.json['job_id']
                deadline = time.monotonic() + .5
                while time.monotonic() < deadline and FakeStorage.job_updates.count((job_id,'running')) < 3:
                    time.sleep(.005)
                self.assertGreaterEqual(FakeStorage.job_updates.count((job_id,'running')), 3)
            finally:
                release.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and FakeStorage.jobs[job_id]['status'] in ('queued','running'):
                time.sleep(.005)
            self.assertEqual(FakeStorage.jobs[job_id]['status'], 'succeeded')
            time.sleep(.04)
            self.assertEqual(FakeStorage.job_updates[-1], (job_id,'succeeded'))

    def test_report_detail_discloses_available_sample_counts_and_history_limits(self):
        client = self.client()
        self.login(client)
        self.app.config['news_bot_storage'].rows = [{'id':1,'report_type':'yearly','title':'年度资料分析',
            'period_start':'2025-01-01','period_end':'2026-01-01','news_count':216,
            'content':json.dumps({'summary':'分析正文','_coverage':{'available_news':216,'sampled_news':200,
                'note':'仅依据当前留存资料及代表性样本；不声称完整覆盖该周期全部新闻。'}},ensure_ascii=False)}]
        response = client.get('/reports/1')
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code,200)
        self.assertIn('当前留存 216 条',body)
        self.assertIn('分析样本 200 条',body)
        self.assertIn('不声称完整覆盖该周期全部新闻',body)
        self.assertIn('旧版已删除的历史',body)

    def test_sources_show_persisted_request_health_and_test_uses_same_collection_limits(self):
        self.raw.update(rss_sources=[{'name':'example','url':'https://example.org/rss'}],
                        allow_private_rss_hosts=['127.0.0.1'],rss_max_response_bytes=1048576,
                        fetch_task_timeout=45,max_news_age_hours=36)
        self.write_config()
        client = self.client()
        self.login(client)
        health = {'example':{'request_success':12,'parse_success':11,'empty':3,
                  'last_error':'<b>parse</b>','last_attempt':'2026-11-17T00:00:00+00:00','returned':0}}
        expected_path = str(self.path.parent/'runtime'/'source-health.sqlite3')
        with patch('src.fetcher.get_source_health',return_value=health) as read_health, \
             patch('src.fetcher.fetch_source',return_value=[]) as fetch:
            body = client.get('/sources/').get_data(as_text=True)
            self.assertIn('请求成功 12',body)
            self.assertIn('解析成功 11',body)
            self.assertIn('空源 3',body)
            self.assertIn('&lt;b&gt;parse&lt;/b&gt;',body)
            self.assertNotIn('<b>parse</b>',body)
            read_health.assert_called_with(health_path=expected_path)
            response = client.post('/sources/test',data={'name':'example','url':'https://example.org/rss',
                                   'csrf_token':self.auth_token(client)})
            self.assertEqual(response.status_code,200)
            self.assertIn('请求与解析成功',response.get_data(as_text=True))
            self.assertEqual(fetch.call_args.kwargs,{'allow_private_hosts':['127.0.0.1'],
                'max_news_age_hours':36,'max_response_bytes':1048576,'task_timeout':45,'health_path':expected_path})

    def test_database_config_refresh_reuses_one_app_storage_after_switch(self):
        client = self.client()
        self.login(client)
        self.assertEqual(len(FakeStorage.instances),1)
        self.raw['mysql'] = {'host':'updated-db.example'}
        self.write_config()
        self.assertEqual(client.get('/news/').status_code,200)
        self.assertEqual(client.get('/news/').status_code,200)
        self.assertEqual(client.get('/sources/').status_code,200)
        self.assertEqual(len(FakeStorage.instances),2)
        self.assertEqual(FakeStorage.instances[-1].config.host,'updated-db.example')
        self.assertEqual(len(FakeStorage.instances[0].queries),0)

    def test_edit_round_trip_preserves_comments_spacing_and_original_secret_literals(self):
        original = ("# selected configuration header\n"
            f"web_password: '{self.raw['web_password']}' # login hash\n"
            "mysql:\n  password: \"private-db-value\" # keep database comment\n"
            "ai:\n  api_key: 'private-api-value' # keep API comment\n"
            "fetch_interval_minutes: 120  # existing schedule\n"
            "\n# source group alpha\nrss_sources:\n"
            "  - name: alpha # source label\n    url: https://example.org/rss # source link\n")
        self.path.write_text(original,encoding='utf-8')
        client = self.client()
        self.login(client)
        response = client.get('/config/edit')
        page = response.get_data(as_text=True)
        editable = html.unescape(re.search(r'<textarea[^>]*>(.*?)</textarea>',page,re.S)[1]).replace('\r\n','\n')
        self.assertIn('# selected configuration header',editable)
        self.assertIn('fetch_interval_minutes: 120  # existing schedule',editable)
        self.assertIn("  - name: alpha # source label\n    url: https://example.org/rss # source link",editable)
        for value in (self.raw['web_password'],'private-db-value','private-api-value'):
            self.assertNotIn(value,page)
        result = client.post('/config/edit',data={'csrf_token':self.auth_token(client),
            'version':re.search(r'name="version" value="([^"]+)"',page)[1],
            'yaml_content':editable.replace('fetch_interval_minutes: 120','fetch_interval_minutes: 17')})
        self.assertEqual(result.status_code,302)
        self.assertEqual(self.path.read_text(encoding='utf-8'),original.replace('fetch_interval_minutes: 120','fetch_interval_minutes: 17'))

    def test_editor_rejects_duplicate_keys_and_stale_versions_without_file_changes(self):
        self.raw['fetch_interval_minutes'] = 120
        self.write_config()
        client = self.client()
        self.login(client)
        page = client.get('/config/edit').get_data(as_text=True)
        editable = html.unescape(re.search(r'<textarea[^>]*>(.*?)</textarea>',page,re.S)[1])
        version = re.search(r'name="version" value="([^"]+)"',page)[1]
        original = self.path.read_bytes()
        duplicate = editable + '\nfetch_interval_minutes: 999\n'
        response = client.post('/config/edit',data={'csrf_token':self.auth_token(client),
            'version':version,'yaml_content':duplicate})
        self.assertEqual(response.status_code,303)
        self.assertEqual(self.path.read_bytes(),original)
        self.assertIn('重复字段',client.get('/config/edit').get_data(as_text=True))
        changed = original + b'\n# external writer changed this file\n'
        self.path.write_bytes(changed)
        response = client.post('/config/edit',data={'csrf_token':self.auth_token(client),
            'version':version,'yaml_content':editable.replace('120','17')})
        self.assertEqual(response.status_code,303)
        self.assertEqual(self.path.read_bytes(),changed)
        self.assertFalse(list(self.path.parent.glob(self.path.name + '.*.bak')))

    def test_source_add_remove_preserves_other_text_group_comments_and_remaining_items(self):
        prefix = f"# selected file\nweb_password: '{self.raw['web_password']}' # auth comment\n"
        suffix = "# schedule group\nfetch_interval_minutes: 120  # keep spacing\n"
        kept = "  - name: beta # beta label\n    url: https://example.org/beta # beta link\n"
        original = (prefix + "rss_sources: # sources header\n  # group alpha\n"
            "  - name: alpha # alpha note\n    url: https://example.org/alpha\n"
            "  # group beta\n" + kept + suffix)
        self.path.write_text(original,encoding='utf-8')
        client = self.client()
        self.login(client)
        response = client.post('/sources/remove',data={'name':'alpha','csrf_token':self.auth_token(client)})
        self.assertEqual(response.status_code,302)
        after_remove = self.path.read_text(encoding='utf-8')
        self.assertNotIn('name: alpha',after_remove)
        self.assertIn('# alpha note',after_remove)
        for text in (prefix,suffix,kept,'rss_sources: # sources header','  # group alpha','  # group beta'):
            self.assertIn(text,after_remove)
        response = client.post('/sources/add',data={'name':'gamma','url':'https://example.org/gamma',
                               'csrf_token':self.auth_token(client)})
        self.assertEqual(response.status_code,302)
        after_add = self.path.read_text(encoding='utf-8')
        self.assertEqual([source['name'] for source in yaml.safe_load(after_add)['rss_sources']],['beta','gamma'])
        for text in (prefix,suffix,kept,'rss_sources: # sources header','  # group alpha','  # group beta','# alpha note'):
            self.assertIn(text,after_add)

    def test_source_updates_handle_empty_absent_indentless_and_last_item_without_losing_comments(self):
        password_line = f"web_password: '{self.raw['web_password']}' # keep auth\n"
        layouts = ('rss_sources: [] # empty source group\n# next setting\nfetch_interval_minutes: 120\n',
                   '# sources not yet configured\nfetch_interval_minutes: 120\n...\n',
                   'rss_sources:\n# existing group\n- name: initial\n  url: https://example.org/initial # old link\n',
                   'rss_sources:\n  -\n    name: initial # old name\n    url: https://example.org/initial')
        for layout in layouts:
            with self.subTest(layout=layout.splitlines()[0]):
                original = password_line + layout
                self.path.write_bytes(original.encode('utf-8'))
                client = self.client()
                self.login(client)
                token = self.auth_token(client)
                client.post('/sources/add',data={'name':'new','url':'https://example.org/new','csrf_token':token})
                current = self.path.read_text(encoding='utf-8')
                self.assertIn('new',[source['name'] for source in yaml.safe_load(current)['rss_sources']])
                for source in list(yaml.safe_load(current)['rss_sources']):
                    client.post('/sources/remove',data={'name':source['name'],'csrf_token':token})
                empty = self.path.read_text(encoding='utf-8')
                self.assertEqual(yaml.safe_load(empty)['rss_sources'],[])
                self.assertTrue(empty.startswith(password_line))
                for comment in re.findall(r'#[^\r\n]+',original):
                    self.assertIn(comment,empty)
                client.post('/sources/add',data={'name':'again','url':'https://example.org/again','csrf_token':token})
                final = self.path.read_text(encoding='utf-8')
                self.assertEqual([source['name'] for source in yaml.safe_load(final)['rss_sources']],['again'])

    def test_unsupported_source_layouts_and_aliases_refuse_changes_without_leaking_secrets(self):
        password_line = f"web_password: '{self.raw['web_password']}'\n"
        cases = ('rss_sources: [{name: alpha, url: https://example.org/rss}] # flow group\n',
                 'keywords:\n  include: &words [topic]\n  require: *words\nrss_sources: []\n')
        for layout in cases:
            with self.subTest(layout=layout.splitlines()[0]):
                original = (password_line + layout).encode('utf-8')
                self.path.write_bytes(original)
                client = self.client()
                self.login(client)
                response = client.post('/sources/add',data={'name':'new','url':'https://example.org/new',
                    'csrf_token':self.auth_token(client)},follow_redirects=True)
                self.assertIn('原文件未修改',response.get_data(as_text=True))
                self.assertNotIn(self.raw['web_password'],response.get_data(as_text=True))
                self.assertEqual(self.path.read_bytes(),original)
                if '&words' in layout:
                    editor = client.get('/config/edit')
                    self.assertEqual(editor.status_code,422)
                    self.assertIn('锚点或别名',editor.get_data(as_text=True))
                    self.assertNotIn(self.raw['web_password'],editor.get_data(as_text=True))
                self.assertFalse(list(self.path.parent.glob(self.path.name + '.*.bak')))

    def test_source_test_rejects_overlong_fields_before_network(self):
        client = self.client()
        self.login(client)
        with patch('src.fetcher.fetch_source') as fetch:
            for name,url in (('x'*129,'https://example.org/rss'),('valid','https://example.org/'+'x'*2030)):
                response = client.post('/sources/test',data={'name':name,'url':url,'csrf_token':self.auth_token(client)})
                self.assertEqual(response.status_code,302)
            fetch.assert_not_called()

    def test_report_list_is_bounded_and_can_page(self):
        client = self.client()
        self.login(client)
        self.app.config["news_bot_storage"].rows = [
            {"id": n, "title": "Report %d" % n, "report_type": "weekly", "period_start": "2026-11-01", "period_end": "2026-11-08", "news_count": 2}
            for n in range(1, 4)]
        body = client.get("/reports/?per_page=1&page=2").get_data(as_text=True)
        self.assertIn("Report 2", body)
        self.assertNotIn("Report 1", body)
        self.assertNotIn("Report 3", body)
        self.assertIn("第 2 / 3 页", body)

    def test_missing_password_refuses_default_admin_with_setup_message(self):
        self.raw.pop("web_password")
        self.write_config()
        client = self.client()
        token = self.token(client)
        response = client.post("/login", data={"password": "admin", "csrf_token": token})
        self.assertEqual(response.status_code, 503)
        self.assertIn("WEB_PASSWORD", response.get_data(as_text=True))
        with client.session_transaction() as sess:
            self.assertFalse(sess.get("authenticated"))


if __name__ == "__main__":
    unittest.main()
