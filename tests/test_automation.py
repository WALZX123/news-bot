from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from threading import Event
import json
import subprocess
import sys
import time
import unittest

from src.automation import request_stop, run_scheduler, scheduler_status
from src.config import AppConfig


class AutomationTest(unittest.TestCase):
    def test_records_a_run_and_keeps_status_after_stopping(self):
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "config.yaml")
            config = AppConfig(config_path=path)

            def execute(collect_only):
                self.assertTrue(collect_only)
                self.assertEqual(scheduler_status(path)["state"], "running")
                self.assertTrue(request_stop(path))
                return {"status": "succeeded", "inserted": 4, "fetched": 5, "sent": 0}

            self.assertTrue(run_scheduler(config, lambda: config, execute, collect_only=True))
            status = scheduler_status(path)
            self.assertFalse(status["active"])
            self.assertEqual("stopped", status["state"])
            self.assertEqual(4, status["recent_runs"][0]["inserted"])
            self.assertEqual("collect", status["recent_runs"][0]["mode"])

    def test_waiting_worker_excludes_second_worker_and_can_stop(self):
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "config.yaml")
            config = AppConfig(config_path=path)
            runs = []
            worker = Thread(target=run_scheduler, args=(config, lambda: config, lambda _mode: runs.append(1)),
                            kwargs={"wait_first": True}, daemon=True)
            worker.start()
            deadline = time.monotonic() + 5
            try:
                while not scheduler_status(path)["next_run_at"] and time.monotonic() < deadline:
                    time.sleep(.04)
                self.assertEqual("waiting", scheduler_status(path)["state"])
                self.assertFalse(run_scheduler(config, lambda: config, lambda: runs.append(2)))
            finally:
                request_stop(path)
                worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual([], runs)

    def test_actual_stop_command_reports_stopped_without_loading_config(self):
        with TemporaryDirectory() as directory:
            command = [sys.executable, "-B", str(Path(__file__).resolve().parents[1] / "main.py"),
                       "--config", str(Path(directory) / "config.yaml"), "--scheduler-stop"]
            result = subprocess.run(command, capture_output=True, check=True, timeout=10)
            self.assertEqual({"stop_requested": False}, json.loads(result.stdout))

    def test_mode_switch_during_cycle_keeps_the_same_worker(self):
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "config.yaml")
            config = AppConfig(config_path=path)
            entered, release = Event(), Event()
            modes = []

            def execute(collect_only):
                modes.append(collect_only)
                if collect_only:
                    entered.set()
                    release.wait(5)
                else:
                    request_stop(path)
                return {"status": "succeeded"}

            worker = Thread(target=run_scheduler, args=(config, lambda: config, execute),
                            kwargs={"collect_only": True}, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                original = scheduler_status(path)["instance"]
                command = [sys.executable, "-B", str(Path(__file__).resolve().parents[1] / "main.py"),
                           "--config", path, "--scheduler-mode", "full"]
                result = subprocess.run(command, capture_output=True, check=True, timeout=10)
                self.assertTrue(json.loads(result.stdout)["mode_requested"])
                self.assertEqual("full", scheduler_status(path)["requested_mode"])
                self.assertEqual(original, scheduler_status(path)["instance"])
            finally:
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual([True, False], modes)
