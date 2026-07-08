"""
services/job_ledger.py — 스케줄 잡 실행 대장 + 일일 헬스체크

문제의식: 지금까지 장애의 절반은 '조용한 실패'였다 — 잡이 아예 안 돌거나
(Render 슬립, 크레딧 소진, DB 폴백) 도중에 죽어도 사용자는 브리핑이
안 온 것을 한참 뒤에야 알아챘다. 시스템 스스로 "어제 예정된 잡이
전부 돌았는가"를 매일 아침 대조하고, 문제가 있을 때만 경보한다.

실행 흔적 소스 2가지:
  1) report_claims — 브리핑·주간 잡들이 시작 시 선점하는 기존 테이블 (재사용)
  2) job_runs      — 그 외 잡(NAV·성과추적·발굴·전략)이 직접 기록하는 신규 대장
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

# 요일별(0=월 … 6=일) 반드시 실행 흔적이 있어야 하는 잡.
# 월간 잡(투자관)은 실행일 판정이 복잡해 v1에서는 제외.
# 2026-07-06 축소: 귀인·적중률·전략·발굴·장기분석 일시 중단 → 기대 목록에서 제거,
# 일요일은 통합 추천 1통(weekly_midterm + weekly_us_invest 클레임으로 검증).
_EXPECTED_BY_WEEKDAY: dict[int, list[str]] = {
    0: ["pre_market", "daily_nav", "daily_tracker"],
    1: ["daily_nav", "daily_tracker"],
    2: ["pre_market", "daily_nav", "daily_tracker"],
    3: ["daily_nav", "daily_tracker"],
    4: ["pre_market", "close_market", "daily_nav", "daily_tracker"],
    5: [],
    6: ["weekly_midterm", "weekly_us_invest"],
}

# 잡 이름 → report_claims.run_type 매핑 (선점 가드를 쓰는 잡들).
# 통합 추천 1통은 내부적으로 midterm·us_invest 클레임을 만들므로
# Render(통합) 경로든 GH Actions(개별 백업) 경로든 같은 흔적이 남는다.
_CLAIM_ALIAS = {
    "pre_market":         "pre_market",
    "close_market":       "close_market",
    "weekly_midterm":     "midterm",
    "weekly_us_invest":   "us_invest",
}


def record_job(job_name: str, status: str, detail: str = "") -> None:
    """잡 실행 결과 기록. 기록 실패가 잡 자체를 죽이면 안 되므로 예외를 삼킨다."""
    try:
        today = datetime.now(_KST).strftime("%Y-%m-%d")
        with get_conn() as conn:
            conn.execute(
                text("INSERT INTO job_runs (date, job_name, status, detail) "
                     "VALUES (:d, :j, :s, :dt)"),
                {"d": today, "j": job_name, "s": status, "dt": detail[:300]},
            )
    except Exception as e:
        logger.warning("[잡대장] 기록 실패 (%s/%s): %s", job_name, status, e)


def has_trace_today(job_name: str) -> bool:
    """오늘(KST) 해당 잡의 실행 흔적이 있는가 — GH Actions 백업 실행기의 중복 방지 가드.

    조회 실패 시 False(=백업 실행)를 반환한다: 백업 잡들은 record_job 가드가
    있어 중복 실행이 데이터를 깨뜨리지 않지만, 누락은 하루치 데이터 손실이다.
    (2026-07-08 daily_tracker 누락 사고 — Render 재시작으로 16:20 실행 증발)
    """
    today = datetime.now(_KST).strftime("%Y-%m-%d")
    try:
        with get_conn() as conn:
            return _has_trace(conn, today, job_name)
    except Exception as e:
        logger.warning("[잡대장] 오늘 흔적 조회 실패 (%s): %s", job_name, e)
        return False


def _has_trace(conn, date_str: str, job_name: str) -> bool:
    """해당 날짜에 잡 실행 흔적(job_runs 성공/스킵 또는 report_claims)이 있는가."""
    row = conn.execute(
        text("SELECT 1 FROM job_runs WHERE date=:d AND job_name=:j "
             "AND status IN ('success','skipped') LIMIT 1"),
        {"d": date_str, "j": job_name},
    ).fetchone()
    if row:
        return True
    claim_type = _CLAIM_ALIAS.get(job_name)
    if claim_type:
        row = conn.execute(
            text("SELECT 1 FROM report_claims WHERE date=:d AND run_type=:rt LIMIT 1"),
            {"d": date_str, "rt": claim_type},
        ).fetchone()
        return row is not None
    return False


def get_yesterday_problems() -> list[str]:
    """어제 예정 잡의 누락·실패 목록. 문제 없으면 빈 리스트."""
    yesterday = datetime.now(_KST) - timedelta(days=1)
    date_str = yesterday.strftime("%Y-%m-%d")
    expected = _EXPECTED_BY_WEEKDAY.get(yesterday.weekday(), [])

    problems: list[str] = []
    try:
        with get_conn() as conn:
            for job in expected:
                if not _has_trace(conn, date_str, job):
                    problems.append(f"❌ {job}: 어제({date_str}) 실행 흔적 없음 — 스케줄러 점검 필요")
            rows = conn.execute(
                text("SELECT job_name, detail FROM job_runs "
                     "WHERE date=:d AND status='fail'"),
                {"d": date_str},
            ).fetchall()
            for job_name, detail in rows:
                problems.append(f"⚠️ {job_name}: 실행 실패 — {(detail or '')[:120]}")
    except Exception as e:
        logger.warning("[잡대장] 헬스체크 조회 실패: %s", e)
        problems.append(f"⚠️ 헬스체크 자체가 DB 조회에 실패: {str(e)[:120]}")
    return problems


def run_daily_health_check() -> None:
    """매일 아침 — 어제 예정 잡 누락·실패를 점검하고 문제 있을 때만 경보.

    정상일 때는 침묵한다 (경보 피로 방지). '시스템이 조용하다 = 정상'이
    성립하려면 이 헬스체크 자체가 매일 돌아야 하므로, 헬스체크 실행 기록도 남긴다.
    """
    problems = get_yesterday_problems()
    record_job("daily_health", "success", f"문제 {len(problems)}건")
    if not problems:
        logger.info("[헬스체크] 어제 예정 잡 모두 정상")
        return
    try:
        from clients.telegram_client import send_error_alert
        send_error_alert(
            "[일일 헬스체크] 어제 잡 실행 이상 감지\n" + "\n".join(problems[:10])
        )
        logger.warning("[헬스체크] 문제 %d건 경보 발송", len(problems))
    except Exception as e:
        logger.error("[헬스체크] 경보 발송 실패: %s", e)
