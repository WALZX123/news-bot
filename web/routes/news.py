from datetime import datetime, timedelta
from flask import Blueprint, abort, render_template, request
from web.auth import login_required, get_storage
from web.db import pagination, read_cursor

news_bp = Blueprint("news", __name__)


@news_bp.route("/")
@login_required
def list_news():
    page, per_page = pagination()
    q = request.args.get("q", "").strip()
    source_filter = request.args.get("source", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    notified = request.args.get("notified", "").strip()
    try:
        start = datetime.strptime(date_from, '%Y-%m-%d') if date_from else None
        end = datetime.strptime(date_to, '%Y-%m-%d') if date_to else None
        if start and end and start > end:
            raise ValueError('Reversed date range')
        start = start - timedelta(hours=8) if start else None
        end = end + timedelta(days=1, hours=-8) if end else None
    except (ValueError, OverflowError):
        abort(400, '日期必须有效，且开始日期不能晚于结束日期。')
    storage = get_storage()
    with read_cursor(storage) as cursor:

        # Get distinct sources for filter dropdown
        cursor.execute("SELECT DISTINCT source FROM news_items ORDER BY source")
        all_sources = [r["source"] for r in cursor.fetchall()]

        # Build query
        conditions = ["1=1"]
        params = []

        if q:
            conditions.append("(title LIKE %s OR summary LIKE %s)")
            params.extend([f"%{q}%", f"%{q}%"])
        if source_filter:
            conditions.append("source = %s")
            params.append(source_filter)
        if start:
            conditions.append("created_at >= %s")
            params.append(start)
        if end:
            conditions.append("created_at < %s")
            params.append(end)
        if notified in ("yes", "no"):
            conditions.append("notified = %s")
            params.append(1 if notified == "yes" else 0)

        where = " WHERE " + " AND ".join(conditions)

        cursor.execute("SELECT COUNT(*) AS cnt FROM news_items" + where, params)
        total = cursor.fetchone()["cnt"]

        offset = (page - 1) * per_page
        cursor.execute(
            "SELECT id, title, url, source, published_at, notified, created_at "
            "FROM news_items" + where + " ORDER BY created_at DESC LIMIT %s OFFSET %s",
            params + [per_page, offset],
        )
        items = cursor.fetchall()

    total_pages = max(1, (total + per_page - 1) // per_page)

    return render_template(
        "news.html",
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        all_sources=all_sources,
        q=q,
        source_filter=source_filter,
        date_from=date_from,
        date_to=date_to,
        notified=notified,
    )


@news_bp.route("/<int:id>")
@login_required
def view_news(id):
    storage = get_storage()
    with read_cursor(storage) as cursor:
        cursor.execute(
            "SELECT id, title, url, summary, source, published_at, notified, created_at "
            "FROM news_items WHERE id = %s",
            (id,),
        )
        item = cursor.fetchone()

    if not item:
        from flask import abort
        abort(404)

    return render_template("news_detail.html", item=item)
