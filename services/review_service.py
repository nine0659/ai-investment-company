import logging

from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)


def save_review(date: str, content: str):
    with get_conn() as conn:
        conn.execute(
            text("INSERT INTO reviews (date, review_content) VALUES (:date, :content)"),
            {"date": date, "content": content},
        )


def save_midterm_report(date: str, report: str):
    with get_conn() as conn:
        conn.execute(
            text("INSERT INTO midterm_reports (date, report) VALUES (:date, :report)"),
            {"date": date, "report": report},
        )


def save_longterm_report(date: str, report: str):
    with get_conn() as conn:
        conn.execute(
            text("INSERT INTO longterm_reports (date, report) VALUES (:date, :report)"),
            {"date": date, "report": report},
        )


def _get_last_report(table: str, before_date: str) -> dict | None:
    """midterm_reports/longterm_reports에서 특정 날짜 이전의 최신 리포트 조회."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                text(
                    f"SELECT date, report FROM {table} "
                    "WHERE date < :d ORDER BY date DESC LIMIT 1"
                ),
                {"d": before_date},
            ).fetchone()
        if row:
            return {"date": row[0], "report": row[1]}
    except Exception as e:
        logger.warning("직전 리포트 조회 실패 (%s): %s", table, e)
    return None


def get_last_midterm_report(before_date: str) -> dict | None:
    return _get_last_report("midterm_reports", before_date)


def get_last_longterm_report(before_date: str) -> dict | None:
    return _get_last_report("longterm_reports", before_date)


def get_last_close_report() -> dict | None:
    try:
        with get_conn() as conn:
            row = conn.execute(
                text(
                    "SELECT date, ceo_report, market_direction FROM reports "
                    "WHERE run_type='close_market' ORDER BY date DESC LIMIT 1"
                )
            ).fetchone()
        if row:
            return {"date": row[0], "ceo_report": row[1], "market_direction": row[2]}
    except Exception as e:
        logger.warning("이전 리포트 조회 실패: %s", e)
    return None
