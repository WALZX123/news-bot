"""The Web buttons schedule the exact same pipeline as the command line."""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, current_app, jsonify, request
from web.auth import get_config, get_storage, login_required

logger = logging.getLogger("news_bot.web")
actions_bp = Blueprint("actions", __name__)
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="news-bot")
_slots = threading.BoundedSemaphore(2)
JOB_HEARTBEAT_SECONDS = 30


def _heartbeat(storage, job_id, stop):
    while not stop.wait(JOB_HEARTBEAT_SECONDS):
        try:
            storage.update_job(job_id, "running", {"message": "任务正在执行。"})
        except Exception as error:
            logger.error("Web job heartbeat failed (%s)", type(error).__name__)


def _work(storage, config, fetch_only, job_id):
    stop = threading.Event()
    heartbeat = None
    try:
        storage.update_job(job_id, "running", {"message": "任务正在执行。"})
        thread = threading.Thread(target=_heartbeat, args=(storage, job_id, stop),
                                  name="news-bot-job-heartbeat", daemon=True)
        thread.start()
        heartbeat = thread
        from main import run_once
        result = run_once(config_path=config.config_path, config=config,
                          storage=storage, fetch_only=fetch_only)
        status = result["status"]
    except Exception as error:
        # External exception bodies can contain keys and remote HTML.
        logger.error("Web job failed (%s)", type(error).__name__)
        status, result = "failed", {"message": "执行失败，请检查服务日志或数据库迁移状态。"}
    finally:
        # No renewal may overwrite the terminal result, even when a DB write was slow.
        stop.set()
        if heartbeat is not None:
            heartbeat.join()
        try:
            storage.update_job(job_id, status, result)
        except Exception as persistence_error:
            logger.error("Web job persistence failed (%s)", type(persistence_error).__name__)
        finally:
            _slots.release()


def _trigger(fetch_only):
    if not _slots.acquire(blocking=False):
        return jsonify(status="busy", message="任务已满，请等待现有任务结束。"), 429
    storage = None
    try:
        config = get_config()
        storage = get_storage()
        job_id = storage.create_job("fetch" if fetch_only else "analyze")
        if job_id is None:
            _slots.release()
            return jsonify(status="busy", message="已有任务执行中，请稍后再试。"), 429
        _executor.submit(_work, storage, config, fetch_only, job_id)
    except Exception as error:
        _slots.release()
        logger.error("Cannot schedule Web job (%s)", type(error).__name__)
        if storage is not None and "job_id" in locals() and job_id:
            try:
                storage.update_job(job_id, "failed", {"message": "任务未能启动。"})
            except Exception:
                pass
        return jsonify(status="failed", message="任务未能启动。请确认已在独立数据库执行初始化命令。"), 503
    return jsonify(job_id=job_id, status="queued", message="任务已排队。"), 202


@actions_bp.post("/fetch")
@login_required
def trigger_fetch():
    return _trigger(True)


@actions_bp.post("/analyze")
@login_required
def trigger_analyze():
    return _trigger(False)


@actions_bp.get("/status")
@login_required
def job_status():
    job_id = request.args.get("job_id", "")
    if not job_id or len(job_id) > 128:
        return jsonify(status="invalid", message="任务编号无效。"), 400
    try:
        storage = get_storage()
        storage.recover_jobs()
        job = storage.get_job(job_id)
    except Exception as error:
        logger.error("Cannot read Web job (%s)", type(error).__name__)
        return jsonify(status="unavailable", message="暂时无法查询任务，请稍后重试。"), 503
    if not job:
        return jsonify(status="not_found", message="任务不存在或记录已过期。"), 404
    return jsonify({key: job.get(key) for key in
                    ("id", "status", "message", "result", "created_at", "updated_at")})
