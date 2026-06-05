"""
services/trading_service.py
안전 매수/매도 실행 서비스

안전 체크 → KIS 주문 → 포트폴리오 반영 → 텔레그램 확인 → DB 기록
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text

from db.database import get_conn
from clients.telegram_client import send_message, send_error_alert

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

# 1회 주문 최대 금액 (모의투자: 제한 없음, 실계좌: 안전장치)
_MAX_ORDER_AMOUNT_REAL = 5_000_000   # 500만원
_MAX_ORDER_AMOUNT_PAPER = 50_000_000  # 5000만원 (모의)


class TradingError(Exception):
    """주문 실행 불가 오류."""


def _init_order_table() -> None:
    """order_history 테이블 생성 (없으면)."""
    with get_conn() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS order_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT DEFAULT (datetime('now','localtime')),
                code        TEXT NOT NULL,
                name        TEXT,
                side        TEXT NOT NULL,
                qty         INTEGER NOT NULL,
                price       INTEGER NOT NULL,
                order_no    TEXT,
                mode        TEXT,
                success     INTEGER DEFAULT 0,
                message     TEXT,
                amount      INTEGER,
                memo        TEXT
            )
        """))


def _save_order(
    code: str, name: str, side: str,
    qty: int, price: int, order_no: str,
    mode: str, success: bool, message: str, memo: str = ""
) -> None:
    _init_order_table()
    with get_conn() as conn:
        conn.execute(text("""
            INSERT INTO order_history
            (code, name, side, qty, price, order_no, mode, success, message, amount, memo)
            VALUES (:code, :name, :side, :qty, :price, :ono, :mode, :ok, :msg, :amt, :memo)
        """), {
            "code": code, "name": name, "side": side,
            "qty": qty, "price": price, "ono": order_no,
            "mode": mode, "ok": int(success), "msg": message,
            "amt": qty * price, "memo": memo,
        })


def _resolve_name(kis, code: str) -> str:
    """종목명 조회 (실패 시 코드 반환)."""
    try:
        data = kis.get_stock_price(code, market=None)
        return data.get("name", code) if data else code
    except Exception:
        return code


def _get_current_price(kis, code: str) -> int:
    """현재가 조회 (주문가 미지정 시 참고용)."""
    try:
        data = kis.get_stock_price(code, market=None)
        return int(data.get("price", 0)) if data else 0
    except Exception:
        return 0


def execute_buy(
    code: str,
    qty: int,
    price: int = 0,
    memo: str = "",
    timeframe: str = "short",
    confirm: bool = True,
) -> dict:
    """
    매수 주문 실행.

    Args:
        code: 종목코드 (6자리)
        qty: 주문 수량
        price: 지정가 (0=시장가)
        memo: 투자 근거
        timeframe: short/mid/long
        confirm: True면 텔레그램으로 사전 확인 메시지 발송 (봇 흐름에선 False)

    Returns:
        {'success': bool, 'order_no': str, 'message': str, 'name': str, 'amount': int}
    """
    from clients.kis_client import KISClient
    from config.settings import KIS_IS_REAL

    _init_order_table()
    code = code.strip().zfill(6)

    try:
        kis = KISClient()
    except Exception as e:
        raise TradingError(f"KIS 클라이언트 초기화 실패: {e}")

    name = _resolve_name(kis, code)

    # 현재가 조회 (시장가 주문 시 금액 계산용)
    cur_price = _get_current_price(kis, code)
    exec_price = price if price > 0 else cur_price
    if exec_price <= 0:
        raise TradingError(f"{code} 현재가 조회 실패 — 장 마감 상태이거나 잘못된 코드")

    amount = qty * exec_price
    max_amount = _MAX_ORDER_AMOUNT_REAL if KIS_IS_REAL else _MAX_ORDER_AMOUNT_PAPER
    if amount > max_amount:
        raise TradingError(
            f"주문 금액 초과: {amount:,}원 > 한도 {max_amount:,}원\n"
            f"수량을 줄이거나 한도를 확인하세요."
        )

    # 잔고 체크
    try:
        balance = kis.get_account_balance()
        cash = balance.get("cash", 0)
        if KIS_IS_REAL and cash < amount:
            raise TradingError(
                f"잔고 부족: 필요 {amount:,}원 / 가용 {cash:,}원"
            )
    except TradingError:
        raise
    except Exception as e:
        logger.warning("[매수] 잔고 조회 실패 (주문은 계속): %s", e)

    mode_label = "실계좌" if KIS_IS_REAL else "모의계좌"
    price_label = f"{price:,}원 지정가" if price > 0 else "시장가"
    logger.info("[매수] %s(%s) %d주 @%s [%s]", name, code, qty, price_label, mode_label)

    # KIS 주문 실행
    result = kis.place_order(code=code, side="buy", qty=qty, price=price)
    success = result.get("success", False)
    order_no = result.get("order_no", "")
    message = result.get("message", "")

    # DB 기록
    _save_order(code, name, "buy", qty, exec_price, order_no, mode_label, success, message, memo)

    # 성공 시 포트폴리오에 자동 등록
    if success:
        try:
            from services.portfolio_service import add_position
            add_position(code, name, qty, exec_price, timeframe=timeframe, memo=memo)
            logger.info("[매수] 포트폴리오 자동 등록: %s", code)
        except Exception as e:
            logger.warning("[매수] 포트폴리오 등록 실패: %s", e)

    # 텔레그램 알림
    icon = "✅" if success else "❌"
    msg = (
        f"{icon} *{'매수 완료' if success else '매수 실패'}* ({mode_label})\n\n"
        f"종목: {name}({code})\n"
        f"수량: {qty:,}주\n"
        f"가격: {price_label}\n"
        f"금액: 약 {amount:,}원\n"
        f"주문번호: {order_no or '없음'}\n"
        + (f"메모: {memo}\n" if memo else "")
        + (f"메시지: {message}" if not success else "")
    )
    try:
        send_message(msg)
    except Exception as e:
        logger.warning("[매수] 텔레그램 발송 실패: %s", e)

    return {
        "success": success,
        "order_no": order_no,
        "message": message,
        "name": name,
        "code": code,
        "amount": amount,
    }


def execute_sell(
    code: str,
    qty: int,
    price: int = 0,
    memo: str = "",
) -> dict:
    """
    매도 주문 실행.

    Args:
        code: 종목코드
        qty: 주문 수량 (0이면 전량)
        price: 지정가 (0=시장가)
        memo: 매도 사유

    Returns:
        {'success': bool, 'order_no': str, 'message': str, 'name': str}
    """
    from clients.kis_client import KISClient
    from config.settings import KIS_IS_REAL

    _init_order_table()
    code = code.strip().zfill(6)

    try:
        kis = KISClient()
    except Exception as e:
        raise TradingError(f"KIS 클라이언트 초기화 실패: {e}")

    name = _resolve_name(kis, code)

    # 보유 수량 체크
    actual_qty = qty
    try:
        holdings = kis.get_holdings()
        holding = next((h for h in holdings if h.get("code") == code), None)
        if not holding:
            raise TradingError(f"{name}({code})를 보유하고 있지 않습니다.")
        owned_qty = holding.get("qty", 0)
        if qty == 0:
            actual_qty = owned_qty
        elif qty > owned_qty:
            raise TradingError(
                f"매도 수량 초과: 요청 {qty}주 / 보유 {owned_qty}주"
            )
    except TradingError:
        raise
    except Exception as e:
        logger.warning("[매도] 보유 수량 조회 실패: %s", e)

    cur_price = _get_current_price(kis, code)
    exec_price = price if price > 0 else cur_price
    price_label = f"{price:,}원 지정가" if price > 0 else "시장가"
    mode_label = "실계좌" if KIS_IS_REAL else "모의계좌"

    logger.info("[매도] %s(%s) %d주 @%s [%s]", name, code, actual_qty, price_label, mode_label)

    result = kis.place_order(code=code, side="sell", qty=actual_qty, price=price)
    success = result.get("success", False)
    order_no = result.get("order_no", "")
    message = result.get("message", "")

    _save_order(code, name, "sell", actual_qty, exec_price, order_no, mode_label, success, message, memo)

    # 성공 시 포트폴리오 청산 기록
    if success:
        try:
            from services.portfolio_service import close_position
            close_position(code, exec_price if exec_price > 0 else None)
            logger.info("[매도] 포트폴리오 청산 기록: %s", code)
        except Exception as e:
            logger.warning("[매도] 포트폴리오 청산 실패: %s", e)

    icon = "✅" if success else "❌"
    amount = actual_qty * exec_price
    msg = (
        f"{icon} *{'매도 완료' if success else '매도 실패'}* ({mode_label})\n\n"
        f"종목: {name}({code})\n"
        f"수량: {actual_qty:,}주\n"
        f"가격: {price_label}\n"
        f"금액: 약 {amount:,}원\n"
        f"주문번호: {order_no or '없음'}\n"
        + (f"사유: {memo}\n" if memo else "")
        + (f"메시지: {message}" if not success else "")
    )
    try:
        send_message(msg)
    except Exception as e:
        logger.warning("[매도] 텔레그램 발송 실패: %s", e)

    return {
        "success": success,
        "order_no": order_no,
        "message": message,
        "name": name,
        "code": code,
        "amount": amount,
    }


def get_pending_orders() -> list[dict]:
    """미체결 주문 조회."""
    from clients.kis_client import KISClient
    try:
        kis = KISClient()
        return kis.get_pending_orders()
    except Exception as e:
        logger.warning("[주문조회] 실패: %s", e)
        return []


def cancel_order(order_no: str, code: str, side: str, qty: int, price: int = 0) -> dict:
    """주문 취소."""
    from clients.kis_client import KISClient
    try:
        kis = KISClient()
        result = kis.cancel_order(order_no, code, side, qty, price)
        success = result.get("success", False)
        msg = (
            f"{'✅ 주문 취소 완료' if success else '❌ 주문 취소 실패'}\n"
            f"주문번호: {order_no}\n"
            f"메시지: {result.get('message', '')}"
        )
        try:
            send_message(msg)
        except Exception:
            pass
        return result
    except Exception as e:
        logger.error("[주문취소] 실패: %s", e)
        return {"success": False, "message": str(e)}


def get_order_history(limit: int = 20) -> list[dict]:
    """최근 주문 이력 조회."""
    _init_order_table()
    try:
        with get_conn() as conn:
            rows = conn.execute(text("""
                SELECT created_at, code, name, side, qty, price, amount,
                       order_no, mode, success, message, memo
                FROM order_history
                ORDER BY id DESC LIMIT :lim
            """), {"lim": limit}).fetchall()
        return [
            {
                "created_at": r[0], "code": r[1], "name": r[2],
                "side": r[3], "qty": r[4], "price": r[5], "amount": r[6],
                "order_no": r[7], "mode": r[8], "success": bool(r[9]),
                "message": r[10], "memo": r[11],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("[주문이력] 조회 실패: %s", e)
        return []


def format_order_history(limit: int = 10) -> str:
    """최근 주문 이력 텍스트 포맷."""
    orders = get_order_history(limit)
    if not orders:
        return "📋 최근 주문 이력 없음"
    lines = [f"📋 *최근 주문 이력 ({len(orders)}건)*\n"]
    for o in orders:
        side_icon = "🟢 매수" if o["side"] == "buy" else "🔴 매도"
        ok_icon = "✅" if o["success"] else "❌"
        lines.append(
            f"{ok_icon} {side_icon} {o['name']}({o['code']}) "
            f"{o['qty']:,}주 @{o['price']:,}원 [{o['mode']}]\n"
            f"   {o['created_at'][:16]}"
        )
    return "\n".join(lines)
