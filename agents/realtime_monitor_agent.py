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


# ── 자동 실행 안전 게이트 (P2-2) ──────────────────────────────────

# 마지막 자동 실행 시각 추적 (1분 내 재실행 방지)
_last_auto_execute: dict[str, datetime] = {}


def _auto_execute_gate(code: str, action: str) -> tuple[bool, str]:
    """자동 실행 안전 게이트.

    Returns:
        (통과 여부, 차단 사유 또는 빈 문자열)
    """
    # 게이트①: 장시간 확인 (09:00~15:20)
    if not _is_market_hours():
        return False, "장 외 시간"

    now = datetime.now(_KST)
    h, m = now.hour, now.minute
    if (h, m) > (15, 20):
        return False, "장 마감 임박 (15:20 이후 자동 실행 차단)"

    # 게이트②: 1분 내 동일 종목 재실행 방지
    gate_key = f"{code}:{action}"
    last = _last_auto_execute.get(gate_key)
    if last and (now - last).total_seconds() < 60:
        return False, f"1분 내 재실행 방지 (마지막: {last.strftime('%H:%M:%S')})"

    # 게이트③: 잔량 0주 체크 (매도 시)
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

    # 통과 — 마지막 실행 시각 갱신
    _last_auto_execute[gate_key] = now
    return True, ""


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

    rsi       = tech.get("rsi14", 50)
    above_ma20 = tech.get("above_ma20", True)
    ma20      = tech.get("ma20", 0)
    bb_pct    = tech.get("bb_pct", 50)
    vol_ratio = tech.get("vol_ratio", 100)
    golden    = tech.get("golden_cross", False)
    dead      = tech.get("dead_cross", False)

    # 신호 2: RSI 극과매도 (28 이하)
    if rsi <= 28:
        signals.append(f"RSI 극과매도 ({rsi:.0f}) — 강한 반등 가능성")

    # 신호 3: RSI 과매도 + MA20 지지권 진입
    elif rsi <= 33 and not above_ma20 and ma20 and price >= ma20 * 0.97:
        signals.append(f"RSI 과매도 ({rsi:.0f}) + MA20 지지권 ({ma20:,.0f}원)")

    # 신호 4: 볼린저밴드 하단 터치 (bb_pct ≤ 5%)
    if bb_pct is not None and bb_pct <= 5:
        bb_lower = tech.get("bb_lower", 0)
        signals.append(f"볼린저밴드 하단 터치 (BB% {bb_pct:.0f}%, 하단 {bb_lower:,.0f}원) — 과매도 반등 구간")

    # 신호 5: 거래량 급증 + 가격이 MA20 위 (돌파 시도)
    if vol_ratio >= 200 and above_ma20:
        signals.append(f"거래량 급증 ({vol_ratio:.0f}%, 5일 평균 대비) + MA20 상단 — 돌파 가능성")
    elif vol_ratio >= 300:
        signals.append(f"거래량 폭증 ({vol_ratio:.0f}%, 5일 평균 대비) — 세력 개입 의심, 주목")

    # 신호 6: MA5/MA20 골든크로스
    if golden:
        signals.append(f"MA5/MA20 골든크로스 발생 — 단기 추세 전환 신호")

    # 신호 7: MA5/MA20 데드크로스 (경고)
    if dead:
        signals.append(f"⚠️ MA5/MA20 데드크로스 — 추세 약화 경고 (진입 주의)")

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
        # 긴급모니터와 중복 방지 — 최근 30분 내 어떤 타입으로든 알림이 갔으면 스킵
        try:
            from services.alert_service import _already_sent_any_type
            if _already_sent_any_type(today, code, cooldown_minutes=30):
                logger.debug("[모니터] %s(%s) 최근 30분 내 알림 발송됨 — 중복 스킵", name, code)
                continue
        except Exception:
            pass
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
            _save_notification(today, "entry", code, name, alert_text)
            # 인라인 버튼 발송 (P1-3)
            try:
                entry_price = int(target_entry) if target_entry else 0
                buttons = [[
                    {"text": "✅ 즉시 매수", "callback_data": f"buy:{code}:1:{entry_price}"},
                    {"text": "👀 워치리스트만", "callback_data": f"ignore:{code}"},
                    {"text": "❌ 무시", "callback_data": f"ignore:{code}"},
                ]]
                send_message_with_buttons(alert_text, buttons)
            except Exception as _be:
                logger.debug("[모니터] 버튼 발송 실패, 일반 메시지로 대체: %s", _be)
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

            # ── 자동 실행 설정 로드 (DB 우선 — 프로세스 간 공유) ────────
            try:
                from config.settings import get_auto_setting
                AUTO_EXECUTE_STOP        = get_auto_setting("AUTO_EXECUTE_STOP")
                AUTO_EXECUTE_TARGET_HALF = get_auto_setting("AUTO_EXECUTE_TARGET_HALF")
            except Exception:
                AUTO_EXECUTE_STOP = False
                AUTO_EXECUTE_TARGET_HALF = False

            # 손절가 도달
            if stop_price and price <= stop_price:
                if not _already_alerted(today, code, "stop"):
                    msg = (
                        f"🚨 *손절선 도달* {name}({code})\n"
                        f"  현재: {price:,}원  |  손절가: {stop_price:,.0f}원\n"
                        f"  평균단가: {avg_price:,.0f}원  |  수익률: {pnl_pct:+.1f}%  |  {qty}주\n"
                        f"  → *즉시 전량 매도 검토*"
                    )
                    stop_alerts.append(msg)
                    _mark_alerted(today, code, "stop")
                    _save_notification(today, "stop", code, name, msg)

                    # P2-1: 자동 손절 실행
                    if AUTO_EXECUTE_STOP:
                        try:
                            gate_ok, gate_reason = _auto_execute_gate(code, "stop") if _auto_execute_gate else (False, "게이트 함수 없음")
                            if gate_ok:
                                from services.trading_service import execute_sell
                                execute_sell(code=code, qty=0, price=0, memo="자동손절")
                                msg += "\n🤖 *자동 손절 실행됨*"
                            else:
                                msg += f"\n⏸️ 자동 손절 차단: {gate_reason}"
                        except Exception as _ae:
                            logger.warning("[모니터] 자동 손절 실패: %s", _ae)
                            msg += f"\n❌ 자동 손절 오류: {_ae}"
                    else:
                        # 인라인 버튼 발송 (P1-3)
                        try:
                            buttons = [[
                                {"text": "🔴 즉시 매도", "callback_data": f"sell:{code}:0:0"},
                                {"text": "⏭️ 무시",      "callback_data": f"ignore:{code}"},
                            ]]
                            send_message_with_buttons(msg, buttons)
                        except Exception as _be:
                            logger.debug("[모니터] 손절 버튼 발송 실패: %s", _be)
                    logger.info("[모니터] 손절선 도달: %s(%s) %+.1f%%", name, code, pnl_pct)

            # 목표가 도달
            if target_price and price >= target_price:
                if not _already_alerted(today, code, "target"):
                    msg = (
                        f"🎯 *목표가 도달* {name}({code})\n"
                        f"  현재: {price:,}원  |  목표가: {target_price:,.0f}원\n"
                        f"  평균단가: {avg_price:,.0f}원  |  수익률: {pnl_pct:+.1f}%  |  {qty}주\n"
                        f"  → 절반 익절 또는 전량 매도 검토"
                    )
                    target_alerts.append(msg)
                    _mark_alerted(today, code, "target")
                    _save_notification(today, "target", code, name, msg)

                    # P2-1: 자동 절반 익절
                    if AUTO_EXECUTE_TARGET_HALF:
                        try:
                            gate_ok, gate_reason = _auto_execute_gate(code, "target") if _auto_execute_gate else (False, "게이트 함수 없음")
                            if gate_ok:
                                half_qty = qty // 2
                                if half_qty > 0:
                                    from services.trading_service import execute_sell
                                    execute_sell(code=code, qty=half_qty, price=0, memo="자동목표익절50%")
                                    msg += "\n🤖 *자동 절반 익절 실행됨*"
                            else:
                                msg += f"\n⏸️ 자동 익절 차단: {gate_reason}"
                        except Exception as _ae:
                            logger.warning("[모니터] 자동 익절 실패: %s", _ae)
                            msg += f"\n❌ 자동 익절 오류: {_ae}"
                    else:
                        # 인라인 버튼 발송 (P1-3)
                        try:
                            half_qty = qty // 2
                            buttons = [[
                                {"text": "💰 절반 익절", "callback_data": f"sell_half:{code}"},
                                {"text": "🔴 전량 매도", "callback_data": f"sell:{code}:0:0"},
                                {"text": "📌 홀드",      "callback_data": f"ignore:{code}"},
                            ]]
                            send_message_with_buttons(msg, buttons)
                        except Exception as _be:
                            logger.debug("[모니터] 목표가 버튼 발송 실패: %s", _be)
                    logger.info("[모니터] 목표가 도달: %s(%s) %+.1f%%", name, code, pnl_pct)

        except Exception as e:
            logger.debug("[모니터] %s 포지션 조회 실패: %s", code, e)

    return stop_alerts, target_alerts


# ── AI 추천 종목 장중 손절/목표가 모니터 ─────────────────────────

def run_ai_rec_monitor(today: str, kis: KISClient) -> tuple[list[str], list[str]]:
    """AI 추천 추적 중인 종목의 장중 손절·목표가 알림 (rec_stop/rec_target 타입으로 중복 방지)."""
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
        logger.warning("[모니터] AI추천 조회 실패: %s", e)
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
                        f"🚨 *[AI추천] 손절선 도달* {name}({code})\n"
                        f"  현재: {price:,}원  |  손절가: {stop_price:,.0f}원\n"
                        f"  진입가: {entry_price:,.0f}원  |  수익률: {ret_pct:+.1f}%\n"
                        f"  → 일중 손절 신호 (16:20 트래커에서 최종 확인)"
                    )
                    stop_alerts.append(msg)
                    _mark_alerted(today, code, "rec_stop")
                    _save_notification(today, "rec_stop", code, name, msg)
                    logger.info("[모니터] AI추천 손절선: %s(%s) %+.1f%%", name, code, ret_pct)

            if target_price and price >= target_price:
                if not _already_alerted(today, code, "rec_target"):
                    msg = (
                        f"🎯 *[AI추천] 목표가 도달* {name}({code})\n"
                        f"  현재: {price:,}원  |  목표가: {target_price:,.0f}원\n"
                        f"  진입가: {entry_price:,.0f}원  |  수익률: {ret_pct:+.1f}%\n"
                        f"  → 익절 검토 구간 진입"
                    )
                    target_alerts.append(msg)
                    _mark_alerted(today, code, "rec_target")
                    _save_notification(today, "rec_target", code, name, msg)
                    logger.info("[모니터] AI추천 목표가: %s(%s) %+.1f%%", name, code, ret_pct)

        except Exception as e:
            logger.debug("[모니터] %s AI추천 조회 실패: %s", code, e)

    return stop_alerts, target_alerts


# ── 진입점 ────────────────────────────────────────────────────────

def run() -> None:
    """워치리스트 + 포트폴리오 + AI추천 통합 모니터링 (15분마다 호출)."""
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

    # 관심종목 진입 신호 (버튼 발송은 run_watchlist_monitor 내부에서 처리됨)
    try:
        entry_alerts = run_watchlist_monitor(today, kis)
        # entry_alerts 목록은 버튼 메시지 발송 후 반환되므로 별도 일반 메시지 발송 불필요
        if entry_alerts:
            logger.info("[실시간모니터] 진입신호 %d건 버튼 발송 완료", len(entry_alerts))
    except Exception as e:
        logger.error("[실시간모니터] 워치리스트 모니터 실패: %s", e)

    # 포트폴리오 손절/목표가 (버튼 메시지는 run_portfolio_monitor 내부에서 발송, 여기서는 추가 발송 안 함)
    try:
        stop_alerts, target_alerts = run_portfolio_monitor(today, kis)
        # 자동 실행 비활성화 상태에서만 일반 메시지 추가 발송
        # (자동 실행 ON이면 run_portfolio_monitor 내부에서 이미 버튼 메시지 발송됨)
        for alert_msg in stop_alerts:
            if "자동 손절" not in alert_msg and "즉시 매도" not in alert_msg:
                send_message(alert_msg)
        for alert_msg in target_alerts:
            if "자동 절반" not in alert_msg and "전량 매도" not in alert_msg:
                send_message(alert_msg)
    except Exception as e:
        logger.error("[실시간모니터] 포트폴리오 모니터 실패: %s", e)

    # AI 추천 종목 장중 손절/목표가
    try:
        ai_stops, ai_targets = run_ai_rec_monitor(today, kis)
        if ai_stops:
            send_message("⚠️ *AI추천 긴급 경고*\n\n" + "\n\n".join(ai_stops))
        if ai_targets:
            send_message("💰 *AI추천 목표가 도달*\n\n" + "\n\n".join(ai_targets))
    except Exception as e:
        logger.error("[실시간모니터] AI추천 모니터 실패: %s", e)
