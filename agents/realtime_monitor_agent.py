"""
agents/realtime_monitor_agent.py
시장 급변 경보 모니터 — scheduler.py의 통합 시장 모니터(15분)에서 호출

목적: 투자 판단을 위한 정보 제공. 매매 타이밍 신호 발생 아님.
기능 (워치리스트 가격 급변은 alert_service.check_watchlist_opportunity가 처리 — 중복 제거):
  1. 보유 포지션 리스크 기준선 도달 알림 (포트폴리오 관리)
  2. 추적 종목 기준가격 도달 통보 (투자 검토 참고 자료)
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from clients.kis_client import KISClient
from clients.telegram_client import send_message, send_message_with_buttons
from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")


def _is_market_hours() -> bool:
    from utils.market_calendar import is_krx_trading_day
    if not is_krx_trading_day():
        return False
    now = datetime.now(_KST)
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


def _save_notification(today: str, alert_type: str, code: str, name: str, message: str) -> None:
    """alert_notifications 테이블에 알림 내용 저장 (웹 UI 표시용)."""
    try:
        with get_conn() as conn:
            conn.execute(
                text(
                    "INSERT INTO alert_notifications (date, alert_type, code, name, message) "
                    "VALUES (:d, :t, :c, :n, :m)"
                ),
                {"d": today, "t": alert_type, "c": code, "n": name, "m": message},
            )
    except Exception as e:
        logger.debug("알림 저장 실패: %s", e)


# ── 자동 실행 안전 게이트 ──────────────────────────────────────────

_last_auto_execute: dict[str, datetime] = {}


def _auto_execute_gate(code: str, action: str) -> tuple[bool, str]:
    """포지션 리스크 자동 실행 안전 게이트."""
    if not _is_market_hours():
        return False, "장 외 시간"

    now = datetime.now(_KST)
    h, m = now.hour, now.minute
    if (h, m) > (15, 20):
        return False, "장 마감 임박 (15:20 이후 자동 실행 차단)"

    gate_key = f"{code}:{action}"
    last = _last_auto_execute.get(gate_key)
    if last and (now - last).total_seconds() < 60:
        return False, f"1분 내 재실행 방지 (마지막: {last.strftime('%H:%M:%S')})"

    if action in ("stop", "target"):
        try:
            with get_conn() as conn:
                row = conn.execute(
                    text("SELECT quantity FROM portfolio_positions WHERE code=:c AND status='holding' LIMIT 1"),
                    {"c": code},
                ).fetchone()
            if row and row[0] <= 0:
                return False, "보유 수량 0주"
        except Exception as _e:
            logger.debug("[게이트] 잔량 체크 실패: %s", _e)

    _last_auto_execute[gate_key] = now
    return True, ""


# ── 포트폴리오 리스크 기준선 모니터 ────────────────────────────────

def run_portfolio_monitor(today: str, kis: KISClient) -> tuple[list[str], list[str]]:
    """보유 포지션 리스크 기준선·수익 목표 도달 알림 (포트폴리오 관리)."""
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
        logger.warning("[경보모니터] 포트폴리오 조회 실패: %s", e)
        return [], []

    stop_alerts, target_alerts = [], []
    for code, name, qty, avg_price, stop_price, target_price, timeframe in rows:
        try:
            pd = kis.get_stock_price(code, market=None)
            price = pd.get("price", 0)
            if not price:
                continue
            pnl_pct = (price - avg_price) / avg_price * 100 if avg_price else 0

            try:
                from config.settings import get_auto_setting
                AUTO_EXECUTE_STOP        = get_auto_setting("AUTO_EXECUTE_STOP")
                AUTO_EXECUTE_TARGET_HALF = get_auto_setting("AUTO_EXECUTE_TARGET_HALF")
            except Exception:
                AUTO_EXECUTE_STOP = False
                AUTO_EXECUTE_TARGET_HALF = False

            # 리스크 기준선 도달 알림
            if stop_price and price <= stop_price:
                if not _already_alerted(today, code, "stop"):
                    msg = (
                        f"⚠️ *포지션 리스크 알림* {name}({code})\n"
                        f"  현재가: {price:,}원  |  리스크 기준선: {stop_price:,.0f}원\n"
                        f"  평균단가: {avg_price:,.0f}원  |  수익률: {pnl_pct:+.1f}%  |  {qty}주\n"
                        f"  → 포지션 재검토 필요 (투자 판단은 직접 하세요)"
                    )
                    stop_alerts.append(msg)
                    _mark_alerted(today, code, "stop")
                    _save_notification(today, "stop", code, name, msg)

                    if AUTO_EXECUTE_STOP:
                        try:
                            gate_ok, gate_reason = _auto_execute_gate(code, "stop")
                            if gate_ok:
                                from services.trading_service import execute_sell
                                execute_sell(code=code, qty=0, price=0, memo="리스크기준선도달")
                                msg += "\n🤖 *사전 설정된 리스크 관리 규칙 실행됨*"
                            else:
                                msg += f"\n⏸️ 자동 실행 차단: {gate_reason}"
                        except Exception as _ae:
                            logger.warning("[경보모니터] 자동 실행 실패: %s", _ae)
                            msg += f"\n❌ 자동 실행 오류: {_ae}"
                        send_message(msg)
                    else:
                        buttons = [[
                            {"text": "🔍 포지션 검토", "callback_data": f"portfolio:{code}"},
                            {"text": "📌 확인함",      "callback_data": f"ignore:{code}"},
                        ]]
                        try:
                            send_message_with_buttons(msg, buttons)
                        except Exception as _be:
                            send_message(msg)
                    logger.info("[경보모니터] 리스크 기준선 도달: %s(%s) %+.1f%%", name, code, pnl_pct)

            # 수익 목표 구간 도달 알림
            if target_price and price >= target_price:
                if not _already_alerted(today, code, "target"):
                    msg = (
                        f"📈 *수익 목표 구간 도달* {name}({code})\n"
                        f"  현재가: {price:,}원  |  수익 목표선: {target_price:,.0f}원\n"
                        f"  평균단가: {avg_price:,.0f}원  |  수익률: {pnl_pct:+.1f}%  |  {qty}주\n"
                        f"  → 포트폴리오 재검토 시점 (투자 판단은 직접 하세요)"
                    )
                    target_alerts.append(msg)
                    _mark_alerted(today, code, "target")
                    _save_notification(today, "target", code, name, msg)

                    if AUTO_EXECUTE_TARGET_HALF:
                        try:
                            gate_ok, gate_reason = _auto_execute_gate(code, "target")
                            if gate_ok:
                                half_qty = qty // 2
                                if half_qty > 0:
                                    from services.trading_service import execute_sell
                                    execute_sell(code=code, qty=half_qty, price=0, memo="수익목표구간도달50%")
                                    msg += "\n🤖 *사전 설정된 수익 목표 규칙 실행됨*"
                            else:
                                msg += f"\n⏸️ 자동 실행 차단: {gate_reason}"
                        except Exception as _ae:
                            logger.warning("[경보모니터] 자동 실행 실패: %s", _ae)
                            msg += f"\n❌ 자동 실행 오류: {_ae}"
                        send_message(msg)
                    else:
                        buttons = [[
                            {"text": "🔍 포트폴리오 검토", "callback_data": f"portfolio:{code}"},
                            {"text": "📌 확인함",           "callback_data": f"ignore:{code}"},
                        ]]
                        try:
                            send_message_with_buttons(msg, buttons)
                        except Exception as _be:
                            send_message(msg)
                    logger.info("[경보모니터] 수익 목표 구간: %s(%s) %+.1f%%", name, code, pnl_pct)

        except Exception as e:
            logger.debug("[경보모니터] %s 포지션 조회 실패: %s", code, e)

    return stop_alerts, target_alerts


# ── 추적 종목 기준가격 도달 통보 ───────────────────────────────────

def run_ai_rec_monitor(today: str, kis: KISClient) -> tuple[list[str], list[str]]:
    """추적 중인 종목의 리스크/수익 기준선 도달 통보 (투자 검토 참고 자료)."""
    try:
        with get_conn() as conn:
            rows = conn.execute(text("""
                SELECT rt.rec_id, rt.code, rt.name,
                       rt.entry_price, rt.stop_price, rt.target_price
                FROM recommendation_tracking rt
                INNER JOIN (
                    SELECT rec_id, MAX(date) AS max_date
                    FROM recommendation_tracking
                    GROUP BY rec_id
                ) latest ON rt.rec_id = latest.rec_id AND rt.date = latest.max_date
                WHERE rt.status = 'tracking'
            """)).fetchall()
    except Exception as e:
        logger.warning("[경보모니터] 추적종목 조회 실패: %s", e)
        return [], []

    stop_alerts, target_alerts = [], []
    for rec_id, code, name, entry_price, stop_price, target_price in rows:
        try:
            pd = kis.get_stock_price(code, market=None)
            price = pd.get("price", 0)
            if not price:
                continue
            ret_pct = (price - entry_price) / entry_price * 100 if entry_price else 0

            if stop_price and price <= stop_price:
                if not _already_alerted(today, code, "rec_stop"):
                    msg = (
                        f"📋 *[추적 중] 리스크 기준선 도달* {name}({code})\n"
                        f"  현재: {price:,}원  |  리스크 기준선: {stop_price:,.0f}원\n"
                        f"  진입 기준가: {entry_price:,.0f}원  |  등락: {ret_pct:+.1f}%\n"
                        f"  → 투자 재검토 필요 (장마감 후 트래커에서 종합 확인)"
                    )
                    stop_alerts.append(msg)
                    _mark_alerted(today, code, "rec_stop")
                    _save_notification(today, "rec_stop", code, name, msg)
                    logger.info("[경보모니터] 추적 리스크 기준선: %s(%s) %+.1f%%", name, code, ret_pct)

            if target_price and price >= target_price:
                if not _already_alerted(today, code, "rec_target"):
                    msg = (
                        f"📋 *[추적 중] 수익 기준선 도달* {name}({code})\n"
                        f"  현재: {price:,}원  |  수익 기준선: {target_price:,.0f}원\n"
                        f"  진입 기준가: {entry_price:,.0f}원  |  등락: {ret_pct:+.1f}%\n"
                        f"  → 포트폴리오 검토 구간 진입 (장마감 후 트래커에서 종합 확인)"
                    )
                    target_alerts.append(msg)
                    _mark_alerted(today, code, "rec_target")
                    _save_notification(today, "rec_target", code, name, msg)
                    logger.info("[경보모니터] 추적 수익 기준선: %s(%s) %+.1f%%", name, code, ret_pct)

        except Exception as e:
            logger.debug("[경보모니터] %s 추적 조회 실패: %s", code, e)

    return stop_alerts, target_alerts


# ── 진입점 ────────────────────────────────────────────────────────

def run() -> None:
    """워치리스트 동향 + 포지션 리스크 + 추적종목 통합 경보 모니터링 (15분마다 호출)."""
    if not _is_market_hours():
        logger.debug("[경보모니터] 장 외 시간 — 스킵")
        return

    today = datetime.now(_KST).strftime("%Y-%m-%d")
    logger.info("[경보모니터] 실행 — %s", datetime.now(_KST).strftime("%H:%M"))

    try:
        kis = KISClient()
    except Exception as e:
        logger.error("[경보모니터] KIS 연결 실패: %s", e)
        return

    # 1. 보유 포지션 리스크 기준선 모니터
    try:
        stop_alerts, target_alerts = run_portfolio_monitor(today, kis)
        logger.info("[경보모니터] 리스크 알림 %d건 / 수익목표 알림 %d건", len(stop_alerts), len(target_alerts))
    except Exception as e:
        logger.error("[경보모니터] 포지션 리스크 모니터 실패: %s", e)

    # 2. 추적 종목 기준선 도달 통보
    try:
        ai_stops, ai_targets = run_ai_rec_monitor(today, kis)
        all_alerts = ai_stops + ai_targets
        if all_alerts:
            send_message("📋 *추적 종목 기준선 도달 통보*\n\n" + "\n\n".join(all_alerts))
    except Exception as e:
        logger.error("[경보모니터] 추적종목 모니터 실패: %s", e)
