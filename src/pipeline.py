"""One durable workflow for CLI and Web. Network effects have explicit outcomes."""
import difflib
import logging
import re
from contextlib import ExitStack, nullcontext
from datetime import timedelta, timezone
from urllib.parse import urlsplit, urlunsplit
from pathlib import Path

from . import analyzer, fetcher, sender
from .diagnostics import RunTrace
from .storage import Storage, json_ready, utc
from .news_records import validate_records as _valid_rows

logger = logging.getLogger('news_bot.pipeline')
# The supported modern retention window has no Shanghai DST. This also works
# on Windows installations without an IANA tzdata package.
DISPLAY_ZONE = timezone(timedelta(hours=8), name='Asia/Shanghai')


def keyword_match(keyword, text, threshold=.7):
    """Literal keywords first, then configured word/window similarity."""
    keyword, text = keyword.casefold(), text.casefold()
    chinese = bool(re.search(r'[一-鿿]', keyword))
    if chinese and keyword in text:
        return True
    if not chinese and re.search(r'\b' + re.escape(keyword) + r'\b', text):
        return True
    candidates = (text[i:i+len(keyword)] for i in range(max(0,len(text)-len(keyword)+1))) if chinese else re.findall(r'\w+', text)
    return any(difflib.SequenceMatcher(None, keyword, token).ratio()>=threshold for token in candidates)


def in_push_window(config, clock):
    hour = clock.astimezone(DISPLAY_ZONE).hour
    start, end = config.push_time_window.start, config.push_time_window.end
    return (start==end or (start<=hour<end if start<end else hour>=start or hour<end))


def normalize_url(value):
    if not isinstance(value,str) or len(value)>2048:
        raise ValueError('Invalid article URL length')
    part = urlsplit(value.strip())
    if part.scheme.lower() not in ('http','https') or not part.hostname or part.username or part.password:
        raise ValueError('Invalid article URL')
    host = part.hostname.lower()
    if ':' in host:
        host = '['+host+']'
    port = part.port
    netloc = host if not port or (part.scheme.lower(), port) in (('http',80),('https',443)) else f'{host}:{port}'
    return urlunsplit((part.scheme.lower(),netloc,part.path or '/',part.query,''))


def _fit(config, items, operation):
    # An individually oversized record must not starve later smaller records.
    # Everything omitted here stays pending and can be processed after a budget
    # change; no omitted ID is ever acknowledged as delivered.
    for offset in range(len(items)):
        fitted = analyzer.fit_news_to_budget(json_ready(items[offset:]),config.ai,
            operation,interests=config.ai_interests)
        if fitted:
            ids={row['id'] for row in fitted}
            return [row for row in items[offset:] if row['id'] in ids]
    return []


def _rank(items, config):
    for item in items:
        item['source_count']=len({other['source'] for other in items if
            difflib.SequenceMatcher(None,item['title'].casefold(),other['title'].casefold()).ratio()>.7})
        item['source_weight']=next((weight for key,weight in (('tier1',3),('tier2',2))
            if item['source'] in config.source_weights.get(key,[])),1)
    items.sort(key=lambda row:(row.get('interest_score',0),row['source_weight']*row['source_count'],
                              utc(row['published_at'] or row['created_at'])),reverse=True)
    return items


def _select(config, storage, clock):
    items, invalid = _valid_rows(storage.get_pending_news(limit=1000))
    storage.skip_news(invalid,'invalid_legacy_record')
    eligible = []
    for item in items:
        published = utc(item['published_at'] or item['created_at'])
        if not clock-timedelta(hours=getattr(config,'max_news_age_hours',72))<=published<=clock+timedelta(minutes=5):
            storage.skip_news([item['id']], 'outside_news_age')
            continue
        text = item['title']+' '+(item['summary'] or '')
        kw = config.keywords
        match = lambda word: keyword_match(word, text, kw.fuzzy_threshold/100)
        if ((kw.include and not any(map(match,kw.include))) or
            (kw.require and not all(map(match,kw.require))) or any(map(match,kw.exclude))):
            storage.skip_news([item['id']], 'keywords')
            continue
        eligible.append(item)
    if config.incremental_mode:
        history = storage.get_recent_sent_titles(clock)
        distinct = []
        for item in eligible:
            if any(difflib.SequenceMatcher(None,item['title'],title).ratio()>.7 for title in history):
                storage.skip_news([item['id']], 'similar_sent_or_batch')
            else:
                distinct.append(item)
                history.append(item['title'])
        eligible = distinct
    def source_weight(item):
        return next((weight for key,weight in (('tier1',3),('tier2',2))
                     if item['source'] in config.source_weights.get(key,[])),1)
    eligible.sort(key=lambda i:(source_weight(i),utc(i['published_at'] or i['created_at'])),reverse=True)
    # Leave unprocessed rows pending; an explicit resource bound also keeps
    # quadratic title comparisons and scoring input within a finite workload.
    eligible = eligible[:getattr(config,'max_candidates_per_run',100)]
    _rank(eligible,config)
    if config.max_news_per_run and not config.ai_interests:
        eligible = eligible[:config.max_news_per_run]
    if eligible:
        fitted = _fit(config,eligible,'digest')
        if not fitted:
            raise analyzer.ModelBudgetError('No pending record fits the configured model budget')
        eligible=fitted
    if config.ai_interests and eligible:
        scores = analyzer.score_news_items(json_ready(eligible), config.ai_interests, config.ai)
        if len(scores)!=len(eligible) or any(isinstance(n,bool) or not isinstance(n,(int,float)) or not 0<=n<=10 for n in scores):
            raise ValueError('Invalid interest scores')
        selected = []
        for item, score in zip(eligible,scores):
            if score>=6:
                item['interest_score'] = score
                selected.append(item)
            else:
                storage.skip_news([item['id']], 'low_interest')
        eligible = selected
    _rank(eligible,config)
    limit = config.max_news_per_run
    eligible = eligible[:limit] if limit else eligible
    return _fit(config, eligible, 'analysis') if eligible else []


def preview_digest(config, items, recent_titles, now):
    """Select and render exactly as a new batch would, leaving delivery state untouched."""
    class Snapshot:
        def get_pending_news(self, limit):
            return items[:limit]

        def get_recent_sent_titles(self, _clock):
            return list(recent_titles)

        def skip_news(self, _ids, _reason):
            pass

    with analyzer.model_budget(config.ai):
        chosen = _select(config, Snapshot(), now)
        if not chosen:
            return {"status": "empty", "message": "没有符合当前筛选条件的新鲜新闻。"}
        analysis = analyzer.analyze_news(json_ready(chosen), config.ai)
        payload, _ = _prepare_digest_delivery(analysis, chosen)
        return {"status": "ready", "analyzed": len(chosen), **payload}


def _prepare_digest_delivery(analysis, articles):
    payload = sender.delivery_payload(analysis, len(articles))
    visible = sender.visible_article_ids(payload, articles)
    if not visible:
        raise analyzer.ModelResponseError("The rendered digest has no verifiable original links", code="no_visible_links")
    return payload, visible


def _timed_selection(config, storage, clock, trace):
    with trace.stage("selection") as step:
        chosen = _select(config, storage, clock)
        step["selected"] = len(chosen)
        if not chosen:
            step.update(status="skipped", code="no_candidates")
        return chosen


def _deliver(config, storage, batch, analysis, included_ids, clock, now, result, trace):
    delivery_clock = utc() if now is None else clock
    with trace.stage("delivery") as step:
        if not in_push_window(config, delivery_clock):
            step.update(status="waiting", code="outside_push_window")
            result.update(status="waiting", message="分析已保存；推送窗口已关闭，等待下一窗口")
            return
        storage.assert_pipeline_lock()
        attempt = storage.begin_delivery(batch["id"], delivery_clock)
        try:
            storage.assert_pipeline_lock()
            if not in_push_window(config, utc() if now is None else clock):
                storage.finish_delivery(batch["id"], attempt, "failed", now=delivery_clock)
                step.update(status="waiting", code="outside_push_window")
                result.update(status="waiting", message="推送窗口已关闭，消息尚未发送")
                return
            success = sender.send_to_dingtalk(analysis, len(included_ids), config.dingtalk)
            storage.finish_delivery(batch["id"], attempt, "succeeded" if success else "failed", now=delivery_clock)
            if success:
                step["sent"] = len(included_ids)
                result.update(status="succeeded", message="简报已发送，已按可见原文链接记录推送",
                              sent=len(included_ids), news_ids=included_ids)
            else:
                step.update(status="failed", code="dingtalk_rejected")
                result.update(status="failed", message="发送失败，批次保留以便下轮重试", errors=result["errors"] + 1)
        except sender.DeliveryUnknownError:
            storage.finish_delivery(batch["id"], attempt, "unknown", now=delivery_clock)
            step.update(status="unknown", code="receipt_unknown")
            result.update(status="partial", message="送达状态未知；已停止自动重发，请人工核对",
                          errors=result["errors"] + 1)
        except Exception:
            # A failed receipt write leaves the batch 'sending' for recovery.
            logger.error("Delivery outcome requires state recovery")
            step.update(status="failed", code="receipt_recovery")
            result.update(status="failed", message="发送或回执保存失败，请核对持久批次状态",
                          errors=result["errors"] + 1)


def run_pipeline(config, *, storage=None, fetch_only=False, now=None, trace=None):
    clock = utc(now)
    trace = trace or RunTrace()
    result = dict(status='skipped',message='没有待处理新闻',fetched=0,inserted=0,
                  selected=0,sent=0,errors=0,report_count=0,batch_id=None,news_ids=[],stages=trace.steps)
    storage = storage if storage is not None else Storage(config.mysql)
    with ExitStack() as session:
        with trace.stage("recovery") as step:
            if not session.enter_context(storage.pipeline_lock()):
                step.update(status="busy")
                return dict(result,status='busy',message='已有任务正在运行')
            storage.assert_pipeline_lock()
            storage.ensure_runtime_ready()
            storage.recover_inflight(clock)
        try:
            with trace.stage("collection") as step:
                fetched = fetcher.fetch_all(config.rss_sources,config.request_timeout,
                    allow_private_hosts=getattr(config,'allow_private_rss_hosts',[]),
                    max_news_age_hours=getattr(config,'max_news_age_hours',72),now=clock,
                    max_response_bytes=getattr(config,'rss_max_response_bytes',2097152),
                    task_timeout=getattr(config,'fetch_task_timeout',120),
                    health_path=str(Path(config.config_path).parent/'runtime'/'source-health.sqlite3')
                                if getattr(config,'config_path','') else None)
                step["fetched"] = result['fetched'] = len(fetched)
                failures = getattr(fetched, "failures", 0)
                result["errors"] += failures
                if failures:
                    step.update(status="partial", code="rss_failed", failed_sources=getattr(fetched, "failed_sources", []))
            with trace.stage("ingestion") as step:
                unique = {}
                for item in fetched:
                    try:
                        item.url = normalize_url(item.url)
                        published = utc(item.published_at) if item.published_at else None
                        if published and not clock-timedelta(hours=getattr(config,'max_news_age_hours',72))<=published<=clock+timedelta(minutes=5):
                            continue
                        unique.setdefault(item.url,item)
                    except (ValueError,TypeError):
                        result['errors'] += 1
                        step.update(status="partial", code="invalid_article")
                step["inserted"] = result['inserted'] = storage.ingest_news(list(unique.values()),now=clock)
        except Exception:
            logger.error('Collection or transactional persistence failed')
            result['errors'] += 1
        if fetch_only:
            return dict(result,status='partial' if result['errors'] else 'succeeded',
                        message='采集已入库；待分析记录保留' if not result['errors'] else '采集或保存失败，未消费待处理记录')
        budget = getattr(analyzer,'model_budget',lambda _:nullcontext())
        with budget(config.ai):
            if not in_push_window(config,clock):
                result.update(status='waiting',message='当前不在推送窗口，待处理记录已保留')
                with trace.stage("delivery") as step:
                    step.update(status="waiting", code="outside_push_window")
            else:
                try:
                    batch = storage.get_retry_batch(now=clock,lease_seconds=config.ai.total_timeout_seconds+30)
                    if (batch and batch['status'] in {'analysis_failed', 'send_failed'} and
                            storage.retry_should_yield(batch['id'])):
                        try:
                            fresh = _timed_selection(config, storage, clock, trace)
                        except analyzer.ModelResponseError:
                            fresh = []
                            logger.warning('Fresh selection could not run before an eligible retry')
                        if fresh:
                            batch = storage.create_batch([item['id'] for item in fresh], clock)
                    if batch is not None and batch['analysis'] is None:
                        valid,invalid=_valid_rows(batch['items'])
                        fitted=_fit(config,valid,'analysis')
                        if len(fitted)!=len(batch['items']):
                            batch=storage.resize_analysis_batch(batch['id'],[row['id'] for row in fitted],clock)
                            storage.skip_news(invalid,'invalid_legacy_record')
                    if batch is None:
                        chosen = _timed_selection(config, storage, clock, trace)
                        batch = storage.create_batch([i['id'] for i in chosen],clock) if chosen else None
                    if batch:
                        result.update(selected=len(batch['items']),batch_id=batch['id'],news_ids=[i['id'] for i in batch['items']])
                        included_ids = result['news_ids']
                        analysis = batch['analysis']
                        with trace.stage("analysis") as step:
                            if analysis is None:
                                try:
                                    storage.assert_pipeline_lock()
                                    analysis = analyzer.analyze_news(json_ready(batch['items']),config.ai)
                                    analysis['_selection']=[{'news_id':row['id'],'independent_source_count':row['source_count'],
                                        'source_weight':row['source_weight']} for row in _rank(batch['items'],config)]
                                    analysis['_delivery'], included_ids = _prepare_digest_delivery(analysis, batch['items'])
                                    storage.set_batch_analysis(batch['id'], analysis, clock, included_ids=included_ids)
                                    result['news_ids'] = included_ids
                                except Exception:
                                    storage.fail_analysis(batch['id'],clock)
                                    raise
                            else:
                                step["status"] = "reused"
                            step["linked"] = len(included_ids)
                        analysis['_delivery'] = sender.delivery_payload(analysis, len(batch['items']))
                        result['visible_links'] = analysis['_delivery']['visible_links']
                        _deliver(config, storage, batch, analysis, included_ids, clock, now, result, trace)
                except Exception:
                    logger.error('Analysis, delivery or outcome persistence failed')
                    result.update(status='failed',message='分析或状态保存失败；下一轮将按持久化状态恢复',errors=result['errors']+1)
            try:
                from .reports import run_reports
                with trace.stage("reports") as step:
                    reports = run_reports(config,storage,clock)
                    step.update(generated=reports["generated"], insufficient=reports["insufficient"],
                                periods=reports["periods"])
                    result['report_count'] = reports['generated']
                    result['errors'] += reports['errors']
                    if reports["errors"]:
                        step.update(status="partial" if reports["generated"] else "failed", code="report_period_failed")
                    elif not reports["generated"]:
                        step.update(status="skipped", code="report_claimed" if reports.get("busy") else "no_report_due")
                    if reports['generated'] and result['status']=='skipped':
                        result.update(status='succeeded',message='周期报告已补齐')
            except Exception:
                logger.error('Report eligibility check failed')
                result['errors'] += 1
        try:
            storage.cleanup_old(days=getattr(config,'retention_days',400),now=clock)
        except Exception:
            logger.error('Retention cleanup failed')
            result['errors'] += 1
        result['model_usage'] = list(analyzer.get_model_usage())
        if result['errors'] and result['status'] not in ('failed','partial'):
            result.update(status='partial' if result['sent'] or result['report_count'] else 'failed',
                          message='本轮部分步骤失败；已保存实际结果并保留可重试任务')
        return result
