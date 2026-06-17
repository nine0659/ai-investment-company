"""
services/prediction_service.py
CIO 예측 저장·검증 — Phase C 자기점검 루프

장전 브리핑에서 CIO 방향·시나리오 예측을 저장하고,
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

# macro_stance → 시장 방향 매핑
_STANCE_TO_DIR = {
    "aggressive": "상승",
    "neutral":    "중립",
    "defensive":  "하락",
}


# ── DB 마이그레이션 ──────────────────────────────────────────────────────

def migrate_market_predictions() -> None:
    """market_predictions에 시나리오 확률·오판원인 컬럼 추가."""
    new_cols = [
        ("baseline_prob", "FLOAT"),    # 기본 시나리오 확률 (%)
        ("bull_prob",     "FLOAT"),    # 우호 시나리오 확률 (%)
        ("bear_prob",     "FLOAT"),    # 비관 시나리오 확률 (%)
        ("tail_risk",     "TEXT"),     # 꼬리위험 한 줄 요약
        ("miss_reason",   "TEXT"),     # 장마감 자기점검 오판 원인
    ]
    try:
        from sqlalchemy import inspect as sa_inspect
        from db.database import engine
        inspector = sa_inspect(engine)
        existing = [c["name"] for c in inspector.get_columns("market_predictions")]
        with engine.begin() as conn:
            for col_name, col_def in new_cols:
                if col_name not in existing:
                    conn.execute(text(
                        f"ALTER TABLE market_predictions ADD COLUMN {col_name} {col_def}"
                    ))
                    logger.info("[예측] market_predictions.%s 컬럼 추가", col_name)
    except Exception as e:
        logger.warning("[예측] 마이그레이션 실패: %s", e)


# ── 예측 저장 ───────────────────────────────────────────────────────────

def save_cio_prediction(
    date: str,
    run_type: str,
    ceo_report: str,
    raw_market_data: dict | None = None,
    ceo_decisions: dict | None = None,
) -> None:
    """장전/글로벌 브리핑에서 CIO 예측을 market_predictions 테이블에 저장.

    방향 결정 우선순위:
      1) ceo_decisions["macro_stance"] (aggressive→상승 / defensive→하락 / neutral→중립)
      2) 보고서 텍스트 키워드 파싱 (fallback)
    """
    # ── 방향 결정 (decisions 우선) ──
    direction = ""
    if ceo_decisions:
        stance = (ceo_decisions.get("macro_stance") or "").lower()
        direction = _STANCE_TO_DIR.get(stance, "")

    if not direction:
        direction, _, _ = _parse_direction_from_report(ceo_report)

    if not direction:
        logger.debug("[예측] 방향 판단 불가 — 저장 생략")
        return

    # ── 시나리오 확률 파싱 ──
    baseline_prob, bull_prob, bear_prob, tail_risk = _parse_scenarios(ceo_report)

    # ── 주도 섹터 파싱 ──
    sector_pred = _parse_sector(ceo_report)

    # ── 주요 예측 확률 (기본 시나리오 기준) ──
    predicted_prob = baseline_prob

    try:
        with get_conn() as conn:
            existing = conn.execute(
                text("SELECT id FROM market_predictions WHERE date=:d AND run_type=:r"),
                {"d": date, "r": run_type},
            ).fetchone()

            params = {
                "dir":      direction,
                "prob":     predicted_prob,
                "sec":      sector_pred,
                "base":     baseline_prob,
                "bull":     bull_prob,
                "bear":     bear_prob,
                "tail":     tail_risk,
                "d":        date,
                "r":        run_type,
            }

            if existing:
                conn.execute(
                    text("""
                        UPDATE market_predictions
                        SET predicted_dir=:dir, predicted_prob=:prob, sector_pred=:sec,
                            baseline_prob=:base, bull_prob=:bull, bear_prob=:bear,
                            tail_risk=:tail
                        WHERE date=:d AND run_type=:r
                    """),
                    params,
                )
            else:
                conn.execute(
                    text("""
                        INSERT INTO market_predictions
                        (date, run_type, predicted_dir, predicted_prob, sector_pred,
                         baseline_prob, bull_prob, bear_prob, tail_risk)
                        VALUES (:d, :r, :dir, :prob, :sec, :base, :bull, :bear, :tail)
                    """),
                    params,
                )

        logger.info("[예측] 저장: %s %s → %s (기본%s%%, 우호%s%%, 비관%s%%)",
                    date, run_type, direction,
                    f"{baseline_prob:.0f}" if baseline_prob else "?",
                    f"{bull_prob:.0f}" if bull_prob else "?",
                    f"{bear_prob:.0f}" if bear_prob else "?")
    except Exception as e:
        logger.warning("[예측] 저장 실패: %s", e)


def update_actual_result(date: str, actual_kospi_pct: float,
                         miss_reason: str = "") -> None:
    """장마감 KOSPI 실제 등락률을 market_predictions에 기록하고 적중 여부 판단."""
    actual_dir = ("상승" if actual_kospi_pct > 0.1
                  else ("하락" if actual_kospi_pct < -0.1 else "중립"))
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
                        SET actual_kospi=:pct, actual_dir=:dir, correct=:c,
                            miss_reason=:reason
                        WHERE id=:id
                    """),
                    {"pct": actual_kospi_pct, "dir": actual_dir,
                     "c": correct, "reason": miss_reason, "id": row_id},
                )
        logger.info("[예측] 실제 기록: %s KOSPI %+.2f%% (%s)",
                    date, actual_kospi_pct, actual_dir)
    except Exception as e:
        logger.warning("[예측] 결과 기록 실패: %s", e)


# ── 자기점검 컨텍스트 생성 ──────────────────────────────────────────────

def get_selfcheck_context(date: str) -> str:
    """장마감 브리핑용 자기점검 컨텍스트 — 오늘 장전 예측 구조화."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                text("""
                    SELECT predicted_dir, predicted_prob,
                           baseline_prob, bull_prob, bear_prob,
                           tail_risk, sector_pred
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

    pred_dir, pred_prob, base_prob, bull_prob, bear_prob, tail, sector = row

    lines = ["[오늘 장전 CIO 예측 — 자기점검 기준]"]
    lines.append(f"  방향 예측: {pred_dir or '미기록'}")

    # 시나리오 확률 (저장된 경우)
    sc_parts = []
    if base_prob: sc_parts.append(f"기본 {base_prob:.0f}%")
    if bull_prob: sc_parts.append(f"우호 {bull_prob:.0f}%")
    if bear_prob: sc_parts.append(f"비관 {bear_prob:.0f}%")
    if sc_parts:
        lines.append(f"  시나리오 확률: {' | '.join(sc_parts)}")

    if sector:
        lines.append(f"  예측 주도 섹터: {sector}")
    if tail:
        lines.append(f"  꼬리위험 경보: {tail}")

    lines.append("  ※ 실제 KOSPI 결과·주도 섹터와 비교 — 적중·불일치·오판 원인 반드시 명시")
    return "\n".join(lines)


def get_accuracy_summary(days: int = 20) -> str:
    """최근 N일 예측 적중률 — 방향별 정확도 + 연속 스트릭 포함."""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text("""
                    SELECT date, predicted_dir, actual_dir, correct, actual_kospi
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
    hits  = sum(1 for r in rows if r[3] == 1)
    rate  = hits / total * 100

    # 방향별 정확도
    dir_stats: dict[str, list[int]] = {}
    for _, pred_dir, _, correct, _ in rows:
        if pred_dir:
            dir_stats.setdefault(pred_dir, []).append(correct or 0)

    dir_lines = []
    for d, cs in dir_stats.items():
        dr = sum(cs) / len(cs) * 100
        dir_lines.append(f"{d} {dr:.0f}%({len(cs)}회)")

    # 연속 스트릭
    streak = 0
    streak_type = ""
    for r in rows:
        c = r[3]
        if streak == 0:
            streak = 1
            streak_type = "적중" if c == 1 else "실패"
        elif (c == 1 and streak_type == "적중") or (c == 0 and streak_type == "실패"):
            streak += 1
        else:
            break

    streak_str = f" | 연속 {streak_type} {streak}회" if streak >= 2 else ""

    quality = "✅ 양호" if rate >= 60 else ("⚠️ 개선 필요" if rate >= 45 else "🔴 검토 필수")
    dir_summary = " / ".join(dir_lines) if dir_lines else ""

    return (
        f"[CIO 예측 적중률 — 최근 {total}회]\n"
        f"  전체: {hits}/{total}회 ({rate:.0f}%) {quality}{streak_str}\n"
        + (f"  방향별: {dir_summary}" if dir_summary else "")
    )


# ── 내부 파싱 유틸 ───────────────────────────────────────────────────────

def _parse_direction_from_report(report: str) -> tuple[str, float | None, str]:
    """CIO 브리핑에서 방향·확률·섹터 추출 (fallback — decisions 없을 때)."""
    direction = ""
    prob: float | None = None
    sector_pred = ""

    # 🧭 섹션 우선 탐색 (오늘 예상 라인)
    nav_match = re.search(
        r"오늘 예상[:\s]*(갭업|갭다운|보합)|내일 KOSPI[:\s]*(우호|중립|불리)",
        report
    )
    if nav_match:
        kw = nav_match.group(1) or nav_match.group(2) or ""
        if kw in ("갭업", "우호"):   direction = "상승"
        elif kw in ("갭다운", "불리"): direction = "하락"
        else:                          direction = "중립"

    # fallback: 키워드 탐색 (🧭 섹션 없을 때)
    if not direction:
        up_score   = len(re.findall(r"갭업|우호|RISK-ON", report))
        down_score = len(re.findall(r"갭다운|불리|RISK-OFF", report))
        flat_score = len(re.findall(r"보합|박스권|NEUTRAL", report))
        mx = max(up_score, down_score, flat_score)
        if mx > 0:
            if up_score == mx:   direction = "상승"
            elif down_score == mx: direction = "하락"
            else:                  direction = "중립"

    # 섹터
    m2 = re.search(r"주도[:\s]*([가-힣·,\s]{2,20})", report)
    if m2:
        sector_pred = m2.group(1).strip()[:30]

    return direction, prob, sector_pred


def _parse_scenarios(report: str) -> tuple[float | None, float | None, float | None, str]:
    """시나리오 확률 블록에서 기본/우호/비관 확률과 꼬리위험 파싱.

    Returns: (baseline_prob, bull_prob, bear_prob, tail_risk)
    """
    baseline = bull = bear = None
    tail = ""

    # 📈 기본 [X%] 또는 기본 X%
    m = re.search(r"기본\s*[\[（(]?(\d{1,3})%[\]）)]?", report)
    if m:
        try: baseline = float(m.group(1))
        except ValueError: pass

    # 🌟 우호 [Y%]
    m = re.search(r"우호\s*[\[（(]?(\d{1,3})%[\]）)]?", report)
    if m:
        try: bull = float(m.group(1))
        except ValueError: pass

    # 🔻 비관 [Z%]
    m = re.search(r"비관\s*[\[（(]?(\d{1,3})%[\]）)]?", report)
    if m:
        try: bear = float(m.group(1))
        except ValueError: pass

    # ☠️ 꼬리위험 — 한 줄 추출
    m = re.search(r"꼬리위험[:\s：]*([^\n]{5,80})", report)
    if m:
        tail = m.group(1).strip()[:80]

    return baseline, bull, bear, tail


def _parse_sector(report: str) -> str:
    """주도 섹터 파싱."""
    m = re.search(r"주도[:\s：]*([가-힣A-Za-z·,\s]{2,25})", report)
    if m:
        return m.group(1).strip()[:30]
    return ""
