from datetime import datetime, timedelta, timezone
from flask import Blueprint, render_template
from web.auth import login_required, get_storage
from web.db import pagination, read_cursor

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
@login_required
def index():
    today = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).date()
    today_start = datetime.combine(today, datetime.min.time()) - timedelta(hours=8)
    storage = get_storage()
    with read_cursor(storage) as cursor:

        cursor.execute("SELECT COUNT(*) AS cnt FROM news_items")
        total_news = cursor.fetchone()["cnt"]

        cursor.execute("SELECT COUNT(*) AS cnt FROM news_items WHERE created_at >= %s AND created_at < %s",
                       (today_start, today_start + timedelta(days=1)))
        today_news = cursor.fetchone()["cnt"]

        cursor.execute("SELECT COUNT(*) AS cnt FROM news_items WHERE processing_state IN ('pending','batched')")
        unnotified = cursor.fetchone()["cnt"]

        cursor.execute("SELECT COUNT(*) AS cnt FROM sent_logs")
        total_pushes = cursor.fetchone()["cnt"]

        cursor.execute("SELECT MAX(sent_at) AS last_run FROM sent_logs")
        row = cursor.fetchone()
        last_run = row["last_run"] if row else None

        cursor.execute(
            "SELECT source, COUNT(*) AS cnt, MAX(created_at) AS last_seen "
            "FROM news_items WHERE created_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 7 DAY) "
            "GROUP BY source ORDER BY cnt DESC"
        )
        source_stats = cursor.fetchall()

        cursor.execute(
            "SELECT id, sent_at, news_count, sentiment FROM sent_logs "
            "ORDER BY sent_at DESC LIMIT 5"
        )
        recent_pushes = cursor.fetchall()

        # --- Chart data: daily news count (last 7 days) ---
        cursor.execute("""
            SELECT DATE(created_at + INTERVAL 8 HOUR) AS day, COUNT(*) AS cnt
            FROM news_items
            WHERE created_at >= %s AND created_at < %s
            GROUP BY DATE(created_at + INTERVAL 8 HOUR)
            ORDER BY day
        """, (today_start - timedelta(days=6), today_start + timedelta(days=1)))
        daily_rows = cursor.fetchall()
        daily_map = {r["day"]: r["cnt"] for r in daily_rows}
        daily_labels = []
        daily_values = []
        for i in range(6, -1, -1):
            d = today - timedelta(days=i)
            daily_labels.append(d.strftime("%m-%d"))
            daily_values.append(int(daily_map.get(d, 0)) if isinstance(daily_map.get(d, 0), int) else 0)

        # --- Chart data: source distribution (top 10 by total count) ---
        cursor.execute("""
            SELECT source, COUNT(*) AS cnt FROM news_items
            GROUP BY source ORDER BY cnt DESC LIMIT 10
        """)
        source_total_rows = cursor.fetchall()

        # --- Chart data: sentiment timeline (last 10 pushes) ---
        cursor.execute("""
            SELECT sent_at, sentiment FROM sent_logs
            ORDER BY sent_at DESC LIMIT 10
        """)
        sentiment_rows = cursor.fetchall()
        sentiment_rows.reverse()
        sentiment_labels = []
        sentiment_moods = []
        for sr in sentiment_rows:
            sent_at = sr['sent_at']
            if sent_at:
                sent_at = sent_at.replace(tzinfo=timezone.utc) if sent_at.tzinfo is None else sent_at
            sentiment_labels.append(sent_at.astimezone(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M") if sent_at else "—")
            s = sr["sentiment"] or ""
            if "乐观" in s or "积极" in s:
                sentiment_moods.append(2)
            elif "担忧" in s or "悲观" in s or "负面" in s:
                sentiment_moods.append(-1)
            elif "中性" in s or "混合" in s:
                sentiment_moods.append(1)
            else:
                sentiment_moods.append(0)


    source_total = sum(s["cnt"] for s in source_stats)

    return render_template(
        "dashboard.html",
        total_news=total_news,
        today_news=today_news,
        unnotified=unnotified,
        total_pushes=total_pushes,
        last_run=last_run,
        source_stats=source_stats,
        source_total=source_total,
        recent_pushes=recent_pushes,
        daily_labels=daily_labels,
        daily_values=daily_values,
        source_total_rows=source_total_rows,
        sentiment_labels=sentiment_labels,
        sentiment_moods=sentiment_moods,
    )
