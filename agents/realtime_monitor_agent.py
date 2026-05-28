"""
agents/realtime_monitor_agent.py
실시간 모니터링 에이전트 — 장중 15분마다 실행

기능:
  1. 관심종목 진입 신호 탐지 (RSI 과매도, 목표진입가 근접)
  2. 보유 포지션 손절가·목표가 도달 즉시 알림
  3. 하루 1회 중복 알림 방지 (price_alert_log 활용)
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from clients.kis_client import KISClient
from clients.market_data_client import fetch_kr_stock_technicals
from clients.telegram_client import send_message
from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")


def _is_market_hours() -> bool:
    now = datetime.now(_KST)
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute
    return (9, 0) <= (h, m) <= (15, 30)


def _already_alerted(today: str, code: str, alert_type: str) -> bool:
    try:
        with get_conn() as conn:
            row = conn.execute(
                text("SELECT 1 FROM price_alert_log WHERE date=:d AND code=:c AND type=:t"),
                {"d": today, "c": code, "t": alert_type},
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _mark_alerted(today: str, code: str, alert_type: str) -> None:
    try:
        with get_conn() as conn:
            conn.execute(
                text("""
                    INSERT INTO price_alert_log (date, code, type)
                    VALUES (:d, :c, :t)
                    ON CONFLICT (date, code, type) DO UPDATE SET sent_at=CURRENT_TIMESTAMP
                """),
                {"d": today, "c": code, "t": alert_type},
            )
    except Exception as e:
        logger.debug("알림 기록 실패: %s", e)


def _get_tech(code: str) -> dict:
    """기술적 지표 조회 — KOSPI 우선, 실패 시 KOSDAQ 시도."""
    for sfx in ("KS", "KQ"):
        try:
            tech = fetch_kr_stock_technicals(f"{code}.{sfx}")
            if tech and tech.get("rsi14"):
                return tech
        except Exception:
            pass
    return {}


# ── 관심종목 진입 신호 ────────────────────────────────────────────

def _check_entry_signals(code: str, price: int, target_entry: float | None, tech: dict) -> list[str]:
    """발동된 진입 신호 목록 반환."""
    signals = []

    # 신호 1: 목표진입가 ±1.5% 이내
    if target_entry and target_entry > 0:
        diff_pct = abs(price - target_entry) / target_entry * 100
        if diff_pct <= 1.5:
            direction = "도달" if price <= target_entry else "근접"
            signals.append(f"목표진입가 {direction} ({price:,}원 / 목표 {target_entry:,.0f}원, 차이 {diff_pct:.1f}%)")

    if not tech:
        return signals

    rsi = tech.get("rsi14", 50)
    above_ma20 = tech.get("above_ma20", True)
    ma20 = tech.get("ma20", 0)

    # 신호 2: RSI 극과매도 (28 이하)
    if rsi <= 28:
        signals.append(f"RSI 극과매도 ({rsi:.0f}) — 강한 반등 가능성")

    # 신호 3: RSI 과매도 + MA20 지지권 진입
    elif rsi <= 33 and not above_ma20 and ma20 and price >= ma20 * 0.97:
        signals.append(f"RSI 과매도 ({rsi:.0f}) + MA20 지지권 ({ma20:,.0f}원)")

    return signals


def run_watchlist_monitor(today: str, kis: KISClient) -> list[str]:
    """관심종목 진입 신호 스캔. 발송된 알림 텍스트 리스트 반환."""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text("""
                    SELECT code, name, target_entry, timeframe, reason
                    FROM watchlist_items
                    WHERE status = 'active'
                    ORDER BY priority DESC, code
                """)
            ).fetchall()
    except Exception as e:
        logger.warning("[모니터] 워치리스트 조회 실패: %s", e)
        return []

    fired = []
    for code, name, target_entry, timeframe, reason in rows:
        if _already_alerted(today, code, "entry"):
            continue
        try:
            pd = kis.get_stock_price(code, market=None)
            price = pd.get("price", 0)
            if not price:
                continue
            tech = _get_tech(code)
            signals = _check_entry_signals(code, price, target_entry, tech)
            if not signals:
                continue

            rsi_str = f" | RSI {tech['rsi14']:.0f}" if tech.get("rsi14") else ""
            ma_str  = f" | MA20 {'위' if tech.get('above_ma20') else '아래'}" if tech else ""
            lines = [
                f"📣 *진입신호* [{timeframe or '단기'}] {name}({code})",
                f"  현재가: {price:,}원{rsi_str}{ma_str}",
            ]
            for s in signals:
                lines.append(f"  ✅ {s}")
            if reason:
                lines.append(f"  주목이유: {reason}")

            alert_text = "\n".join(lines)
            fired.append(alert_text)
            _mark_alerted(today, code, "entry")
            logger.info("[모니터] 진입신호: %s(%s)", name, code)
        except Exception as e:
            logger.debug("[모니터] %s 진입신호 실패: %s", code, e)

    return fired


# ── 포트폴리오 손절/목표가 모니터 ─────────────────────────────────

def run_portfolio_monitor(today: str, kis: KISClient) -> tuple[list[str], list[str]]:
    """보유 포지션 손절가·목표가 도달 스캔. (stop_alerts, target_alerts) 반환."""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text("""
                    SELECT code, name, quantity, avg_price, stop_price, target_price, timeframe
                    FROM portfolio_positions
                    WHERE status = 'holding' AND quantity > 0
                """)
            ).fetchall()
    except Exception as e:
        logger.warning("[모니터] 포트폴리오 조회 실패: %s", e)
        return [], []

    stop_alerts, target_alerts = [], []
    for code, name, qty, avg_price, stop_price, target_price, timeframe in rows:
        try:
            pd = kis.get_stock_price(code, market=None)
            price = pd.get("price", 0)
            if not price:
                continue
            pnl_pct = (price - avg_price) / avg_price * 100 if avg_price else 0

            # 손절가 도달
            if stop_price and price <= stop_price:
                if not _already_alerted(today, code, "stop"):
                    stop_alerts.append(
                        f"🚨 *손절선 도달* {name}({code})\n"
                        f"  현재: {price:,}원  |  손절가: {stop_price:,.0f}원\n"
                        f"  평균단가: {avg_price:,.0f}원  |  수익률: {pnl_pct:+.1f}%  |  {qty}주\n"
                        f"  → *즉시 전량 매도 검토*"
                    )
                    _mark_alerted(today, code, "stop")
                    logger.info("[모니터] 손절선 도달: %s(%s) %+.1f%%", name, code, pnl_pct)

            # 목표가 도달
            if target_price and price >= target_price:
                if not _already_alerted(today, code, "target"):
                    target_alerts.append(
                        f"🎯 *목표가 도달* {name}({code})\n"
                        f"  현재: {price:,}원  |  목표가: {target_price:,.0f}원\n"
                        f"  평균단가: {avg_price:,.0f}원  |  수익률: {pnl_pct:+.1f}%  |  {qty}주\n"
                        f"  → 절반 익절 또는 전량 매도 검토"
                    )
                    _mark_alerted(today, code, "target")
                    logger.info("[모니터] 목표가 도달: %s(%s) %+.1f%%", name, code, pnl_pct)

        except Exception as e:
            logger.debug("[모니터] %s 포지션 조회 실패: %s", code, e)

    return stop_alerts, target_alerts


# ── 진입점 ────────────────────────────────────────────────────────

def run() -> None:
    """워치리스트 + 포트폴리오 통합 모니터링 (15분마다 호출)."""
    if not _is_market_hours():
        logger.debug("[실시간모니터] 장 외 시간 — 스킵")
        return

    today = datetime.now(_KST).strftime("%Y-%m-%d")
    logger.info("[실시간모니터] 실행 — %s", datetime.now(_KST).strftime("%H:%M"))

    try:
        kis = KISClient()
    except Exception as e:
        logger.error("[실시간모니터] KIS 연결 실패: %s", e)
        return

    # 관심종목 진입 신호
    try:
        entry_alerts = run_watchlist_monitor(today, kis)
        if entry_alerts:
            msg = "🔔 *워치리스트 진입 신호*\n\n" + "\n\n".join(entry_alerts)
            send_message(msg)
    except Exception as e:
        logger.error("[실시간모니터] 워치리스트 모니터 실패: %s", e)

    # 포트폴리오 손절/목표가
    try:
        stop_alerts, target_alerts = run_portfolio_monitor(today, kis)
        if stop_alerts:
            send_message("⚠️ *포트폴리오 긴급 경고*\n\n" + "\n\n".join(stop_alerts))
        if target_alerts:
            send_message("💰 *목표가 도달 알림*\n\n" + "\n\n".join(target_alerts))
    except Exception as e:
        logger.error("[실시간모니터] 포트폴리오 모니터 실패: %s", e)
