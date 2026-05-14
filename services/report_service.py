import json
import logging
import os
import sqlite3

logger = logging.getLogger(__name__)

_DB = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data", "database.sqlite3"))


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB), exist_ok=True)
    return sqlite3.connect(_DB)


def save_report(date: str, run_type: str, ceo_report: str,
                candidates: list, sector_scores: list, market_direction: str):
    with _conn() as c:
        c.execute(
            "INSERT INTO reports (date, run_type, ceo_report, candidates, sector_scores, market_direction) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (date, run_type, ceo_report,
             json.dumps(candidates, ensure_ascii=False),
             json.dumps(sector_scores, ensure_ascii=False),
             market_direction),
        )
    logger.info("리포트 저장 완료: %s %s", date, run_type)


def already_ran_today(date: str, run_type: str) -> bool:
    """오늘 날짜에 해당 run_type 리포트가 이미 DB에 저장됐으면 True (중복 실행 방지)."""
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT 1 FROM reports WHERE date=? AND run_type=?",
                (date, run_type),
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
