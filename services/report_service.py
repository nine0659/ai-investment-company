import json
import logging

from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)


def save_report(date: str, run_type: str, ceo_report: str,
                candidates: list, sector_scores: list, market_direction: str):
    with get_conn() as conn:
        conn.execute(
            text(
                "INSERT INTO reports "
                "(date, run_type, ceo_report, candidates, sector_scores, market_direction) "
                "VALUES (:date, :run_type, :ceo_report, :candidates, :sector_scores, :market_direction)"
            ),
            {
                "date":             date,
                "run_type":         run_type,
                "ceo_report":       ceo_report,
                "candidates":       json.dumps(candidates, ensure_ascii=False),
                "sector_scores":    json.dumps(sector_scores, ensure_ascii=False),
                "market_direction": market_direction,
            },
        )
    logger.info("리포트 저장 완료: %s %s", date, run_type)


def already_ran_today(date: str, run_type: str) -> bool:
    """중복 실행 방지: 오늘 날짜에 해당 run_type 리포트가 이미 저장됐으면 True."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                text("SELECT 1 FROM reports WHERE date=:date AND run_type=:run_type"),
                {"date": date, "run_type": run_type},
            ).fetchone()
        return row is not None
    except Exception:
        return False


def format_report_for_db(state: dict) -> dict:
    return {
        "date":             state.get("date", ""),
        "run_type":         state.get("run_type", ""),
        "ceo_report":       state.get("ceo_report", ""),
        "candidates":       state.get("candidates", []),
        "sector_scores":    state.get("sector_scores", []),
        "market_direction": state.get("market_direction", ""),
    }
