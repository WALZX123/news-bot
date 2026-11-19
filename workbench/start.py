from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.client import HTTPConnection, HTTPException
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
START_TIMEOUT = 25


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


@contextmanager
def instance_lock(path):
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            def lock():
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            def lock():
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            lock()
        except OSError as exc:
            if exc.errno not in {11, 13, 36}:
                raise
            yield False
        else:
            # The OS releases the lock when this handle or process exits.
            yield True


def matches(port, path, field, expected, key=None):
    if type(port) is not int or not 0 < port <= 65535:
        return False
    connection = HTTPConnection(HOST, port, timeout=0.4)
    try:
        headers = {"X-Workbench-Key": key} if key else {}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            return False
        value = json.loads(response.read(4096))
        return isinstance(value, dict) and value.get(field) == expected
    except (HTTPException, OSError, ValueError):
        return False
    finally:
        connection.close()


def active_status(runtime):
    status = read_json(runtime / "processes.json")
    instance = status.get("instance")
    if not isinstance(instance, str) or len(instance) != 32 or any(c not in "0123456789abcdef" for c in instance):
        return None
    if matches(status.get("port"), "/_workbench", "instance", instance):
        return status
    return None


def stop_gateway(process):
    if process.stdin:
        process.stdin.close()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def wait_until(result, running, message):
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline and running():
        value = result()
        if value:
            return value
        time.sleep(0.1)
    raise RuntimeError(message)


def run(runtime):
    if not (ROOT / "dist" / "index.html").is_file():
        raise RuntimeError("页面尚未构建，请进入 workbench 执行 npm.cmd run build。")
    node = shutil.which("node")
    if not node:
        raise RuntimeError("需要 Node.js 24。请通过工作台启动脚本加载工作区环境。")
    preferred = int(os.environ.get("NEWS_WORKBENCH_PORT", 4380))
    if preferred != 0 and not 1024 <= preferred <= 65535:
        raise ValueError("网页首选端口应为 0 或 1024 至 65535。")
    instance = secrets.token_hex(16)
    key = secrets.token_urlsafe(32)
    ready = runtime / f"gateway-{instance}.json"
    stop_signal = runtime / f"stop-{instance}.request"
    status_path = runtime / "processes.json"
    status_tmp = runtime / f"status-{instance}.tmp"
    status_path.unlink(missing_ok=True)
    os.environ["NEWS_WORKBENCH_KEY"] = key
    os.environ["NEWS_WORKBENCH_RUNTIME"] = str(runtime)

    backend_log = logging.FileHandler(runtime / "fastapi.log", encoding="utf-8", mode="w")
    backend_log.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    loggers = [logging.getLogger(name) for name in ("uvicorn", "news_workbench")]
    for logger in loggers:
        logger.addHandler(backend_log)
        logger.setLevel(logging.INFO)
        logger.propagate = False

    gateway = None
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener, \
            (runtime / "gateway.log").open("w", encoding="utf-8") as gateway_log:
        listener.bind((HOST, 0))
        data_port = listener.getsockname()[1]
        backend = uvicorn.Server(uvicorn.Config(
            "backend.service:app", log_config=None, access_log=False,
            limit_concurrency=64, timeout_keep_alive=3, timeout_graceful_shutdown=4,
        ))

        def serve_backend():
            try:
                backend.run(sockets=[listener])
            except Exception:
                loggers[0].exception("Data service failed")

        thread = threading.Thread(target=serve_backend, name="workbench-data", daemon=True)
        thread.start()
        try:
            wait_until(lambda: matches(data_port, "/alive", "ok", True, key),
                       thread.is_alive, "数据服务未就绪，请查看 runtime/fastapi.log。")
            environment = dict(os.environ, NEWS_WORKBENCH_INSTANCE=instance,
                               NEWS_WORKBENCH_PORT=str(preferred), NEWS_WORKBENCH_DATA_PORT=str(data_port),
                               NEWS_WORKBENCH_READY_FILE=str(ready))
            gateway = subprocess.Popen([node, "server.ts"], cwd=ROOT, env=environment,
                                       stdin=subprocess.PIPE, stdout=gateway_log, stderr=subprocess.STDOUT)

            def gateway_port():
                record = read_json(ready)
                port = record.get("port")
                if record.get("instance") == instance and matches(port, "/_workbench", "instance", instance):
                    return port
                return None

            port = wait_until(gateway_port, lambda: gateway.poll() is None and thread.is_alive(),
                              "网页服务未就绪，请查看 runtime/gateway.log。")
            status = {"instance": instance, "port": port, "backend_port": data_port,
                      "url": f"http://{HOST}:{port}/", "pid": os.getpid(), "gateway_pid": gateway.pid}
            status_tmp.write_text(json.dumps(status, indent=2), encoding="utf-8")
            status_tmp.replace(status_path)
            print("新闻工作台已启动 " + status["url"], flush=True)
            while gateway.poll() is None and thread.is_alive():
                if stop_signal.exists():
                    return
                time.sleep(0.25)
            raise RuntimeError("本地服务退出，请查看 runtime 里的运行日志。")
        except KeyboardInterrupt:
            pass
        finally:
            if gateway is not None:
                stop_gateway(gateway)
            backend.should_exit = True
            thread.join(timeout=8)
            for path in (status_path, status_tmp, stop_signal, ready, ready.with_suffix(".json.tmp")):
                path.unlink(missing_ok=True)
            for logger in loggers:
                logger.removeHandler(backend_log)
            backend_log.close()


def main():
    parser = argparse.ArgumentParser(description="本地新闻工作台")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--status", action="store_true")
    actions.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    runtime = Path(os.environ.get("NEWS_WORKBENCH_RUNTIME", ROOT / "runtime")).resolve()
    runtime.mkdir(parents=True, exist_ok=True)

    if args.status or args.stop:
        status = active_status(runtime)
        if not status:
            print("新闻工作台当前未就绪。")
        elif args.status:
            print(status["url"])
        else:
            (runtime / f'stop-{status["instance"]}.request').touch()
            wait_until(lambda: read_json(runtime / "processes.json").get("instance") != status["instance"],
                       lambda: True, "停止超时，请查看运行日志。")
            print("新闻工作台已停止。")
        return

    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        with instance_lock(runtime / "launch.lock") as acquired:
            if acquired:
                run(runtime)
                return
        status = active_status(runtime)
        if status:
            print("新闻工作台已在运行 " + status["url"], flush=True)
            return
        time.sleep(0.2)
    raise RuntimeError("已有工作台正在启动，请查看 runtime 里的运行日志。")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
