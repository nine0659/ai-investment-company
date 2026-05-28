"""
agents/emergency_monitor_agent.py
긴급 시장 상황 모니터 — 5분마다 실행 (장중)

체크 항목:
  1. KOSPI/KOSDAQ 급락 (LEVEL 1: -2.5%, LEVEL 2: -1.5%)
  2. VIX 급등 (LEVEL 1: 32 이상)
  3. 원달러 환율 급등 (LEVEL 1: 1,450원, LEVEL 2: 1,420원)
  4. WTI 원유 급등 (LEVEL 1: 5% 이상)
  5. 뉴스 지정학 키워드 감지 (LEVEL 1)
  6. 보유 종목 단일일 급락 (LEVEL 1: -7%, LEVEL 2: -4%)
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")


def _is_market_hours() -> bool:
    now = datetime.now(_KST)
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute
    return (9, 0) <= (h, m) <= (15, 30)


def run() -> None:
    """긴급 모니터링 메인 함수 — 스케줄러에서 5분마다 호출."""
    if not _is_market_hours():
        logger.debug("[긴급모니터] 장 외 시간 — 스킵")
        return

    now_str = datetime.now(_KST).strftime("%H:%M")
    logger.info("[긴급모니터] 실행 — %s", now_str)

    # 1. 시장 데이터 수집
    try:
        from clients.market_data_client import fetch_global_market_data, fetch_kr_index_realtime
        market_data = fetch_global_market_data()
        kr_index    = fetch_kr_index_realtime()
        if kr_index:
            market_data["kospi"]  = kr_index.get("kospi", {})
            market_data["kosdaq"] = kr_index.get("kosdaq", {})
    except Exception as e:
        logger.warning("[긴급모니터] 시장 데이터 수집 실패: %s", e)
        market_data = {}

    # 2. 뉴스 수집 (5분마다 전체 수집은 과도 → 지정학 쿼리만)
    news_data: dict = {}
    try:
        from clients.news_client import fetch_compound_news
        news_data = fetch_compound_news(max_items=3)
    except Exception as e:
        logger.debug("[긴급모니터] 뉴스 수집 실패: %s", e)

    # 3. 긴급 알림 체크
    try:
        from services.alert_service import run_market_alert_check
        run_market_alert_check(market_data, news_data)
    except Exception as e:
        logger.error("[긴급모니터] 알림 체크 실패: %s", e)

    # 4. 보유 종목 급락 체크 (portfolio에 KIS 조회)
    try:
        _check_portfolio_crash()
    except Exception as e:
        logger.debug("[긴급모니터] 포트폴리오 급락 체크 실패: %s", e)

    logger.debug("[긴급모니터] 완료 — %s", now_str)


def _check_portfolio_crash() -> None:
    """보유 종목 단일일 급락 체크."""
    from datetime import date
    from clients.kis_client import KISClient
    from services.alert_service import (
        send_alert, _already_sent, _mark_sent,
        LEVEL_CRITICAL, LEVEL_URGENT,
        CRITICAL_PORTFOLIO_DROP, URGENT_PORTFOLIO_DROP,
    )
    from db.database import get_conn
    from sqlalchemy import text

    today = datetime.now(_KST).strftime("%Y-%m-%d")

    try:
        with get_conn() as conn:
            rows = conn.execute(
                text(
                    "SELECT code, name, avg_price FROM portfolio_positions "
                    "WHERE status='holding' AND quantity > 0"
                )
            ).fetchall()
    except Exception:
        return

    if not rows:
        return

    try:
        kis = KISClient()
    except Exception:
        return

    for code, name, avg_price in rows:
        try:
            pd = kis.get_stock_price(code, market=None)
            price = pd.get("price", 0)
            chg_pct = pd.get("change_pct", 0) or 0  # 당일 등락률
            if not price:
                continue

            if chg_pct <= CRITICAL_PORTFOLIO_DROP:
                key = f"portfolio_crash_{code}"
                if not _already_sent(today, key):
                    pnl = (price - avg_price) / avg_price * 100
                    send_alert(
                        LEVEL_CRITICAL,
                        f"[보유종목] {name} 급락 {chg_pct:+.1f}%",
                        f"종목: {name}({code})\n"
                        f"현재가: {price:,}원  |  당일 등락: {chg_pct:+.1f}%\n"
                        f"평균단가: {avg_price:,.0f}원  |  총 수익률: {pnl:+.1f}%\n\n"
                        f"즉시 손절 여부 검토 필요합니다.",
                        code=code, name=name,
                    )
                    _mark_sent(today, key)

            elif chg_pct <= URGENT_PORTFOLIO_DROP:
                key = f"portfolio_drop_{code}"
                if not _already_sent(today, key):
                    send_alert(
                        LEVEL_URGENT,
                        f"[보유종목] {name} 하락 {chg_pct:+.1f}%",
                        f"종목: {name}({code})\n"
                        f"현재가: {price:,}원  |  당일 등락: {chg_pct:+.1f}%\n"
                        f"포지션 점검 권고. 손절가와의 거리 확인 필요.",
                        code=code, name=name,
                    )
                    _mark_sent(today, key)

        except Exception as e:
            logger.debug("[긴급모니터] %s 종목 체크 실패: %s", code, e)
