"""Record bounded, credential-free outcomes for a local pipeline run."""
from contextlib import contextmanager
from time import monotonic

from mysql.connector import Error as MySQLError

from . import analyzer


def failure_code(error):
    if isinstance(error, analyzer.ModelBudgetError):
        return "model_budget"
    if isinstance(error, analyzer.ModelRequestError):
        return "model_" + error.code
    if isinstance(error, analyzer.ModelResponseError):
        return error.code
    if isinstance(error, MySQLError):
        return "database_error"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, OSError):
        return "system_error"
    return "internal_error"


class RunTrace:
    def __init__(self):
        self.steps = []

    @contextmanager
    def stage(self, name):
        started = monotonic()
        step = {"stage": name, "status": "succeeded"}
        self.steps.append(step)
        try:
            yield step
        except Exception as error:
            step.update(status="failed", code=failure_code(error))
            raise
        finally:
            step["duration_ms"] = round((monotonic() - started) * 1000)
