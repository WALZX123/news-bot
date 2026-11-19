#!/usr/bin/env python
"""RSS news bot: one workflow shared with the Web UI; explicit DB migration."""
import argparse
from datetime import datetime
import json
import logging
import logging.handlers
from pathlib import Path
import signal
import sys
from src.automation import request_mode, request_stop, run_scheduler, scheduler_status

from src.config import load_config, resolve_config_path
from src.diagnostics import RunTrace
from src.storage import Storage

logger = logging.getLogger('news_bot')


def setup_logging(level, log_file):
    logging.basicConfig(level=getattr(logging,level.upper(),logging.INFO),
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=[logging.StreamHandler(sys.stdout),logging.handlers.RotatingFileHandler(
            log_file,encoding='utf-8',maxBytes=10*1024*1024,backupCount=5)])


def run_once(config_path=None, *, config=None, storage=None, storage_provider=None, fetch_only=False,
             enrich=False, now=None):
    """Return a durable task outcome. Dependency failures never kill scheduling."""
    trace = RunTrace()
    try:
        from src.pipeline import run_pipeline
        with trace.stage("prepare"):
            config = config if config is not None else load_config(config_path)
            if storage is None and storage_provider is not None:
                storage = storage_provider(config.mysql)
            if storage is None:
                storage = Storage(config.mysql)
        result = run_pipeline(config, storage=storage, fetch_only=fetch_only, now=now, trace=trace)
        if enrich and config.auto_summarize and result['status'] != 'busy':
            from src import analyzer
            from src.editorial import summarize_recent
            usage = list(result.get("model_usage", []))
            try:
                with trace.stage("summaries") as step, analyzer.model_budget(config.ai):
                    metrics = {}
                    try:
                        result["summaries"] = summarize_recent(config, storage, metrics=metrics)
                        step.update(metrics, generated=result["summaries"])
                    finally:
                        result["model_usage"] = usage + analyzer.get_model_usage()
                    if metrics.get("failed"):
                        step.update(status="partial" if result["summaries"] else "failed",
                                    code=(metrics.get("error_codes") or ["summary_failed"])[0])
                        result["errors"] += metrics["failed"]
                        result["summary_error"] = True
                    elif not metrics.get("attempted"):
                        step.update(status="skipped", code="no_pending_summaries")
            except Exception as error:
                logger.warning("Article summary task failed (%s)", type(error).__name__)
                result["summary_error"] = True
                result["errors"] += 1
            if result.get("summary_error") and result["status"] in {"succeeded", "skipped"}:
                result.update(status="partial", message="本轮已保存可用结果，部分摘要生成失败。")
        return result
    except Exception:
        logger.error('Task dependency failed; details are withheld from public output')
        return dict(status='failed',message='任务依赖失败，请检查配置、数据库及迁移状态',
                    fetched=0,inserted=0,selected=0,sent=0,errors=1,report_count=0,stages=trace.steps)


def main(argv=None):
    parser = argparse.ArgumentParser(description='新闻机器人：RSS → AI → 钉钉')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--once',action='store_true',help='执行一次完整流水线')
    mode.add_argument('--init-db',action='store_true',help='显式建库/兼容迁移（先备份）')
    mode.add_argument('--check-db','--status',dest='check_db',action='store_true',help='只读检查数据库结构、计数和未知送达批次')
    mode.add_argument('--resolve-batch',metavar='BATCH_ID',help='按已经核验的平台回执解除unknown；此命令不发送消息')
    mode.add_argument('--scheduler-status',action='store_true',help='查询独立调度器状态')
    mode.add_argument('--scheduler-stop',action='store_true',help='请求独立调度器在本轮结束后停止')
    mode.add_argument('--scheduler-mode',choices=['collect','full'],help='本轮结束后切换后台运行模式')
    parser.add_argument('--fetch-only',action='store_true',help='只采集入库，不评分、分析、推送或报告')
    parser.add_argument('--scheduler-collect',action='store_true',help='定时运行，仅采集入库')
    parser.add_argument('--wait-first-run',action='store_true',help='等待一个采集间隔后执行首轮调度')
    parser.add_argument('--first-due',help='首轮计划时间，使用带时区的 ISO 8601 时间')
    parser.add_argument('--interval',type=int,default=0,help='定时分钟数，0使用配置')
    parser.add_argument('--config',default=None,help='配置文件路径')
    parser.add_argument('--legacy-timezone',default=None,help='旧库系统时间原时区，如 +08:00；仅首次旧库迁移需要，原RSS发布时间不变')
    parser.add_argument('--delivery-outcome',choices=['confirmed-sent','confirmed-not-sent'])
    parser.add_argument('--receipt-note',help='人工核验的平台回执ID或记录；不要填写密码或Token')
    args = parser.parse_args(argv)
    if args.scheduler_status or args.scheduler_stop or args.scheduler_mode:
        path = resolve_config_path(args.config)
        if args.scheduler_stop:
            print(json.dumps({"stop_requested": request_stop(path)}))
        elif args.scheduler_mode:
            print(json.dumps({"mode_requested": request_mode(path, args.scheduler_mode)}))
        else:
            print(json.dumps(scheduler_status(path),ensure_ascii=False))
        return 0
    try:
        config = load_config(args.config)
    except (OSError, ValueError, TypeError):
        print("无法读取运行配置，请检查 config.yaml 的格式和字段。", file=sys.stderr)
        return 2
    if args.init_db:
        Storage.initialize(config.mysql,legacy_timezone=args.legacy_timezone)
        print('数据库兼容迁移已完成；历史记录已保留')
        return 0
    if args.check_db:
        result = Storage(config.mysql).check_database()
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if result['ready'] else 2
    if args.resolve_batch:
        if not args.delivery_outcome or not args.receipt_note:
            parser.error('--resolve-batch requires --delivery-outcome and --receipt-note')
        result=Storage(config.mysql).reconcile_unknown(args.resolve_batch,args.delivery_outcome,args.receipt_note)
        print(json.dumps(result,ensure_ascii=False))
        return 0
    setup_logging(config.log_level,config.log_file)
    if args.once or args.fetch_only:
        result = run_once(args.config,fetch_only=args.fetch_only)
        print(json.dumps(result,ensure_ascii=False))
        return 1 if result['status']=='failed' else 0
    if args.interval<0:
        parser.error('--interval must be non-negative')
    first_due = None
    if args.first_due:
        try:
            first_due = datetime.fromisoformat(args.first_due)
            if first_due.tzinfo is None:
                raise ValueError()
        except ValueError:
            parser.error('--first-due requires a timezone-aware ISO 8601 timestamp')
    stop = False
    def handle_shutdown(signum, frame):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGINT,handle_shutdown)
    signal.signal(signal.SIGTERM,handle_shutdown)
    scheduled_storage = None

    def get_storage(mysql_config):
        nonlocal scheduled_storage
        if scheduled_storage is None or scheduled_storage.config != mysql_config:
            scheduled_storage = Storage(mysql_config)
        return scheduled_storage

    def execute(collect_only):
        result = run_once(args.config, storage_provider=get_storage, fetch_only=collect_only, enrich=True)
        logger.info('Task result: %s',result['status'])
        return result

    config_file = Path(config.config_path)
    version = None
    current_config = config

    def check_interval():
        nonlocal version, current_config
        stat = config_file.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        if signature != version:
            current_config = load_config(str(config_file))
            version = signature
        return current_config

    started = run_scheduler(
        config, check_interval, execute,
        interval=args.interval, collect_only=args.scheduler_collect,
        wait_first=args.wait_first_run, first_due=first_due, interrupted=lambda: stop,
    )
    if not started:
        print("调度器已在运行。")
    return 0


if __name__=='__main__':
    raise SystemExit(main())
