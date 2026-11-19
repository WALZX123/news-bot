"""Persistent state for the command-line scheduler, independent of the workbench."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from threading import Event, Thread
import time
from uuid import uuid4


def runtime_dir(config_path):
    return Path(config_path).parent / "runtime"


def _read(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, value):
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.04)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _lock(path, *, create=False):
    try:
        handle = path.open("a+b" if create else "r+b")
    except FileNotFoundError:
        yield False
        return
    with handle:
        if create and not handle.seek(0, os.SEEK_END):
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in {11, 13, 36}:
                raise
            yield False
        else:
            yield True


def scheduler_status(config_path):
    directory = runtime_dir(config_path)
    lockfile = directory / "automation.lock"
    state = _read(directory / "automation.json")
    active = False
    if lockfile.is_file():
        with _lock(lockfile) as available:
            active = not available
    mode_request = _read(directory / "automation.mode")
    heartbeat = state.get("heartbeat_at")
    stalled = bool(active and heartbeat and
                   datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat) > timedelta(seconds=45))
    slow = bool(active and state.get("state") == "running" and state.get("attention_after") and
                datetime.now(timezone.utc) > datetime.fromisoformat(state["attention_after"]))
    return {
        "active": active,
        "state": "stalled" if stalled else "slow" if slow else state.get("state", "starting") if active else "stopped",
        "mode": state.get("mode") if active else None,
        "instance": state.get("instance") if active else None,
        "started_at": state.get("started_at") if active else None,
        "next_run_at": state.get("next_run_at") if active else None,
        "heartbeat_at": heartbeat if active else None,
        "requested_mode": mode_request.get("mode") if active and mode_request.get("instance") == state.get("instance")
                          and mode_request.get("mode") != state.get("mode") else None,
        "stopping": bool(active and state.get("instance") and
                         _read(directory / "automation.stop").get("instance") == state.get("instance")),
        "recent_runs": state.get("recent_runs", []),
    }


def request_stop(config_path):
    status = scheduler_status(config_path)
    if not status["active"] or not status["instance"]:
        return False
    _write(runtime_dir(config_path) / "automation.stop", {"instance": status["instance"]})
    return True


def request_mode(config_path, mode):
    if mode not in {"collect", "full"}:
        raise ValueError("Unknown automation mode")
    status = scheduler_status(config_path)
    if not status["active"] or not status["instance"] or status["stopping"]:
        return False
    _write(runtime_dir(config_path) / "automation.mode", {"instance": status["instance"], "mode": mode})
    return True


def run_scheduler(config, get_config, execute, *, interval=None, collect_only=False, wait_first=False, first_due=None,
                  interrupted=lambda: False):
    directory = runtime_dir(config.config_path)
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / "automation.json"
    stop_path = directory / "automation.stop"
    mode_path = directory / "automation.mode"
    with _lock(directory / "automation.lock", create=True) as acquired:
        if not acquired:
            return False
        now = lambda: datetime.now(timezone.utc)
        instance = uuid4().hex
        state = {
            "instance": instance, "mode": "collect" if collect_only else "full",
            "started_at": now().isoformat(), "recent_runs": _read(state_path).get("recent_runs", [])[:50],
        }
        previous_end = time.monotonic()
        cadence = (interval or config.fetch_interval_minutes) * 60
        run_at = previous_end + (cadence if wait_first else 0)
        if first_due is not None:
            run_at = previous_end + max(0, (first_due - now()).total_seconds())
            previous_end = run_at - cadence

        def stopping():
            return interrupted() or _read(stop_path).get("instance") == instance

        def publish():
            state["heartbeat_at"] = now().isoformat()
            _write(state_path, state)

        def ping(done):
            while not done.wait(5):
                publish()

        try:
            while not stopping():
                requested = _read(mode_path)
                if requested.get("instance") == instance and requested.get("mode") in {"collect", "full"}:
                    if state["mode"] != requested["mode"]:
                        state["mode"] = requested["mode"]
                        run_at = time.monotonic()
                delay = run_at - time.monotonic()
                if delay > 0:
                    next_run = (now() + timedelta(seconds=delay)).replace(microsecond=0).isoformat()
                    state.update(state="waiting", next_run_at=next_run)
                    publish()
                    time.sleep(min(delay, 2))
                    try:
                        current = get_config()
                    except (ValueError, OSError, TypeError):
                        continue
                    run_at = previous_end + (interval or current.fetch_interval_minutes) * 60
                    continue

                try:
                    current = get_config()
                except (ValueError, OSError, TypeError):
                    current = config
                start = now()
                attention = current.fetch_task_timeout + current.ai.total_timeout_seconds + 90
                state.update(state="running", next_run_at=None,
                             attention_after=(start + timedelta(seconds=attention)).isoformat())
                publish()
                done = Event()
                recorder = Thread(target=ping, args=(done,), daemon=True)
                recorder.start()
                try:
                    result = execute(state["mode"] == "collect")
                finally:
                    done.set()
                    recorder.join()
                outcome = {field: result[field] for field in (
                    "status", "message", "fetched", "inserted", "selected", "sent", "errors",
                    "report_count", "batch_id", "model_usage", "visible_links", "summaries", "summary_error", "stages",
                ) if field in result}
                outcome.update(started_at=start.isoformat(), finished_at=now().isoformat(), mode=state["mode"])
                state["recent_runs"] = [outcome, *state["recent_runs"]][:50]
                previous_end = time.monotonic()
                try:
                    current = get_config()
                except (ValueError, OSError, TypeError):
                    current = config
                run_at = previous_end + (interval or current.fetch_interval_minutes) * 60
                publish()
        finally:
            state.update(state="stopped", next_run_at=None)
            publish()
            if _read(stop_path).get("instance") == instance:
                stop_path.unlink(missing_ok=True)
            if _read(mode_path).get("instance") == instance:
                mode_path.unlink(missing_ok=True)
    return True
