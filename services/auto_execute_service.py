"""
services/auto_execute_service.py
AI 추천 자동 실행 서비스

- 5단계 게이트 체크 (장시간·드로다운·노출도·단일종목·중복)
- 자동 매수 실행 (confidence 기반 포지션 사이징)
- 드로다운 방어 실행 (50% 청산 / 전량 청산 + 차단)
- 자동 실행 요약 생성 (CEO 피드백 루프)
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")


def _is_market_hours() -> bool:
    """장시간(09:00~15:20) 확인."""
    try:
        from utils.market_calendar import is_krx_trading_day
        if not is_krx_trading_day():
            return False
    except Exception:
        pass
    now = datetime.now(_KST)
    h, m = now.hour, now.minute
    return (9, 0) <= (h, m) <= (15, 20)


def _is_paused() -> bool:
    """자동 실행 일시 중단 여부 확인."""
    try:
        now_str = datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S")
        with get_conn() as conn:
            row = conn.execute(
                text("SELECT 1 FROM auto_execute_pause WHERE pause_until > :now ORDER BY id DESC LIMIT 1"),
                {"now": now_str},
            ).fetchone()
        return row is not None
    except Exception:
        return False


# ── 5단계 게이트 체크 ─────────────────────────────────────────────

def check_auto_gates(code: str, confidence: str, entry_price: float) -> tuple[bool, str]:
    """5단계 자동 실행 게이트 체크.

    Args:
        code: 종목코드
        confidence: 확신도 ('상'/'중'/'하')
        entry_price: 진입 예정가

    Returns:
        (통과 여부, 차단 사유 또는 빈 문자열)
    """
    # 일시 중단 플래그 체크
    if _is_paused():
        return False, "자동 실행 일시 중단 중"

    # 게이트①: 장시간 확인
    if not _is_market_hours():
        return False, "장 외 시간 (09:00~15:20)"

    from config.settings import AUTO_MAX_DAILY_EXPOSURE, AUTO_SIZE_MAP, get_auto_setting
    # AUTO_EXECUTE_BUY도 DB에서 실시간 확인
    if not get_auto_setting("AUTO_EXECUTE_BUY"):
        return False, "AUTO_EXECUTE_BUY 비활성 (DB 설정)"

    # 게이트②: NAV 드로다운 -10% 이하면 차단
    try:
        from services.nav_service import check_drawdown_defense
        dd = check_drawdown_defense()
        if dd.get("action") in ("half", "all"):
            return False, f"드로다운 방어 모드: {dd.get('message', '')}"
    except Exception as _e:
        logger.debug("[게이트②] 드로다운 체크 실패: %s", _e)

    # 게이트③: 오늘 신규 포지션 합산 AUTO_MAX_DAILY_EXPOSURE 초과 시 차단
    daily_exp = get_daily_exposure()
    if daily_exp >= AUTO_MAX_DAILY_EXPOSURE:
        return False, f"일일 노출 한도 초과: {daily_exp*100:.1f}% >= {AUTO_MAX_DAILY_EXPOSURE*100:.0f}%"

    # 게이트④: 단일 종목 5% 초과 시 차단
    try:
        from services.nav_service import get_latest_nav
        nav = get_latest_nav()
        total_assets = nav.get("total_value", 0) if nav else 0
        if total_assets > 0:
            position_size = AUTO_SIZE_MAP.get(confidence, 0.01)
            expected_amount = total_assets * position_size
            if entry_price > 0:
                expected_pct = expected_amount / total_assets
                if expected_pct > 0.05:
                    return False, f"단일 종목 한도 초과: 예상 {expected_pct*100:.1f}% > 5%"
    except Exception as _e:
        logger.debug("[게이트④] 단일 종목 체크 실패: %s", _e)

    # 게이트⑤: 이미 해당 code 보유 중이면 차단
    try:
        with get_conn() as conn:
            row = conn.execute(
                text("SELECT quantity FROM portfolio_positions WHERE code=:c AND status='holding' AND quantity > 0 LIMIT 1"),
                {"c": code},
            ).fetchone()
        if row:
            return False, f"이미 {code} 보유 중 ({row[0]:,}주) — 추가 매수 차단"
    except Exception as _e:
        logger.debug("[게이트⑤] 보유 체크 실패: %s", _e)

    # 3거래일 연속 손실 시 신규 진입 차단
    consec_loss = get_consecutive_loss_days()
    if consec_loss >= 3:
        return False, f"3거래일 연속 손실({consec_loss}일) — 신규 포지션 차단"

    return True, ""


# ── 자동 매수 실행 ─────────────────────────────────────────────────

def auto_buy_recommendation(rec: dict, total_assets: int) -> dict:
    """AI 추천 종목 자동 매수.

    Args:
        rec: 추천 딕셔너리 (code, name, entry_price, stop_price, target_price, rationale 포함)
        total_assets: 총 자산 (원)

    Returns:
        실행 결과 딕셔너리 (success, code, name, qty, price, reason 등)
    """
    from config.settings import AUTO_SIZE_MAP

    code        = rec.get("code", "").strip().zfill(6) if rec.get("code") else ""
    name        = rec.get("name", code)
    entry_price = int(rec.get("entry_price") or 0)
    stop_price  = rec.get("stop_price", 0)
    target_price = rec.get("target_price", 0)
    rationale   = rec.get("rationale", "")

    # confidence 파싱 (rationale 또는 별도 필드에서)
    confidence = rec.get("confidence", "하")
    if not confidence:
        import re
        m = re.search(r'확신\s*[:\s]*(상|중|하)', rationale)
        confidence = m.group(1) if m else "하"

    if not code:
        return {"success": False, "code": "", "name": name, "reason": "종목코드 없음"}

    gate_ok, gate_reason = check_auto_gates(code, confidence, entry_price)
    if not gate_ok:
        logger.info("[자동매수] 차단 (%s): %s", code, gate_reason)
        return {"success": False, "code": code, "name": name, "reason": gate_reason}

    # 포지션 사이징
    position_pct = AUTO_SIZE_MAP.get(confidence, 0.01)
    if total_assets <= 0:
        return {"success": False, "code": code, "name": name, "reason": "총 자산 조회 실패"}

    target_amount = int(total_assets * position_pct)
    if entry_price <= 0:
        return {"success": False, "code": code, "name": name, "reason": "진입가 0 — 현재가 조회 필요"}

    qty = max(1, target_amount // entry_price)

    # rec_id 조회 (stock_recommendations 에서)
    rec_id: int | None = None
    try:
        with get_conn() as conn:
            row_rec = conn.execute(
                text("SELECT id FROM stock_recommendations WHERE code=:c ORDER BY id DESC LIMIT 1"),
                {"c": code},
            ).fetchone()
        rec_id = row_rec[0] if row_rec else None
    except Exception as _e:
        logger.debug("[자동매수] rec_id 조회 실패: %s", _e)

    logger.info("[자동매수] %s(%s) %d주 @%d원 (확신:%s %.0f%%)", name, code, qty, entry_price, confidence, position_pct*100)

    try:
        from services.trading_service import execute_buy
        result = execute_buy(
            code=code, qty=qty, price=entry_price,
            memo=f"AI자동매수 확신:{confidence} {rationale[:100]}",
            timeframe="short",
            rec_id=rec_id,
        )
        result["qty"]  = qty
        result["price"] = entry_price
        return result
    except Exception as e:
        logger.error("[자동매수] 실패 (%s): %s", code, e)
        from clients.telegram_client import send_error_alert
        try:
            send_error_alert(f"자동 매수 오류 ({name}/{code}): {e}")
        except Exception:
            pass
        return {"success": False, "code": code, "name": name, "reason": str(e), "qty": qty, "price": entry_price}


# ── 오늘 노출도 계산 ──────────────────────────────────────────────

def get_daily_exposure() -> float:
    """오늘 신규 매수 금액 합산 / 총 자산."""
    try:
        today = datetime.now(_KST).strftime("%Y-%m-%d")
        with get_conn() as conn:
            row = conn.execute(
                text("""
                    SELECT COALESCE(SUM(amount), 0)
                    FROM order_history
                    WHERE side='buy' AND success=1 AND created_at >= :today
                """),
                {"today": today + " 00:00:00"},
            ).fetchone()
        total_bought = row[0] if row else 0

        from services.nav_service import get_latest_nav
        nav = get_latest_nav()
        total_assets = nav.get("total_value", 1) if nav else 1
        if total_assets <= 0:
            return 0.0
        return float(total_bought) / float(total_assets)
    except Exception as _e:
        logger.debug("[노출도] 계산 실패: %s", _e)
        return 0.0


# ── 연속 손실 거래일 수 ───────────────────────────────────────────

def get_consecutive_loss_days() -> int:
    """최근 연속 손실 거래일 수 (portfolio_history 기준)."""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text("""
                    SELECT exit_date, return_pct FROM portfolio_history
                    WHERE exit_date IS NOT NULL
                    ORDER BY exit_date DESC LIMIT 30
                """)
            ).fetchall()

        if not rows:
            return 0

        # 날짜별 손익 집계
        daily_pnl: dict[str, float] = {}
        for exit_date, ret in rows:
            if exit_date:
                daily_pnl[exit_date] = daily_pnl.get(exit_date, 0) + (ret or 0)

        sorted_dates = sorted(daily_pnl.keys(), reverse=True)
        count = 0
        for d in sorted_dates:
            if daily_pnl[d] < 0:
                count += 1
            else:
                break
        return count
    except Exception as _e:
        logger.debug("[연속손실] 계산 실패: %s", _e)
        return 0


# ── 자동 실행 일시 중단 ───────────────────────────────────────────

def pause_auto_execute(reason: str, days: int = 1) -> None:
    """자동 실행을 N일간 일시 차단 (DB 플래그 저장)."""
    pause_until = (datetime.now(_KST) + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_conn() as conn:
            conn.execute(
                text("INSERT INTO auto_execute_pause (reason, pause_until) VALUES (:r, :p)"),
                {"r": reason, "p": pause_until},
            )
        logger.info("[자동실행] %d일 일시 중단 등록: %s (until %s)", days, reason, pause_until)
        import config.settings as _s
        _s.AUTO_EXECUTE_BUY = False
    except Exception as _e:
        logger.warning("[자동실행] 일시중단 등록 실패: %s", _e)


# ── 드로다운 방어 실행 (P4-3) ─────────────────────────────────────

def execute_drawdown_defense(action: str, kis=None) -> dict:
    """드로다운 임계 초과 시 포지션 청산.

    Args:
        action: "half" (50% 청산) 또는 "all" (전량 청산 + 7일 차단)
        kis: KISClient 인스턴스 (None이면 새로 생성)

    Returns:
        실행 결과 딕셔너리
    """
    if action not in ("half", "all"):
        return {"success": False, "reason": f"알 수 없는 action: {action}"}

    logger.warning("[드로다운방어] %s 실행 시작", action)

    try:
        if kis is None:
            from clients.kis_client import KISClient
            kis = KISClient()
        holdings = kis.get_holdings()
    except Exception as e:
        logger.error("[드로다운방어] KIS 연결 실패: %s", e)
        return {"success": False, "reason": f"KIS 연결 실패: {e}"}

    from services.trading_service import execute_sell
    from clients.telegram_client import send_message

    results = []
    for h in holdings:
        code  = h.get("code", "")
        name  = h.get("name", code)
        owned = h.get("qty", 0)
        if not code or owned <= 0:
            continue
        try:
            sell_qty = owned // 2 if action == "half" else owned
            if sell_qty <= 0:
                continue
            r = execute_sell(code=code, qty=sell_qty, price=0, memo=f"드로다운방어_{action}")
            results.append({"code": code, "name": name, "qty": sell_qty, "success": r.get("success", False)})
        except Exception as e:
            logger.error("[드로다운방어] %s(%s) 매도 실패: %s", name, code, e)
            results.append({"code": code, "name": name, "qty": 0, "success": False, "error": str(e)})

    # 전량 청산이면 7일 AUTO_EXECUTE_BUY 차단
    if action == "all":
        pause_auto_execute("드로다운 -15% 전량청산 자동 차단", days=7)

    success_count = sum(1 for r in results if r.get("success"))
    total_count   = len(results)

    summary_lines = [f"🚨 *드로다운 방어 실행 완료* ({action.upper()})\n"]
    for r in results:
        icon = "✅" if r.get("success") else "❌"
        summary_lines.append(f"  {icon} {r['name']}({r['code']}) {r.get('qty', 0):,}주")
    summary_lines.append(f"\n총 {total_count}개 종목, {success_count}개 성공")
    if action == "all":
        summary_lines.append("⏸️ AUTO_EXECUTE_BUY 7일 차단 적용")

    try:
        send_message("\n".join(summary_lines))
    except Exception:
        pass

    return {"success": True, "action": action, "total": total_count, "success_count": success_count, "results": results}


# ── 자동 실행 요약 (P5-2) ─────────────────────────────────────────

def get_auto_execution_summary(days: int = 7) -> str:
    """최근 N일 자동 실행 결과 요약 문자열 반환 (CEO 피드백용).

    Returns:
        요약 텍스트 또는 빈 문자열
    """
    try:
        cutoff = (datetime.now(_KST) - timedelta(days=days)).strftime("%Y-%m-%d")
        with get_conn() as conn:
            rows = conn.execute(
                text("""
                    SELECT code, name, side, qty, price, amount, success, memo, created_at
                    FROM order_history
                    WHERE rec_id IS NOT NULL AND created_at >= :cutoff
                    ORDER BY created_at DESC
                """),
                {"cutoff": cutoff + " 00:00:00"},
            ).fetchall()

        if not rows:
            return ""

        total     = len(rows)
        buys      = [r for r in rows if r[2] == "buy"]
        sells     = [r for r in rows if r[2] == "sell"]
        successes = [r for r in rows if r[6] == 1]

        # 매수 후 매도된 종목의 수익률 계산
        returns_pct: list[float] = []
        buy_map: dict[str, tuple[int, int]] = {}  # code -> (qty, price)
        for r in sorted(rows, key=lambda x: x[8]):  # created_at 순
            code  = r[0]
            side  = r[2]
            qty   = r[3] or 0
            price = r[4] or 0
            if side == "buy" and r[6]:
                buy_map[code] = (qty, price)
            elif side == "sell" and r[6] and code in buy_map:
                buy_qty, buy_price = buy_map[code]
                if buy_price > 0:
                    ret = (price - buy_price) / buy_price * 100
                    returns_pct.append(ret)

        avg_return = sum(returns_pct) / len(returns_pct) if returns_pct else 0.0

        # 자동 손절 건수
        auto_stop_count = sum(1 for r in sells if r[7] and "자동손절" in (r[7] or ""))
        # 게이트 차단 건수는 별도 로그가 없으므로 추정 불가 — 0으로 표시
        gate_blocked = 0  # TODO: 게이트 차단 로그 테이블 추가 시 집계

        lines = [
            f"[자동 실행 요약 ({days}일)]",
            f"  총 실행: {total}건 (매수 {len(buys)} / 매도 {len(sells)})",
            f"  성공률: {len(successes)/total*100:.0f}%",
        ]
        if returns_pct:
            lines.append(f"  평균 수익률: {avg_return:+.2f}% (매도 완료 {len(returns_pct)}건)")
        if auto_stop_count:
            lines.append(f"  자동 손절 실행: {auto_stop_count}건")

        return "\n".join(lines)
    except Exception as e:
        logger.debug("[자동실행요약] 조회 실패: %s", e)
        return ""
