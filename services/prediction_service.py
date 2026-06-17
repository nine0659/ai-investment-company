"""
services/prediction_service.py
CIO 예측 저장·검증 — Phase C 자기점검 루프

장전 브리핑에서 CIO 방향 예측을 저장하고,
장마감 브리핑에서 실제 결과와 비교해 자기점검 데이터를 제공한다.
"""
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")


# ── 예측 저장 ───────────────────────────────────────────────────────────

def save_cio_prediction(
    date: str,
    run_type: str,
    ceo_report: str,
    raw_market_data: dict | None = None,
) -> None:
    """장전/글로벌 브리핑에서 CIO 예측을 market_predictions 테이블에 저장."""
    direction, prob, sector = _parse_prediction_from_report(ceo_report)
    if not direction:
        logger.debug("[예측] 방향 파싱 실패 — 저장 생략")
        return

    # 현재 KOSPI 기준가 (예측 시점)
    baseline_kospi = None
    if raw_market_data:
        kr_data = raw_market_data.get("kospi", {})
        baseline_kospi = kr_data.get("close") or kr_data.get("price")

    try:
        with get_conn() as conn:
            # 당일 같은 run_type이 이미 있으면 업데이트
            existing = conn.execute(
                text("SELECT id FROM market_predictions WHERE date=:d AND run_type=:r"),
                {"d": date, "r": run_type},
            ).fetchone()

            if existing:
                conn.execute(
                    text("""
                        UPDATE market_predictions
                        SET predicted_dir=:dir, predicted_prob=:prob, sector_pred=:sec
                        WHERE date=:d AND run_type=:r
                    """),
                    {"dir": direction, "prob": prob, "sec": sector,
                     "d": date, "r": run_type},
                )
            else:
                conn.execute(
                    text("""
                        INSERT INTO market_predictions
                        (date, run_type, predicted_dir, predicted_prob, sector_pred)
                        VALUES (:d, :r, :dir, :prob, :sec)
                    """),
                    {"d": date, "r": run_type, "dir": direction,
                     "prob": prob, "sec": sector},
                )
        logger.info("[예측] 저장 완료: %s %s → %s(%.0f%%)",
                    date, run_type, direction, prob or 0)
    except Exception as e:
        logger.warning("[예측] 저장 실패: %s", e)


def update_actual_result(date: str, actual_kospi_pct: float) -> None:
    """장마감 KOSPI 실제 등락률을 market_predictions에 기록하고 적중 여부 판단."""
    actual_dir = "상승" if actual_kospi_pct > 0.1 else ("하락" if actual_kospi_pct < -0.1 else "중립")
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text("""
                    SELECT id, predicted_dir FROM market_predictions
                    WHERE date=:d AND actual_kospi IS NULL
                """),
                {"d": date},
            ).fetchall()

            for row_id, pred_dir in rows:
                correct = 1 if pred_dir == actual_dir else 0
                conn.execute(
                    text("""
                        UPDATE market_predictions
                        SET actual_kospi=:pct, actual_dir=:dir, correct=:c
                        WHERE id=:id
                    """),
                    {"pct": actual_kospi_pct, "dir": actual_dir,
                     "c": correct, "id": row_id},
                )
        logger.info("[예측] 실제 결과 기록: %s KOSPI %+.2f%% (%s)",
                    date, actual_kospi_pct, actual_dir)
    except Exception as e:
        logger.warning("[예측] 결과 기록 실패: %s", e)


# ── 자기점검 컨텍스트 생성 ──────────────────────────────────────────────

def get_selfcheck_context(date: str) -> str:
    """장마감 브리핑용 자기점검 컨텍스트 — 오늘 장전 예측을 정리해 반환."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                text("""
                    SELECT predicted_dir, predicted_prob, sector_pred
                    FROM market_predictions
                    WHERE date=:d AND run_type='pre_market'
                    ORDER BY id DESC LIMIT 1
                """),
                {"d": date},
            ).fetchone()
    except Exception as e:
        logger.warning("[예측] 자기점검 조회 실패: %s", e)
        return ""

    if not row:
        return ""

    pred_dir, pred_prob, sector_pred = row
    prob_str = f" ({pred_prob:.0f}%)" if pred_prob else ""
    sector_str = f" | 예측 주도 섹터: {sector_pred}" if sector_pred else ""
    return (
        f"[오늘 장전 CIO 예측 — 🔍 자기점검 기준]\n"
        f"  방향 예측: {pred_dir}{prob_str}{sector_str}\n"
        f"  ※ 실제 KOSPI 결과와 비교해 적중·불일치·오판 원인을 반드시 명시할 것"
    )


def get_accuracy_summary(days: int = 20) -> str:
    """최근 N일 예측 적중률 요약 텍스트."""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text("""
                    SELECT predicted_dir, actual_dir, correct, actual_kospi
                    FROM market_predictions
                    WHERE correct IS NOT NULL
                    ORDER BY date DESC
                    LIMIT :n
                """),
                {"n": days},
            ).fetchall()
    except Exception as e:
        logger.warning("[예측] 적중률 조회 실패: %s", e)
        return ""

    if len(rows) < 3:
        return ""

    total = len(rows)
    hits  = sum(1 for r in rows if r[2] == 1)
    rate  = hits / total * 100
    return (
        f"[CIO 예측 적중률 — 최근 {total}회]\n"
        f"  적중: {hits}/{total}회 ({rate:.0f}%) "
        f"{'✅ 양호' if rate >= 60 else '⚠️ 검토 필요'}"
    )


# ── 내부 파싱 유틸 ───────────────────────────────────────────────────────

def _parse_prediction_from_report(report: str) -> tuple[str, float | None, str]:
    """CIO 브리핑 텍스트에서 방향·확률·섹터 예측을 추출.

    Returns: (direction, probability, sector_pred)
    """
    direction  = ""
    prob: float | None = None
    sector_pred = ""

    # 방향 키워드 탐색
    if re.search(r"갭업|상승|우호|강세", report):
        direction = "상승"
    elif re.search(r"갭다운|하락|불리|약세", report):
        direction = "하락"
    elif re.search(r"보합|중립|박스권", report):
        direction = "중립"

    # 확률 파싱: "상승 65%", "기본 55%", "강세 35%" 등
    m = re.search(r"(기본|강세|상승|약세|하락)[^\d]*([\d]{2,3})%", report)
    if m:
        try:
            raw_prob = float(m.group(2))
            # "강세 35%"처럼 하락 확률이 높을 때 방향 보정
            if m.group(1) in ("약세", "하락") and raw_prob > 50:
                direction = "하락"
            prob = raw_prob
        except ValueError:
            pass

    # 섹터 파싱: "반도체·AI" 등
    m2 = re.search(r"주도[:\s]*([가-힣·,\s]{2,20})", report)
    if m2:
        sector_pred = m2.group(1).strip()[:30]

    return direction, prob, sector_pred
