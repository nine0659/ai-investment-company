"""
services/position_lifecycle_service.py
포지션 생애주기(EARLY→DEVELOPING→MATURE→EXHAUSTED) 추적 + 손익비(R/R) 검증

Phase B 핵심 구현:
- CIO 결정 로그에서 lifecycle 필드를 portfolio_positions DB에 반영
- R/R < 3:1 포지션 경고
- 현재 포지션 생애주기 현황 (수익률·목표가 진행도 포함)을 팩트 시트에 주입
- 단계 전환 자동 탐지 + DB 갱신
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

_STAGE_LABEL = {
    "early":      "🌱EARLY(탐색·1~3%)",
    "developing": "🌿DEVELOPING(성장·3~7%)",
    "mature":     "🍎MATURE(성숙·유지/축소)",
    "exhausted":  "⚰️EXHAUSTED(소진·청산검토)",
}
_CONV_LABEL = {
    "high":   "확신上",
    "medium": "확신中",
    "low":    "확신下",
}


# ── DB 마이그레이션 ──────────────────────────────────────────────────────

def migrate_portfolio_positions() -> None:
    """portfolio_positions에 생애주기 관련 컬럼이 없으면 추가."""
    new_cols = [
        ("thesis_stage",  "TEXT DEFAULT 'early'"),
        ("thesis",        "TEXT"),
        ("risk_reward",   "TEXT"),
        ("falsification", "TEXT"),
        ("conviction",    "TEXT DEFAULT 'medium'"),
    ]
    try:
        from sqlalchemy import inspect as sa_inspect
        from db.database import engine
        inspector = sa_inspect(engine)
        existing = [c["name"] for c in inspector.get_columns("portfolio_positions")]
        with engine.begin() as conn:
            for col_name, col_def in new_cols:
                if col_name not in existing:
                    conn.execute(text(
                        f"ALTER TABLE portfolio_positions ADD COLUMN {col_name} {col_def}"
                    ))
                    logger.info("[생애주기] portfolio_positions.%s 컬럼 추가", col_name)
    except Exception as e:
        logger.warning("[생애주기] 마이그레이션 실패: %s", e)


# ── CIO 결정 → DB 반영 ──────────────────────────────────────────────────

def update_from_cio_decisions(date: str, decisions: dict) -> None:
    """CIO 결정 파싱 결과를 portfolio_positions 생애주기 필드에 반영."""
    now_str = datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S")

    try:
        with get_conn() as conn:
            # 신규 포지션 — draft 레코드에 lifecycle 정보 추가
            for pos in decisions.get("new_positions", []):
                code = pos.get("code", "")
                if not code:
                    continue
                conn.execute(
                    text("""
                        UPDATE portfolio_positions
                        SET thesis_stage  = :stage,
                            thesis        = :thesis,
                            risk_reward   = :rr,
                            falsification = :falsify,
                            conviction    = :conviction,
                            updated_at    = :now
                        WHERE code = :code AND entry_date = :date
                    """),
                    {
                        "code":       code,
                        "date":       date,
                        "stage":      pos.get("thesis_stage", "early"),
                        "thesis":     pos.get("thesis", ""),
                        "rr":         pos.get("risk_reward", ""),
                        "falsify":    pos.get("falsification", ""),
                        "conviction": pos.get("conviction", "medium"),
                        "now":        now_str,
                    },
                )

            # 유지 포지션 — 단계·반증신호 갱신
            for pos in decisions.get("position_holds", []):
                code = pos.get("code", "")
                if not code:
                    continue
                conn.execute(
                    text("""
                        UPDATE portfolio_positions
                        SET thesis_stage   = :stage,
                            conviction     = :conviction,
                            falsification  = :falsify,
                            updated_at     = :now
                        WHERE code = :code AND status IN ('holding', 'draft')
                    """),
                    {
                        "code":       code,
                        "stage":      pos.get("thesis_stage", "developing"),
                        "conviction": pos.get("conviction", "medium"),
                        "falsify":    pos.get("falsification", ""),
                        "now":        now_str,
                    },
                )

            # 청산 포지션 — exhausted 마킹
            for pos in decisions.get("position_changes", []):
                if pos.get("action") == "exit":
                    conn.execute(
                        text("""
                            UPDATE portfolio_positions
                            SET thesis_stage = 'exhausted', updated_at = :now
                            WHERE code = :code AND status IN ('holding', 'draft')
                        """),
                        {"code": pos.get("code", ""), "now": now_str},
                    )

        logger.info("[생애주기] CIO 결정 반영 완료: %s (신규 %d건, 유지 %d건)",
                    date,
                    len(decisions.get("new_positions", [])),
                    len(decisions.get("position_holds", [])))
    except Exception as e:
        logger.warning("[생애주기] 반영 실패: %s", e)


# ── 팩트 시트 주입용 현황 조회 (수익률·목표가 진행도 포함) ────────────────

def get_lifecycle_context(prices: dict[str, float] | None = None) -> str:
    """현재 보유 포지션 생애주기 현황 — avg_price·target_price·목표가 진행도 포함.

    Args:
        prices: {code: current_price} 딕셔너리 (없으면 현재가 생략)
    """
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text("""
                    SELECT code, name, thesis_stage, conviction,
                           risk_reward, falsification, thesis,
                           avg_price, target_price, entry_date
                    FROM portfolio_positions
                    WHERE status IN ('holding', 'draft')
                    ORDER BY
                        CASE thesis_stage
                            WHEN 'early'      THEN 1
                            WHEN 'developing' THEN 2
                            WHEN 'mature'     THEN 3
                            WHEN 'exhausted'  THEN 4
                            ELSE 5
                        END
                """)
            ).fetchall()
    except Exception as e:
        logger.warning("[생애주기] 조회 실패: %s", e)
        return ""

    if not rows:
        return ""

    lines = ["[현재 포지션 생애주기 — CIO 비중·단계 판단 참고]"]
    for code, name, stage, conviction, rr, falsify, thesis, avg_price, target_price, entry_date in rows:
        stage_lbl = _STAGE_LABEL.get(str(stage or "").lower(), stage or "미정")
        conv_lbl  = _CONV_LABEL.get(str(conviction or "").lower(), conviction or "")
        rr_lbl    = f" | R:R={rr}" if rr else ""

        # 수익률 + 목표가 진행도
        perf_parts = []
        cur_price = (prices or {}).get(code)
        if avg_price and avg_price > 0:
            if cur_price:
                ret_pct = (cur_price - avg_price) / avg_price * 100
                perf_parts.append(f"수익률 {ret_pct:+.1f}%")
                if target_price and target_price > avg_price:
                    tgt_progress = (cur_price - avg_price) / (target_price - avg_price) * 100
                    perf_parts.append(f"목표가 {tgt_progress:.0f}% 도달")
            else:
                perf_parts.append(f"진입가 {avg_price:,.0f}원")
                if target_price:
                    perf_parts.append(f"목표 {target_price:,.0f}원")

        perf_lbl = f" | {' / '.join(perf_parts)}" if perf_parts else ""

        # 보유기간
        days_held = ""
        if entry_date:
            try:
                from datetime import date as _date
                ed = datetime.strptime(str(entry_date), "%Y-%m-%d").date()
                days_held = f" | {(_date.today() - ed).days}일 보유"
            except Exception:
                pass

        falsify_lbl = f"\n    ⛔반증: {falsify[:60]}" if falsify else ""
        thesis_lbl  = f"\n    📝테제: {thesis[:60]}" if thesis else ""

        lines.append(
            f"  {name}({code}): {stage_lbl} | {conv_lbl}{rr_lbl}{perf_lbl}{days_held}"
            f"{falsify_lbl}{thesis_lbl}"
        )

    return "\n".join(lines)


# ── R/R 검증 ────────────────────────────────────────────────────────────

def check_rr_warnings(decisions: dict) -> list[str]:
    """신규 포지션 중 R/R < 3:1 또는 미명시 항목을 경고 리스트로 반환."""
    warnings: list[str] = []
    for pos in decisions.get("new_positions", []):
        name = pos.get("name") or pos.get("code", "?")
        rr_raw = str(pos.get("risk_reward", "")).strip()
        if not rr_raw:
            warnings.append(
                f"⚠️ R/R 미명시: {name} — 진입 재검토 권고"
            )
            continue
        try:
            rr_num = float(rr_raw.split(":")[0].replace("R", "").strip())
            if rr_num < 3.0:
                warnings.append(
                    f"⚠️ R/R 미달: {name} R/R={rr_raw} < 3:1 → 헌장 위반, 진입 보류 권고"
                )
        except (ValueError, IndexError):
            pass
    return warnings


def get_rr_warning_context(decisions: dict) -> str:
    """R/R 경고를 팩트 시트 주입용 텍스트로 포맷."""
    warns = check_rr_warnings(decisions)
    if not warns:
        return ""
    return "[⚠️ CIO 헌장 경고 — R/R 기준 위반]\n" + "\n".join(f"  {w}" for w in warns)


# ── 단계 자동 전환 평가 + DB 갱신 ──────────────────────────────────────

def evaluate_stage_transitions(
    prices: dict[str, float] | None = None,
    auto_update: bool = False,
) -> list[str]:
    """현재 포지션 단계 전환 탐지.

    Args:
        prices:      {code: current_price} (없으면 KIS API 직접 조회)
        auto_update: True이면 전환 조건 충족 시 DB thesis_stage 자동 갱신

    전환 기준:
        EARLY → DEVELOPING: 수익률 +10% 이상
        DEVELOPING → MATURE: 수익률 +25% 또는 목표가 도달 80%
        MATURE → EXHAUSTED:  수익률 +40% 또는 목표가 도달 95%
    """
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text("""
                    SELECT code, name, avg_price, thesis_stage, target_price
                    FROM portfolio_positions
                    WHERE status IN ('holding', 'draft')
                      AND avg_price > 0
                """)
            ).fetchall()
    except Exception as e:
        logger.warning("[생애주기] 단계전환 평가 실패: %s", e)
        return []

    alerts: list[str] = []
    now_str = datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S")

    for code, name, avg_price, stage, target_price in rows:
        if not avg_price:
            continue

        # 현재가 결정 (전달받은 prices → 없으면 KIS 직접 조회)
        cur_price = (prices or {}).get(code)
        if not cur_price:
            try:
                from clients.kis_client import KISClient
                _kis = KISClient()
                _pd = _kis.get_stock_price(code, market=None)
                cur_price = _pd.get("price")
            except Exception:
                pass

        if not cur_price:
            continue

        ret_pct   = (cur_price - avg_price) / avg_price * 100
        tgt_ratio = (cur_price / target_price * 100) if target_price else 0
        cur_stage = (stage or "early").lower()
        next_stage = None

        if cur_stage == "early" and ret_pct >= 10:
            next_stage = "developing"
            alerts.append(
                f"📈 단계 전환 검토: {name}({code}) EARLY→DEVELOPING "
                f"(수익률 {ret_pct:+.1f}%)"
            )
        elif cur_stage == "developing" and (ret_pct >= 25 or tgt_ratio >= 80):
            next_stage = "mature"
            alerts.append(
                f"🍎 단계 전환 검토: {name}({code}) DEVELOPING→MATURE "
                f"(수익률 {ret_pct:+.1f}%, 목표가 {tgt_ratio:.0f}%)"
            )
        elif cur_stage == "mature" and (ret_pct >= 40 or tgt_ratio >= 95):
            next_stage = "exhausted"
            alerts.append(
                f"⚰️ 청산 검토: {name}({code}) MATURE→EXHAUSTED "
                f"(수익률 {ret_pct:+.1f}%, 목표가 {tgt_ratio:.0f}%)"
            )

        # 자동 DB 갱신
        if auto_update and next_stage:
            try:
                with get_conn() as conn2:
                    conn2.execute(
                        text("""
                            UPDATE portfolio_positions
                            SET thesis_stage = :stage, updated_at = :now
                            WHERE code = :code AND status IN ('holding', 'draft')
                        """),
                        {"stage": next_stage, "code": code, "now": now_str},
                    )
                logger.info("[생애주기] %s(%s) 자동 전환: %s → %s",
                            name, code, cur_stage, next_stage)
            except Exception as _ue:
                logger.warning("[생애주기] 자동 전환 실패: %s", _ue)

    return alerts
