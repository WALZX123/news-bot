"""Calendar report periods use Shanghai dates and half-open UTC queries."""
from datetime import date, datetime, time, timedelta, timezone
from .storage import utc
import json
import logging
from collections import defaultdict
from . import analyzer
from .diagnostics import failure_code
from .news_records import validate_records

logger = logging.getLogger('news_bot.reports')

SHANGHAI = timezone(timedelta(hours=8), name='Asia/Shanghai')
FORMAT_FAILURES = {
    "model.schema_fields", "model.schema_report", "model.schema_events", "model.schema_list",
    "model.schema_text", "model.invalid_evidence", "model.incomplete_model_output", "model.invalid_json",
}


def _floor(kind, day):
    if kind == 'weekly':
        return day - timedelta(days=day.weekday())
    if kind == 'monthly':
        return day.replace(day=1)
    if kind == 'quarterly':
        return date(day.year, 1 + 3 * ((day.month - 1) // 3), 1)
    if kind == 'yearly':
        return date(day.year, 1, 1)
    raise ValueError('Invalid report type')


def _move(kind, start, delta=1):
    if kind == 'weekly':
        return start + timedelta(weeks=delta)
    months = {'monthly': 1, 'quarterly': 3, 'yearly': 12}[kind] * delta
    index = start.year * 12 + start.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def completed_period(kind, now):
    end = _floor(kind, utc(now).astimezone(SHANGHAI).date())
    return _move(kind, end, -1), end


def query_boundary(day):
    return datetime.combine(day, time.min, tzinfo=SHANGHAI).astimezone(timezone.utc)


def _sample(rows, limit=200):
    if limit<1:
        raise ValueError('A report sample must contain at least one record')
    if len(rows) <= limit:
        return rows
    if limit==1:
        return [rows[len(rows)//2]]
    groups = defaultdict(list)
    for index,row in enumerate(rows):
        groups[row.get('source', '')].append(index)
    if len(groups)>limit:
        return [rows[round(i*(len(rows)-1)/(limit-1))] for i in range(limit)]
    chosen = set()
    quota = max(1, limit // len(groups))
    for indices in groups.values():
        count = min(quota, len(indices))
        for i in range(count):
            chosen.add(indices[round(i * (len(indices)-1) / (count-1))] if count>1 else indices[len(indices)//2])
    remaining = [i for i in range(len(rows)) if i not in chosen]
    missing = min(limit-len(chosen), len(remaining))
    for i in range(max(0, missing)):
        chosen.add(remaining[round(i * (len(remaining)-1) / (missing-1))] if missing>1 else remaining[len(remaining)//2])
    return [rows[i] for i in sorted(chosen)[:limit]]


def _period_job(config, storage, kind, start, end, clock, earliest):
    result = {"report_type": kind, "period_start": start.isoformat(), "period_end": end.isoformat()}
    lower, upper = query_boundary(start), query_boundary(end)
    claimed = False
    phase = "read"
    try:
        total = storage.count_news_for_period(lower, upper)
        result["available_news"] = total
        if total < 10:
            return dict(result, status="insufficient", code="insufficient_material")
        storage.assert_pipeline_lock("reports")
        previous = storage.get_report_period(kind, start, end)
        compact_retry = previous and previous["status"] == "failed" and previous["error_code"] in FORMAT_FAILURES
        claimed = storage.claim_report(kind, start, end, clock, lease_seconds=config.ai.total_timeout_seconds + 30)
        if not claimed:
            return dict(result, status="skipped", code="report_claimed")
        rows, invalid = validate_records(storage.get_news_for_period(lower, upper))
        if len(rows) < 10:
            storage.fail_report(kind, start, end, clock, error_code="insufficient_material")
            return dict(result, status="insufficient", code="insufficient_material")
        phase = "budget"
        sampled = _sample(rows, limit=48 if compact_retry else 200)
        for _ in range(10):
            fitted = analyzer.fit_news_to_budget(sampled, config.ai, "report", report_type=kind,
                                                 period_start=start.isoformat(), period_end=end.isoformat())
            if len(fitted) == len(sampled) and fitted:
                break
            if not fitted:
                raise analyzer.ModelBudgetError("Report has no capacity in this task budget")
            sampled = _sample(rows, len(fitted))
        else:
            raise analyzer.ModelBudgetError("Report representative sample exceeds budget")
        result["sampled_news"] = len(sampled)
        phase = "model"
        report = analyzer.generate_report(sampled, kind, start.isoformat(), end.isoformat(), config.ai)
        phase = "persist"
        storage.assert_pipeline_lock("reports")
        label = {"weekly": "周报", "monthly": "月报", "quarterly": "季报", "yearly": "年报"}[kind]
        report["title"] = f"{label} {start.isoformat()} 至 {end.isoformat()}（结束日不含）"
        report["_coverage"] = {"timezone": "Asia/Shanghai", "period_start": start.isoformat(),
            "period_end_exclusive": end.isoformat(), "available_news": total, "sampled_news": len(sampled),
            "first_retained_news": earliest.isoformat(), "completeness": "not_established",
            "excluded_invalid_records_in_query": len(invalid),
            "note": "覆盖范围依据当前留存资料和代表性样本。"}
        if report.get("_omitted_events"):
            report["_coverage"]["note"] += f" 省略了 {report['_omitted_events']} 条超限或未通过校验的事件。"
        if report.get("_omitted_domains"):
            report["_coverage"]["note"] += f" 省略了 {report['_omitted_domains']} 条超限或未通过校验的领域分析。"
        if compact_retry:
            report["_coverage"]["note"] += " 格式异常后的重跑使用了缩小的分来源代表性样本。"
        report_id = storage.save_report(kind, start, end, report["title"], json.dumps(report, ensure_ascii=False),
                                        total, now=clock)
        return dict(result, status="succeeded", report_id=report_id)
    except Exception as error:
        code = phase + "." + failure_code(error)
        logger.error("Report generation failed for %s %s (%s)", kind, start.isoformat(), code)
        if claimed:
            try:
                storage.assert_pipeline_lock("reports")
                storage.fail_report(kind, start, end, clock, error_code=code)
            except Exception:
                logger.error("Report failure state could not be recorded")
        return dict(result, status="failed", code=code, budget_exhausted=isinstance(error, analyzer.ModelBudgetError))


def retry_report(config, storage, kind, start, end, now=None):
    clock = utc(now)
    _, boundary = completed_period(kind, clock)
    if start != _floor(kind, start) or end != _move(kind, start) or end > boundary:
        return {"status": "failed", "code": "invalid_period"}
    with storage.pipeline_lock("reports") as acquired:
        if not acquired:
            return {"status": "busy", "code": "report_claimed"}
        period = storage.get_report_period(kind, start, end)
        if not period or period["status"] != "failed":
            return {"status": "skipped", "code": "period_not_failed"}
        earliest = storage.get_first_news_time()
        if earliest is None:
            return {"status": "insufficient", "code": "insufficient_material"}
        with analyzer.model_budget(config.ai):
            result = _period_job(config, storage, kind, start, end, clock, earliest)
            result["model_usage"] = analyzer.get_model_usage()
        return result


def run_reports(config, storage, now):
    result = {'generated':0, 'errors':0, 'insufficient':0, 'periods':[]}
    enabled = [kind for kind in ('weekly','monthly','quarterly','yearly') if getattr(config.reports,kind)]
    if not enabled:
        return result
    clock = utc(now)
    with storage.pipeline_lock('reports') as acquired:
        if not acquired:
            result["busy"] = True
            return result
        earliest = storage.get_first_news_time()
        if earliest is None:
            return result
        retention_start = (clock-timedelta(days=getattr(config,'retention_days',400))).astimezone(SHANGHAI).date()
        first = max(earliest.astimezone(SHANGHAI).date(), retention_start)
        for kind in enabled:
            completed = storage.get_completed_report_periods(kind)
            _, stop = completed_period(kind,clock)
            start = _floor(kind,first)
            # Revisit holes, not just MAX(period_end): a later success must not
            # hide an earlier failure. At most ~60 weeks/15 months are retained.
            while start < stop:
                end = _move(kind,start)
                if (start,end) in completed:
                    start = end
                    continue
                job = _period_job(config, storage, kind, start, end, clock, earliest)
                status = job["status"]
                if status in {"succeeded", "failed"}:
                    result["periods"].append(job)
                result["generated"] += int(status == "succeeded")
                result["insufficient"] += int(status == "insufficient")
                result["errors"] += int(status == "failed")
                if job.get("budget_exhausted"):
                    return result
                start = end
    return result
