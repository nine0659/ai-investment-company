"""
services/market_prediction_service.py
시장 방향 예측 정확도 추적 서비스

- CEO 브리핑 리포트에서 방향 예측 파싱 (상승/하락/중립 + 확률)
- 실제 KOSPI 등락률과 대조하여 적중 여부 판정
- 정확도 통계 및 트렌드 분석
"""
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

# ── 방향 파싱 패턴 ─────────────────────────────────────────────────

# "상승 75%" / "75% 상승" / "상승(75%)" 등 패턴
_DIR_PROB_RE = re.compile(
    r"(상승|하락|중립)[^\d]{0,10}(\d{2,3})\s*%"
    r"|(\d{2,3})\s*%[^\d]{0,10}(상승|하락|중립)",
    re.IGNORECASE,
)

# 방향 키워드만 추출 (확률 없을 때 폴백)
_DIR_KEYWORD_RE = re.compile(
    r"(강력?\s*상승|단기\s*상승|상승\s*전환|상승\s*우위|상승|"
    r"강력?\s*하락|단기\s*하락|하락\s*전환|하락\s*우위|하락|"
    r"중립|혼조|횡보|불확실)",
    re.IGNORECASE,
)

# 섹터 예측 키워드
_SECTOR_RE = re.compile(r"주도\s*섹터[：:\s]+([\w가-힣·,\s]{5,60})")


def _parse_direction(report_text: str) -> tuple[str | None, float | None]:
    """리포트 텍스트에서 방향 예측과 확률 파싱."""
    # 먼저 방향+확률 동시 파싱 시도
    m = _DIR_PROB_RE.search(report_text)
    if m:
        if m.group(1):
            direction = m.group(1)
            prob      = float(m.group(2))
        else:
            prob      = float(m.group(3))
            direction = m.group(4)
        return direction, prob

    # 방향 키워드만 폴백 (확률 없음)
    m2 = _DIR_KEYWORD_RE.search(report_text)
    if m2:
        raw = m2.group(1)
        if "상승" in raw:
            return "상승", None
        if "하락" in raw:
            return "하락", None
        return "중립", None

    return None, None


def _parse_sector(report_text: str) -> str | None:
    """주도 섹터 예측 파싱."""
    m = _SECTOR_RE.search(report_text)
    if m:
        return m.group(1).strip()[:100]
    return None


def _to_direction(change_pct: float, threshold: float = 0.3) -> str:
    """KOSPI 등락률을 방향 문자열로 변환."""
    if change_pct >= threshold:
        return "상승"
    if change_pct <= -threshold:
        return "하락"
    return "중립"


# ── 저장 ───────────────────────────────────────────────────────────

def save_prediction(date: str, run_type: str, report_text: str) -> bool:
    """CEO 리포트에서 예측 파싱 후 market_predictions에 저장."""
    direction, prob    = _parse_direction(report_text)
    sector_pred        = _parse_sector(report_text)

    if not direction:
        logger.info("[Predict] 방향 예측 키워드 없음 (%s %s) — 저장 스킵", date, run_type)
        return False

    try:
        with get_conn() as conn:
            # 같은 날 같은 run_type 은 덮어쓰기
            conn.execute(
                text("DELETE FROM market_predictions WHERE date=:date AND run_type=:rt"),
                {"date": date, "rt": run_type},
            )
            conn.execute(
                text("""
                    INSERT INTO market_predictions
                    (date, run_type, predicted_dir, predicted_prob, sector_pred)
                    VALUES (:date, :rt, :dir, :prob, :sec)
                """),
                {
                    "date": date, "rt": run_type,
                    "dir": direction, "prob": prob, "sec": sector_pred,
                },
            )
        logger.info("[Predict] 저장: %s %s → %s (%.0f%%)",
                    date, run_type, direction, prob or 0)
        return True
    except Exception as e:
        logger.warning("[Predict] 저장 실패: %s", e)
        return False


def verify_predictions(date: str) -> int:
    """특정 날짜의 예측 vs 실제 KOSPI 대조하여 correct 컬럼 업데이트."""
    verified = 0
    try:
        # 실제 KOSPI 등락률 조회 (market_snapshots에서)
        with get_conn() as conn:
            snap = conn.execute(
                text("""
                    SELECT kospi_chg FROM market_snapshots
                    WHERE date=:date AND run_type='close_market'
                    ORDER BY created_at DESC LIMIT 1
                """),
                {"date": date},
            ).fetchone()

        if not snap or snap[0] is None:
            logger.debug("[Predict] %s 실제 KOSPI 데이터 없음", date)
            return 0

        actual_chg = float(snap[0])
        actual_dir = _to_direction(actual_chg)

        # 해당 날짜 미검증 예측 업데이트
        with get_conn() as conn:
            preds = conn.execute(
                text("""
                    SELECT id, predicted_dir FROM market_predictions
                    WHERE date=:date AND correct IS NULL
                """),
                {"date": date},
            ).fetchall()

            for pred_id, pred_dir in preds:
                correct = 1 if pred_dir == actual_dir else 0
                conn.execute(
                    text("""
                        UPDATE market_predictions
                        SET actual_kospi=:chg, actual_dir=:adir, correct=:correct
                        WHERE id=:id
                    """),
                    {
                        "chg": actual_chg, "adir": actual_dir,
                        "correct": correct, "id": pred_id,
                    },
                )
                verified += 1
                logger.info("[Predict] 검증: id=%d 예측=%s 실제=%s(%+.2f%%) → %s",
                            pred_id, pred_dir, actual_dir, actual_chg,
                            "적중" if correct else "실패")

    except Exception as e:
        logger.warning("[Predict] 검증 실패: %s", e)

    return verified


def run_daily_verify() -> int:
    """어제 및 그제 날짜 예측 검증 (장마감 후 실행)."""
    total = 0
    today = datetime.now(_KST)
    for delta in [1, 2]:
        target_date = (today - timedelta(days=delta)).strftime("%Y-%m-%d")
        total += verify_predictions(target_date)
    return total


# ── 통계 조회 ───────────────────────────────────────────────────────

def get_prediction_stats(days: int = 30) -> dict:
    """최근 N일 예측 정확도 통계."""
    empty = {
        "total": 0, "correct": 0, "accuracy": 0.0,
        "up_accuracy": 0.0, "down_accuracy": 0.0, "neutral_accuracy": 0.0,
        "avg_prob_correct": 0.0, "avg_prob_wrong": 0.0,
        "items": [],
    }
    try:
        cutoff = (datetime.now(_KST) - timedelta(days=days)).strftime("%Y-%m-%d")
        with get_conn() as conn:
            rows = conn.execute(
                text("""
                    SELECT date, run_type, predicted_dir, predicted_prob,
                           actual_kospi, actual_dir, correct, sector_pred
                    FROM market_predictions
                    WHERE date >= :cutoff AND correct IS NOT NULL
                    ORDER BY date DESC
                """),
                {"cutoff": cutoff},
            ).fetchall()

        if not rows:
            return empty

        items = [
            {
                "date": r[0], "run_type": r[1], "predicted_dir": r[2],
                "predicted_prob": r[3], "actual_kospi": r[4],
                "actual_dir": r[5], "correct": r[6], "sector_pred": r[7],
            }
            for r in rows
        ]

        total   = len(items)
        correct = sum(1 for i in items if i["correct"] == 1)
        accuracy = round(correct / total * 100, 1) if total else 0.0

        # 방향별 정확도
        def _dir_acc(direction: str) -> float:
            sub = [i for i in items if i["predicted_dir"] == direction]
            if not sub:
                return 0.0
            return round(sum(1 for i in sub if i["correct"] == 1) / len(sub) * 100, 1)

        # 확률 높을 때 vs 낮을 때 성과
        with_prob = [i for i in items if i["predicted_prob"] is not None]
        correct_probs = [i["predicted_prob"] for i in with_prob if i["correct"] == 1]
        wrong_probs   = [i["predicted_prob"] for i in with_prob if i["correct"] == 0]
        avg_prob_correct = round(sum(correct_probs) / len(correct_probs), 1) if correct_probs else 0.0
        avg_prob_wrong   = round(sum(wrong_probs) / len(wrong_probs), 1) if wrong_probs else 0.0

        return {
            "total":            total,
            "correct":          correct,
            "accuracy":         accuracy,
            "up_accuracy":      _dir_acc("상승"),
            "down_accuracy":    _dir_acc("하락"),
            "neutral_accuracy": _dir_acc("중립"),
            "avg_prob_correct": avg_prob_correct,
            "avg_prob_wrong":   avg_prob_wrong,
            "items":            items,
        }
    except Exception as e:
        logger.warning("[Predict] 통계 조회 실패: %s", e)
        return empty


def format_prediction_report(days: int = 30) -> str:
    """예측 정확도 텔레그램 리포트 포맷."""
    stats = get_prediction_stats(days)
    if stats["total"] == 0:
        return f"📡 최근 {days}일 시장 예측 검증 데이터 없음"

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📡 AI 시장 방향 예측 정확도 (최근 {days}일)",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"총 {stats['total']}건 | 적중 {stats['correct']}건 | "
        f"정확도 {stats['accuracy']}%",
        "",
        f"방향별  상승 {stats['up_accuracy']}% | "
        f"하락 {stats['down_accuracy']}% | "
        f"중립 {stats['neutral_accuracy']}%",
        f"고확률 예측 정확도: {stats['avg_prob_correct']}%",
        f"저확률 예측 정확도: {stats['avg_prob_wrong']}%",
        "",
        "[최근 예측 이력]",
    ]

    for item in stats["items"][:10]:
        result = "✅" if item["correct"] == 1 else "❌"
        prob_str = f"{item['predicted_prob']:.0f}%" if item["predicted_prob"] else "?"
        actual = f"{item['actual_kospi']:+.2f}%" if item["actual_kospi"] is not None else "?"
        lines.append(
            f"{result} {item['date']} {item['predicted_dir']}({prob_str}) "
            f"→ 실제 KOSPI {actual}"
        )

    return "\n".join(lines)
