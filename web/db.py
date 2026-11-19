from contextlib import contextmanager
from flask import abort, request


@contextmanager
def read_cursor(storage):
    conn = storage._get_connection()
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True)
        yield cursor
    finally:
        try:
            if cursor is not None:
                cursor.close()
        finally:
            conn.close()


def pagination():
    try:
        page = int(request.args.get("page", "1"))
        per_page = int(request.args.get("per_page", "20"))
    except (TypeError, ValueError):
        abort(400, "分页参数必须是整数。")
    if not 1 <= page <= 100000 or not 1 <= per_page <= 100:
        abort(400, "page 必须为 1 到 100000，per_page 必须为 1 到 100。")
    return page, per_page
