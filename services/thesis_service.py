"""
services/thesis_service.py
월간 투자 테제(Investment Thesis) 저장 및 조회 서비스

저장:  save_thesis(date, fields...)
조회:  get_active_thesis()         → 현재 활성 테제 전체 row
조회:  get_thesis_ceo_summary()    → CEO 일일 브리핑 주입용 압축 요약
"""
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

# 테제 유효 기간 — 최대 45일 전까지 허용 (월간 실행이 늦어져도 이전 테제 활용)
_THESIS_MAX_AGE_DAYS = 45


def save_thesis(
    date: str,
    cycle_stage: str,
    macro_regime: str,
    outlook_6m: str,
    outlook_12m: str,
    sector_overweight: list,
    sector_underweight: list,
    conviction_ideas: list,
    bull_scenario: str,
    base_scenario: str,
    bear_scenario: str,
    invalidation: str,
    full_report: str,
    ceo_summary: str,
) -> None:
    try:
        with get_conn() as conn:
            conn.execute(
                text("""
                    INSERT INTO investment_thesis
                    (date, cycle_stage, macro_regime, outlook_6m, outlook_12m,
                     sector_overweight, sector_underweight, conviction_ideas,
                     bull_scenario, base_scenario, bear_scenario,
                     invalidation, full_report, ceo_summary)
                    VALUES
                    (:date, :cs, :mr, :o6, :o12, :sow, :suw, :ci,
                     :bull, :base, :bear, :inv, :full, :ceo)
                """),
                {
                    "date": date, "cs": cycle_stage, "mr": macro_regime,
                    "o6": outlook_6m, "o12": outlook_12m,
                    "sow": json.dumps(sector_overweight, ensure_ascii=False),
                    "suw": json.dumps(sector_underweight, ensure_ascii=False),
                    "ci":  json.dumps(conviction_ideas,   ensure_ascii=False),
                    "bull": bull_scenario, "base": base_scenario, "bear": bear_scenario,
                    "inv": invalidation, "full": full_report, "ceo": ceo_summary,
                },
            )
        logger.info("[테제서비스] %s 저장 완료", date)
    except Exception as e:
        logger.warning("[테제서비스] 저장 실패: %s", e)


def get_active_thesis() -> dict | None:
    """현재 활성 투자 테제 반환. 없으면 None."""
    try:
        cutoff = (datetime.now(_KST) - timedelta(days=_THESIS_MAX_AGE_DAYS)).strftime("%Y-%m-%d")
        with get_conn() as conn:
            row = conn.execute(
                text("""
                    SELECT date, cycle_stage, macro_regime, outlook_6m, outlook_12m,
                           sector_overweight, sector_underweight, conviction_ideas,
                           bull_scenario, base_scenario, bear_scenario,
                           invalidation, full_report, ceo_summary
                    FROM investment_thesis
                    WHERE date >= :cutoff
                    ORDER BY date DESC, id DESC
                    LIMIT 1
                """),
                {"cutoff": cutoff},
            ).fetchone()
        if not row:
            return None
        keys = ["date", "cycle_stage", "macro_regime", "outlook_6m", "outlook_12m",
                "sector_overweight", "sector_underweight", "conviction_ideas",
                "bull_scenario", "base_scenario", "bear_scenario",
                "invalidation", "full_report", "ceo_summary"]
        result = dict(zip(keys, row))
        for field in ("sector_overweight", "sector_underweight", "conviction_ideas"):
            try:
                result[field] = json.loads(result[field] or "[]")
            except Exception:
                result[field] = []
        return result
    except Exception as e:
        logger.debug("[테제서비스] 조회 실패: %s", e)
        return None


def get_thesis_ceo_summary() -> str:
    """CEO 일일 브리핑 주입용 투자 테제 압축 요약. 없으면 빈 문자열."""
    thesis = get_active_thesis()
    if not thesis or not thesis.get("ceo_summary"):
        return ""
    date = thesis["date"]
    summary = thesis["ceo_summary"]
    return f"[{date} 투자테제]\n{summary}"
