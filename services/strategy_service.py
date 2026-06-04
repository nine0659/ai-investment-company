"""
services/strategy_service.py
주간·장기 전략 리포트 저장 및 조회 서비스

저장:  save_strategy_report(date, report_type, report, ceo_summary)
조회:  get_latest_strategy_summary(max_days) → 최신 전략 요약 문자열
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")


def save_strategy_report(
    date: str,
    report: str,
    ceo_summary: str,
    report_type: str = "weekly",
) -> None:
    """전략 리포트를 DB에 저장. 같은 날 같은 유형이 이미 있으면 덮어씀."""
    try:
        with get_conn() as conn:
            conn.execute(
                text("""
                    INSERT INTO strategy_reports (date, report_type, report, ceo_summary)
                    VALUES (:date, :rt, :report, :summary)
                """),
                {"date": date, "rt": report_type, "report": report, "summary": ceo_summary},
            )
        logger.info("[전략서비스] %s %s 저장 완료", report_type, date)
    except Exception as e:
        logger.warning("[전략서비스] 저장 실패: %s", e)


def get_latest_strategy_summary(max_days: int = 7) -> str:
    """최근 max_days일 내의 가장 최신 주간 전략 요약 반환.

    일일 CEO 브리핑에 주입하기 위한 압축 요약(ceo_summary)을 반환.
    해당 기간 내 전략이 없으면 빈 문자열 반환.
    """
    try:
        cutoff = (datetime.now(_KST) - timedelta(days=max_days)).strftime("%Y-%m-%d")
        with get_conn() as conn:
            row = conn.execute(
                text("""
                    SELECT date, report_type, ceo_summary
                    FROM strategy_reports
                    WHERE date >= :cutoff
                    ORDER BY date DESC, id DESC
                    LIMIT 1
                """),
                {"cutoff": cutoff},
            ).fetchone()
        if row and row[2]:
            date_str, rtype, summary = row
            label = "주간전략" if rtype == "weekly" else "장기전략"
            return f"[{date_str} {label}]\n{summary}"
        return ""
    except Exception as e:
        logger.debug("[전략서비스] 조회 실패: %s", e)
        return ""


def get_latest_strategy_report(report_type: str = "weekly", max_days: int = 10) -> str:
    """전체 전략 리포트 반환 (요약본이 아닌 원문). 웹 대시보드·수동 조회용."""
    try:
        cutoff = (datetime.now(_KST) - timedelta(days=max_days)).strftime("%Y-%m-%d")
        with get_conn() as conn:
            row = conn.execute(
                text("""
                    SELECT report FROM strategy_reports
                    WHERE report_type = :rt AND date >= :cutoff
                    ORDER BY date DESC, id DESC
                    LIMIT 1
                """),
                {"rt": report_type, "cutoff": cutoff},
            ).fetchone()
        return row[0] if row else ""
    except Exception as e:
        logger.debug("[전략서비스] 리포트 조회 실패: %s", e)
        return ""
