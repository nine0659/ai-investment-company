import json
import logging

from db.database import get_conn
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

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


def claim_report_slot(date: str, run_type: str) -> bool:
    """오늘 해당 run_type 슬롯을 원자적으로 선점.

    스케줄러 cron · 웹 대시보드 수동실행 · 재시작 복구 스레드 등 서로 다른
    경로가 같은 브리핑을 동시에 시작하려 할 때, DB UNIQUE 제약(report_claims)으로
    단 하나만 통과시킨다. 파이프라인 시작 직전(무거운 데이터수집·LLM 호출 전)에
    호출해야 한다 — already_ran_today()는 리포트 저장 시점(파이프라인 끝)에야
    참이 되므로 그 사이의 몇 분 동안 중복 트리거를 막지 못했다.

    Returns:
        True  — 선점 성공, 이 호출자가 파이프라인을 진행해야 함
        False — 이미 다른 트리거가 선점함, 즉시 중단해야 함
    """
    try:
        with get_conn() as conn:
            conn.execute(
                text("INSERT INTO report_claims (date, run_type) VALUES (:date, :run_type)"),
                {"date": date, "run_type": run_type},
            )
        return True
    except IntegrityError:
        return False
    except Exception as e:
        logger.warning("[중복방지] claim 시도 실패 — 안전하게 통과 처리: %s", e)
        return True  # DB 오류로 판단 불가할 땐 발송을 막지 않음 (fail-open)


def release_report_slot(date: str, run_type: str) -> None:
    """파이프라인이 실패로 끝났을 때 선점 해제 — 같은 날 재시도가 가능하도록."""
    try:
        with get_conn() as conn:
            conn.execute(
                text("DELETE FROM report_claims WHERE date=:date AND run_type=:run_type"),
                {"date": date, "run_type": run_type},
            )
    except Exception as e:
        logger.warning("[중복방지] claim 해제 실패 (무시): %s", e)


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


def save_deep_report(date: str, run_type: str, content: str) -> None:
    """메인 브리핑에서 압축돼 잘려나간 분석 원문(글로벌 서사·전문가시각·종목 기술/수급)을
    보존한다. /insight 명령어·대시보드에서 조회용."""
    if not content:
        return
    try:
        with get_conn() as conn:
            conn.execute(
                text(
                    "INSERT INTO deep_reports (date, run_type, content) "
                    "VALUES (:date, :run_type, :content)"
                ),
                {"date": date, "run_type": run_type, "content": content},
            )
        logger.info("[심층리포트] 저장 완료: %s %s (%d자)", date, run_type, len(content))
    except Exception as e:
        logger.warning("[심층리포트] 저장 실패 (무시): %s", e)


def get_latest_deep_report() -> dict | None:
    """가장 최근에 저장된 심층 리포트 조회."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                text(
                    "SELECT date, run_type, content, created_at FROM deep_reports "
                    "ORDER BY id DESC LIMIT 1"
                ),
            ).fetchone()
        if not row:
            return None
        return {"date": row[0], "run_type": row[1], "content": row[2], "created_at": row[3]}
    except Exception as e:
        logger.warning("[심층리포트] 조회 실패: %s", e)
        return None


def format_report_for_db(state: dict) -> dict:
    return {
        "date":             state.get("date", ""),
        "run_type":         state.get("run_type", ""),
        "ceo_report":       state.get("ceo_report", ""),
        "candidates":       state.get("candidates", []),
        "sector_scores":    state.get("sector_scores", []),
        "market_direction": state.get("market_direction", ""),
    }
