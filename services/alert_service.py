"""
services/alert_service.py
긴급 기회 알림 서비스

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[긴급 알림의 의미]

이 시스템의 "긴급 알림"은 단순 위험 경고가 아닙니다.
"지금 이 순간 진입해야 기회를 잡을 수 있다"는 신호입니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🚀 OPPORTUNITY (기회 긴급 — 지금 매수 진입)

정의: 복수의 강한 상승 신호가 동시에 발생해 즉각 진입하지 않으면
       기회를 놓칠 가능성이 높은 상황

발동 조건 (아래 중 2개 이상 동시 충족):
  [기술적]
  · 관심종목 RSI ≤ 28 (극과매도) + 볼린저밴드 하단 터치
  · MA5/MA20 골든크로스 + 거래량 300% 이상 급증
  · 전일 급락 후 장 초반 강한 반등(+2% 이상) 시작
  [이벤트/뉴스 촉매]
  · 지정학 이벤트 → 명확한 수혜 섹터 존재 (방산·원유·방어주 등)
  · 정책 발표 → 특정 섹터 직접 수혜 (보조금·규제완화·금리인하)
  · 실적 서프라이즈 → 경쟁사 동반 상승 기대
  [수급 신호]
  · 외국인 대규모 순매수 전환 + EWY/EEM ETF 급등
  · 기관 대량 매수 + 공매도 잔고 감소

🚨 RISK (위험 긴급 — 즉시 포지션 점검)

정의: 예상치 못한 외부 충격으로 보유 포지션이 즉각 위협받는 상황

발동 조건:
  · 지정학 블랙스완 (미군기지 공격·핵·전쟁선포·서킷브레이커)
  · KOSPI -2.5% 이상 + 외국인 대량 매도 동반
  · 보유 종목 단일일 -7% 이상 급락
  · 원달러 환율 1,450원 돌파 (외국인 이탈 가속)
  · VIX 35 이상 패닉 수준 (시스템 리스크)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[카카오톡 설정]
환경변수: KAKAO_ACCESS_TOKEN
발급: https://developers.kakao.com → 앱 생성 → 카카오 로그인 → 액세스토큰
미설정 시 텔레그램으로만 발송 (OPPORTUNITY·RISK 모두)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

# ── 알림 타입 ────────────────────────────────────────────────────
TYPE_OPPORTUNITY = "opportunity"  # 🚀 지금 진입 기회
TYPE_RISK        = "risk"         # 🚨 즉각 포지션 점검
TYPE_ENTRY       = "entry"        # ✅ 관심종목 진입 신호 (실시간모니터)
TYPE_TARGET      = "target"       # 🎯 목표가 도달
TYPE_STOP        = "stop"         # 🛑 손절선 도달

_EMOJI = {
    TYPE_OPPORTUNITY: "🚀🚀🚀 *[기회 — 지금 진입]*",
    TYPE_RISK:        "🚨🚨🚨 *[위험 — 즉시 점검]*",
    TYPE_ENTRY:       "✅ *[진입 신호]*",
    TYPE_TARGET:      "🎯 *[목표가 도달]*",
    TYPE_STOP:        "🛑 *[손절선 도달]*",
}

# ── 위험 임계값 ──────────────────────────────────────────────────
RISK_KOSPI_CRASH   = -2.5    # KOSPI 급락 (%)
RISK_VIX_PANIC     = 35.0    # VIX 패닉 수준
RISK_USD_KRW       = 1450    # 원달러 긴급 수준 (원)
RISK_STOCK_CRASH   = -7.0    # 보유 종목 단일일 급락 (%)

# ── 기회 임계값 ──────────────────────────────────────────────────
OPP_RSI_OVERSOLD   = 28      # RSI 극과매도
OPP_BB_LOWER_PCT   = 5.0     # 볼린저밴드 하단 (%)
OPP_VOL_SURGE      = 250     # 거래량 급증 (5일 평균 대비 %)
OPP_REBOUND_PCT    = 2.0     # 반등 시작 (%)

# ── 지정학 RISK 키워드 ────────────────────────────────────────────
GEOPOLITICAL_RISK_KEYWORDS = [
    "전쟁선포", "미군기지 공격", "핵공격", "핵폭탄", "이란 공습",
    "호르무즈 봉쇄", "서킷브레이커", "거래정지",
    "iran attack", "us military strike", "nuclear", "war declaration",
    "circuit breaker",
]

# ── 기회 뉴스 키워드 → 수혜 섹터 매핑 ───────────────────────────
OPPORTUNITY_CATALYSTS: list[tuple[list[str], str, str]] = [
    # (뉴스 키워드 목록, 수혜 섹터, 대표 ETF/종목)
    (["이란", "중동 분쟁", "호르무즈"],     "방산·원유",   "한화에어로스페이스·현대로템"),
    (["북한 도발", "북한 미사일"],           "방산",        "한화에어로스페이스·LIG넥스원"),
    (["금리 인하", "기준금리 인하", "피벗"], "성장주·리츠", "카카오·크래프톤·리츠"),
    (["반도체 보조금", "AI 투자 확대"],      "반도체·AI",   "삼성전자·SK하이닉스"),
    (["원달러 급락", "달러 약세"],            "외국인 유입", "대형주·삼성전자"),
    (["VIX 급락", "공포지수 하락"],          "위험자산",    "성장주·반도체"),
    (["조선 수주", "LNG선 계약"],            "조선",        "HD현대중공업·삼성중공업"),
    (["2차전지 수주", "전기차 확대"],         "배터리",      "LG에너지솔루션·에코프로비엠"),
]


# ── 발송 함수 ─────────────────────────────────────────────────────

def _send_telegram(message: str) -> bool:
    try:
        from clients.telegram_client import send_message
        send_message(message)
        return True
    except Exception as e:
        logger.error("텔레그램 발송 실패: %s", e)
        return False


def _send_kakao(message: str) -> bool:
    """카카오톡 나에게 보내기 (토큰 자동 갱신 포함)."""
    try:
        from clients.kakao_client import send_message as kakao_send, is_configured
        if not is_configured():
            logger.warning(
                "[카카오톡] 미설정 — python scripts/kakao_setup.py 를 실행해 연동하세요"
            )
            return False
        return kakao_send(message)
    except Exception as e:
        logger.error("카카오톡 발송 오류: %s", e)
        return False


def send_alert(alert_type: str, title: str, body: str,
               code: str = "", name: str = "") -> None:
    """알림 발송 + DB 저장.

    발송 채널:
      OPPORTUNITY · RISK → 카카오톡만 (긴급 기회/위험)
      ENTRY · TARGET · STOP · 기타 → 텔레그램
    """
    today   = datetime.now(_KST).strftime("%Y-%m-%d")
    emoji   = _EMOJI.get(alert_type, "📌")
    message = f"{emoji}\n*{title}*\n\n{body}"

    # DB 저장 (웹 대시보드 표시)
    try:
        with get_conn() as conn:
            conn.execute(
                text(
                    "INSERT INTO alert_notifications (date, alert_type, code, name, message) "
                    "VALUES (:d, :t, :c, :n, :m)"
                ),
                {"d": today, "t": alert_type, "c": code or "MARKET", "n": name, "m": message},
            )
    except Exception as e:
        logger.debug("알림 DB 저장 실패: %s", e)

    if alert_type in (TYPE_OPPORTUNITY, TYPE_RISK):
        # 긴급 기회·위험 → 카카오톡 우선, 실패 시 텔레그램 fallback
        kakao_ok = _send_kakao(message)
        if not kakao_ok:
            # 카카오 미설정 또는 실패 → 텔레그램으로 발송 (긴급 표시 강화)
            urgent_message = (
                "🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴\n"
                + message +
                "\n🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴"
            )
            _send_telegram(urgent_message)
    else:
        # 일반 알림 (진입신호·목표가·손절) → 텔레그램
        _send_telegram(message)

    logger.info("[AlarmService] %s 발송: %s", alert_type.upper(), title)


# ── 중복 방지 ─────────────────────────────────────────────────────
# 종목 코드별 오늘 발송된 알림 타입을 메모리 캐시로도 관리 (DB 조회 감소 + 즉각 중복 차단)
import threading
_sent_cache: dict[str, set] = {}   # {today: {(code, type), ...}}
_sent_lock = threading.Lock()


def _already_sent(today: str, code: str, alert_type: str) -> bool:
    # 1. 메모리 캐시 먼저 확인 (빠름)
    with _sent_lock:
        if today in _sent_cache and (code, alert_type) in _sent_cache[today]:
            return True
    # 2. DB 확인
    try:
        with get_conn() as conn:
            row = conn.execute(
                text("SELECT 1 FROM price_alert_log WHERE date=:d AND code=:c AND type=:t"),
                {"d": today, "c": code, "t": alert_type},
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _mark_sent(today: str, code: str, alert_type: str) -> None:
    # 메모리 캐시 업데이트
    with _sent_lock:
        if today not in _sent_cache:
            # 오래된 날짜 캐시 정리
            _sent_cache.clear()
            _sent_cache[today] = set()
        _sent_cache[today].add((code, alert_type))
    # DB 기록
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
    except Exception:
        pass


def _already_sent_any_type(today: str, code: str, cooldown_minutes: int = 30) -> bool:
    """같은 종목에 대해 최근 cooldown_minutes 내 어떤 타입으로든 발송됐으면 True.
    긴급모니터 + 실시간모니터 중복 알림 방지용.
    """
    try:
        from datetime import timedelta
        cutoff = (datetime.now(_KST) - timedelta(minutes=cooldown_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        with get_conn() as conn:
            row = conn.execute(
                text("""
                    SELECT 1 FROM price_alert_log
                    WHERE date=:d AND code=:c AND sent_at >= :cutoff
                    LIMIT 1
                """),
                {"d": today, "c": code, "cutoff": cutoff},
            ).fetchone()
        return row is not None
    except Exception:
        return False


# ── 기회 감지 ─────────────────────────────────────────────────────

def check_opportunity_signals(code: str, name: str, price: int, tech: dict,
                               today: str) -> list[str]:
    """특정 종목의 기회 신호 감지. 신호 목록 반환."""
    signals = []

    rsi    = tech.get("rsi14", 50)
    bb_pct = tech.get("bb_pct", 50)
    vol    = tech.get("vol_ratio", 100)
    golden = tech.get("golden_cross", False)
    above  = tech.get("above_ma20", True)
    ma20   = tech.get("ma20", 0)

    # 신호1: RSI 극과매도 + 볼린저 하단
    if rsi <= OPP_RSI_OVERSOLD and bb_pct is not None and bb_pct <= OPP_BB_LOWER_PCT:
        signals.append(f"RSI {rsi:.0f} 극과매도 + 볼린저밴드 하단 터치 — 강한 반등 기대")

    # 신호2: 골든크로스 + 거래량 폭발
    if golden and vol >= OPP_VOL_SURGE:
        signals.append(f"MA5/MA20 골든크로스 발생 + 거래량 {vol:.0f}% 폭발 — 추세 전환 진입 시점")

    # 신호3: MA20 지지 + 거래량 급증 (눌림목 후 반등)
    if above and ma20 and price <= ma20 * 1.02 and vol >= 200:
        signals.append(f"MA20 지지권 반등 + 거래량 {vol:.0f}% 급증 — 눌림목 매수 기회")

    return signals


def check_news_opportunity(news_data: dict, today: str) -> None:
    """뉴스 기반 기회 알림 — 특정 촉매 이벤트가 섹터 수혜를 만드는 경우."""
    key = "news_opportunity"
    if _already_sent(today, "MARKET", key):
        return

    all_titles = []
    for items in news_data.values():
        for item in items:
            t = item.get("title", "")
            if t:
                all_titles.append(t)

    for keywords, sector, stocks in OPPORTUNITY_CATALYSTS:
        matched_kws = [kw for kw in keywords if any(kw in t for t in all_titles)]
        if len(matched_kws) >= 1:
            _mark_sent(today, "MARKET", key)
            send_alert(
                TYPE_OPPORTUNITY,
                f"뉴스 촉매 감지 → {sector} 섹터 기회",
                f"다음 이슈가 {sector} 섹터 상승 촉매로 작용할 수 있습니다:\n"
                f"감지 키워드: {', '.join(matched_kws)}\n\n"
                f"주목 종목: {stocks}\n\n"
                f"지금 진입 검토 — 섹터 ETF·뉴스·수급 동시 확인 후 결정",
            )
            break  # 하나만 발송


def check_watchlist_opportunity(today: str) -> None:
    """관심종목 중 기회 신호 2개 이상 동시 발생 종목 → 긴급 진입 알림."""
    try:
        from db.database import get_conn
        from sqlalchemy import text
        from clients.kis_client import KISClient
        from clients.market_data_client import fetch_kr_stock_technicals

        with get_conn() as conn:
            rows = conn.execute(
                text(
                    "SELECT code, name, target_entry FROM watchlist_items "
                    "WHERE status='active'"
                )
            ).fetchall()
        if not rows:
            return

        kis = KISClient()
        for code, name, target_entry in rows:
            alert_key = f"opp_{code}"
            if _already_sent(today, code, alert_key):
                continue
            try:
                pd    = kis.get_stock_price(code, market=None)
                price = pd.get("price", 0)
                if not price:
                    continue

                # 기술적 지표
                tech = {}
                for sfx in ("KS", "KQ"):
                    try:
                        t = fetch_kr_stock_technicals(f"{code}.{sfx}")
                        if t and t.get("rsi14"):
                            tech = t
                            break
                    except Exception:
                        pass

                signals = check_opportunity_signals(code, name, price, tech, today)

                # 목표 진입가 근접도 신호 추가
                if target_entry and abs(price - target_entry) / target_entry <= 0.01:
                    signals.append(f"목표진입가 {target_entry:,.0f}원 도달 (현재 {price:,}원)")

                if len(signals) >= 2:
                    _mark_sent(today, code, alert_key)
                    tech_info = (
                        f"RSI {tech['rsi14']:.0f} | "
                        f"BB% {tech.get('bb_pct',50):.0f}% | "
                        f"거래량 {tech.get('vol_ratio',100):.0f}%"
                    ) if tech else "기술지표 없음"

                    send_alert(
                        TYPE_OPPORTUNITY,
                        f"{name} — 지금이 진입 타이밍",
                        f"종목: {name}({code})\n"
                        f"현재가: {price:,}원 | {tech_info}\n\n"
                        f"발동 신호:\n" + "\n".join(f"  ✅ {s}" for s in signals)
                        + "\n\n복수 신호 동시 발생 — 지금 진입하지 않으면 기회를 놓칠 수 있습니다.",
                        code=code, name=name,
                    )
                    logger.info("[기회알림] %s(%s) — 신호 %d개", name, code, len(signals))

            except Exception as e:
                logger.debug("[기회알림] %s 체크 실패: %s", code, e)

    except Exception as e:
        logger.error("[기회알림] 관심종목 기회 체크 실패: %s", e)


# ── 위험 감지 ─────────────────────────────────────────────────────

def check_risk_signals(market_data: dict, news_data: dict, today: str) -> None:
    """시장·지정학 위험 신호 감지 → RISK 알림."""

    # KOSPI 급락
    kospi = market_data.get("kospi", {})
    if isinstance(kospi, dict):
        chg = kospi.get("change_pct", 0) or 0
        if chg <= RISK_KOSPI_CRASH and not _already_sent(today, "KOSPI", "crash"):
            _mark_sent(today, "KOSPI", "crash")
            send_alert(
                TYPE_RISK,
                f"KOSPI 급락 {chg:+.1f}% — 즉시 포지션 점검",
                f"KOSPI: {kospi.get('current', 0):,.2f}  ({chg:+.1f}%)\n\n"
                f"손절 조건 확인 필요. 외국인 수급 동향 즉시 확인.\n"
                f"반등 신호 없으면 현금 비중 확대 권고.",
            )

    # VIX 패닉
    vix = market_data.get("vix", {})
    if isinstance(vix, dict):
        v = vix.get("close", 0) or 0
        if v >= RISK_VIX_PANIC and not _already_sent(today, "VIX", "panic"):
            _mark_sent(today, "VIX", "panic")
            send_alert(
                TYPE_RISK,
                f"VIX {v:.1f} 패닉 수준 — RISK-OFF 전환",
                f"공포지수(VIX)가 {v:.1f}로 패닉 구간 진입.\n\n"
                f"신규 포지션 전면 중단. 현금 비중 최대화.\n"
                f"역발상 매수 검토는 VIX 30 이하 복귀 확인 후.",
            )

    # 환율 급등
    usd = market_data.get("usd_krw", {})
    if isinstance(usd, dict):
        rate = usd.get("close", 0) or 0
        if rate >= RISK_USD_KRW and not _already_sent(today, "USDKRW", "crisis"):
            _mark_sent(today, "USDKRW", "crisis")
            send_alert(
                TYPE_RISK,
                f"원달러 {rate:,.0f}원 — 외국인 이탈 위험",
                f"환율 {rate:,.0f}원 돌파. 외국인 KOSPI 대규모 매도 우려.\n\n"
                f"환율 연동 수출주 수혜(삼성전자·현대차) 확인.\n"
                f"외국인 수급 동향 실시간 모니터링 필요.",
            )

    # 지정학 블랙스완
    if news_data:
        all_titles = " ".join(
            item.get("title", "") for items in news_data.values() for item in items
        ).lower()
        matched = [kw for kw in GEOPOLITICAL_RISK_KEYWORDS if kw.lower() in all_titles]
        if matched and not _already_sent(today, "GEO", "blackswan"):
            _mark_sent(today, "GEO", "blackswan")
            send_alert(
                TYPE_RISK,
                "지정학 블랙스완 감지 — 즉시 포지션 점검",
                f"위험 키워드 감지: {', '.join(matched[:4])}\n\n"
                f"즉각 포지션 확인. 방산주 단기 수혜 가능성 확인.\n"
                f"원유 가격 변동 확인 (에너지주·항공주 영향).",
            )


def check_portfolio_risk(today: str) -> None:
    """보유 종목 단일일 급락 감지."""
    try:
        from clients.kis_client import KISClient
        from db.database import get_conn
        from sqlalchemy import text

        with get_conn() as conn:
            rows = conn.execute(
                text(
                    "SELECT code, name, avg_price FROM portfolio_positions "
                    "WHERE status='holding' AND quantity > 0"
                )
            ).fetchall()
        if not rows:
            return

        kis = KISClient()
        for code, name, avg_price in rows:
            key = f"stock_crash_{code}"
            if _already_sent(today, code, key):
                continue
            try:
                pd      = kis.get_stock_price(code, market=None)
                price   = pd.get("price", 0)
                chg_pct = pd.get("change_pct", 0) or 0
                if not price or chg_pct > RISK_STOCK_CRASH:
                    continue

                _mark_sent(today, code, key)
                pnl = (price - avg_price) / avg_price * 100 if avg_price else 0
                send_alert(
                    TYPE_RISK,
                    f"[보유종목] {name} 급락 {chg_pct:+.1f}%",
                    f"종목: {name}({code})\n"
                    f"현재가: {price:,}원  |  당일: {chg_pct:+.1f}%\n"
                    f"평균단가: {avg_price:,.0f}원  |  총 손익: {pnl:+.1f}%\n\n"
                    f"손절 조건 즉시 확인. 원인 파악 후 대응 결정.",
                    code=code, name=name,
                )
            except Exception as e:
                logger.debug("[위험] %s 급락 체크 실패: %s", code, e)

    except Exception as e:
        logger.error("[위험] 포트폴리오 점검 실패: %s", e)


# ── 통합 실행 ─────────────────────────────────────────────────────

def run_full_alert_check(market_data: dict, news_data: dict = None) -> None:
    """기회 + 위험 통합 체크. 긴급 모니터에서 호출."""
    today = datetime.now(_KST).strftime("%Y-%m-%d")
    news_data = news_data or {}

    # 1. 기회 감지
    try:
        check_watchlist_opportunity(today)
    except Exception as e:
        logger.error("[알림] 기회 체크 실패: %s", e)

    try:
        check_news_opportunity(news_data, today)
    except Exception as e:
        logger.error("[알림] 뉴스 기회 체크 실패: %s", e)

    # 2. 위험 감지
    try:
        check_risk_signals(market_data, news_data, today)
    except Exception as e:
        logger.error("[알림] 위험 체크 실패: %s", e)

    try:
        check_portfolio_risk(today)
    except Exception as e:
        logger.error("[알림] 포트폴리오 위험 체크 실패: %s", e)


# ── 하위 호환 (emergency_monitor_agent에서 호출) ───────────────────
def run_market_alert_check(market_data: dict, news_data: dict = None) -> None:
    run_full_alert_check(market_data, news_data)
