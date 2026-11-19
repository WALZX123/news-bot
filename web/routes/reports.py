import json
from flask import Blueprint, render_template, request
from web.auth import login_required, get_storage
from web.db import pagination, read_cursor

reports_bp = Blueprint("reports", __name__)


@reports_bp.route("/")
@login_required
def list_reports():
    page, per_page = pagination()
    storage = get_storage()
    with read_cursor(storage) as cursor:
        cursor.execute("SELECT COUNT(*) AS cnt FROM reports")
        total = cursor.fetchone()["cnt"]
        cursor.execute(
            "SELECT id, report_type, period_start, period_end, title, news_count, created_at "
            "FROM reports ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (per_page, (page - 1) * per_page)
        )
        items = cursor.fetchall()

    type_labels = {"weekly": "周报", "monthly": "月报", "quarterly": "季报", "yearly": "年报"}

    return render_template("reports.html", items=items, type_labels=type_labels, page=page, per_page=per_page, total=total, total_pages=max(1, (total + per_page - 1) // per_page))


@reports_bp.route("/<int:id>")
@login_required
def view_report(id):
    storage = get_storage()
    with read_cursor(storage) as cursor:
        cursor.execute(
            "SELECT id, report_type, period_start, period_end, title, content, news_count, created_at "
            "FROM reports WHERE id = %s",
            (id,),
        )
        item = cursor.fetchone()

    if not item:
        from flask import abort
        abort(404)

    try:
        content = json.loads(item["content"])
    except (json.JSONDecodeError, TypeError):
        content = {"raw": item["content"][:500] if item["content"] else "无数据"}

    type_labels = {"weekly": "周报", "monthly": "月报", "quarterly": "季报", "yearly": "年报"}
    item["type_label"] = type_labels.get(item["report_type"], item["report_type"])

    return render_template("report_detail.html", item=item, content=content)
