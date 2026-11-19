import json
from flask import Blueprint, render_template, request
from web.auth import login_required, get_storage
from web.db import pagination, read_cursor

history_bp = Blueprint("history", __name__)


@history_bp.route("/")
@login_required
def list_history():
    page, per_page = pagination()
    storage = get_storage()

    with read_cursor(storage) as cursor:
        cursor.execute("SELECT COUNT(*) AS cnt FROM sent_logs")
        total = cursor.fetchone()["cnt"]

        offset = (page - 1) * per_page
        cursor.execute(
            "SELECT id, sent_at, news_count, sentiment, LEFT(analysis_text, 300) AS preview "
            "FROM sent_logs ORDER BY sent_at DESC LIMIT %s OFFSET %s",
            (per_page, offset),
        )
        items = cursor.fetchall()

    total_pages = max(1, (total + per_page - 1) // per_page)

    return render_template(
        "history.html",
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )


@history_bp.route("/<int:id>")
@login_required
def view_history(id):
    storage = get_storage()
    with read_cursor(storage) as cursor:
        cursor.execute(
            "SELECT id, sent_at, news_count, analysis_text, sentiment FROM sent_logs WHERE id = %s",
            (id,),
        )
        item = cursor.fetchone()

    if not item:
        from flask import abort
        abort(404)

    try:
        analysis = json.loads(item["analysis_text"])
    except (json.JSONDecodeError, TypeError):
        analysis = {"raw": item["analysis_text"][:500] if item["analysis_text"] else "无分析数据"}

    return render_template("history_detail.html", item=item, analysis=analysis)
