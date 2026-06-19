"""
services/alert_service.py
시장 경보 알림 서비스

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[알림의 목적]

이 시스템의 알림은 투자 판단을 위한 정보 제공입니다.
매매 타이밍 신호 발생이 아닙니다. 직접 분석 후 판단하세요.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📡 OPPORTUNITY (시장 이벤트 감지 — 정보 제공)

감지 대상:
  [섹터 이벤트]
  · 지정학 이벤트 → 관련 섹터 영향 분석 (방산·원유·방어주 등)
  · 정책 발표 → 특정 섹터 수혜 가능성 (보조금·규제완화·금리인하)
  · 실적 서프라이즈 → 동일 섹터 영향 파악
  [수급 이상 감지]
  · 외국인 대규모 순매수 전환 + EWY/EEM ETF 급등
  · 기관 대량 매수 + 워치리스트 종목 급변

🚨 RISK (시장 리스크 경보 — 즉시 점검)

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
미설정 시 텔레그램으로만 발송
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
TYPE_OPPORTUNITY = "opportunity"  # 📡 시장 이벤트 감지 (정보 제공)
TYPE_RISK        = "risk"         # 🚨 시장 리스크 경보
TYPE_ENTRY       = "entry"        # 📊 워치리스트 동향 (정보 제공)
TYPE_TARGET      = "target"       # 📈 수익 목표 구간 도달
TYPE_STOP        = "stop"         # ⚠️ 리스크 기준선 도달

_EMOJI = {
    TYPE_OPPORTUNITY: "📡 *[시장 이벤트 감지]*",
    TYPE_RISK:        "🚨🚨🚨 *[위험 — 즉시 점검]*",
    TYPE_ENTRY:       "📊 *[워치리스트 동향]*",
    TYPE_TARGET:      "📈 *[수익 목표 구간 도달]*",
    TYPE_STOP:        "⚠️ *[리스크 기준선 도달]*",
}

# ── 위험 임계값 ──────────────────────────────────────────────────
RISK_KOSPI_CRASH        = -2.5   # KOSPI 급락 (%)
RISK_VIX_PANIC          = 35.0   # VIX 패닉 수준
RISK_USD_KRW            = 1450   # 원달러 긴급 수준 (원)
RISK_USD_KRW_MIN_CHANGE = 0.5    # 환율 경보 최소 일일 상승폭 (%) — 노이즈 필터: 0.5% 미만은 무시
RISK_USD_KRW_KOSPI_CONFIRM = -0.3  # 환율 경보 발동을 위한 KOSPI 동반 약세 확인선 (%)
                                    # 환율만 오르고 KOSPI가 견조/강세면 "외국인 이탈" 진단이 틀린 것 → 발동 차단
RISK_STOCK_CRASH        = -7.0   # 보유 종목 단일일 급락 (%)

# ── 기회 임계값 ──────────────────────────────────────────────────
OPP_USD_KRW_DROP   = -1.0    # 원달러 급락 (%) — 원화 강세 = 외국인 유입 기회 신호

# ── 지정학 OPPORTUNITY 키워드 — 2개 이상 동시 등장해야 발동 (배경 기사 오작동 방지) ──
_GEO_OPPORTUNITY_KEYWORDS = frozenset({"이란", "중동 분쟁", "호르무즈", "북한 도발", "북한 미사일"})

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


# ── 섹터 이벤트 감지 (정보 제공용) ───────────────────────────────

def check_news_opportunity(news_data: dict, today: str) -> None:
    """뉴스 촉매 이벤트 감지 — 섹터 관심 정보 제공 (매매 신호 아님)."""
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
        # 지정학 키워드(이란·호르무즈·북한)는 배경 기사 오작동 방지를 위해 2개 이상 동시 등장 필요
        is_geo = bool(set(keywords) & _GEO_OPPORTUNITY_KEYWORDS)
        required = 2 if is_geo else 1
        if len(matched_kws) >= required:
            _mark_sent(today, "MARKET", key)
            send_alert(
                TYPE_OPPORTUNITY,
                f"섹터 이벤트 감지 → {sector}",
                f"다음 이슈가 {sector} 섹터와 관련됩니다:\n"
                f"감지 키워드: {', '.join(matched_kws)}\n\n"
                f"관련 섹터·종목: {stocks}\n\n"
                f"ℹ️ 투자 검토 참고 자료 — 직접 분석 후 판단 필요",
            )
            break


def check_watchlist_opportunity(today: str) -> None:
    """워치리스트 종목 가격 급변 감지 — 정보 제공 (매매 신호 아님)."""
    try:
        from db.database import get_conn
        from sqlalchemy import text
        from clients.kis_client import KISClient

        with get_conn() as conn:
            rows = conn.execute(
                text(
                    "SELECT code, name, reason FROM watchlist_items "
                    "WHERE status='active'"
                )
            ).fetchall()
        if not rows:
            return

        kis = KISClient()
        for code, name, reason in rows:
            alert_key = f"opp_{code}"
            if _already_sent(today, code, alert_key):
                continue
            try:
                pd      = kis.get_stock_price(code, market=None)
                price   = pd.get("price", 0)
                chg_pct = pd.get("change_pct", 0) or 0
                if not price:
                    continue

                # 3% 이상 급등락 시에만 정보 알림
                if abs(chg_pct) < 3.0:
                    continue

                direction = "급등" if chg_pct > 0 else "급락"
                _mark_sent(today, code, alert_key)
                send_alert(
                    TYPE_ENTRY,
                    f"워치리스트 동향 — {name} {chg_pct:+.1f}%",
                    f"종목: {name}({code})\n"
                    f"현재가: {price:,}원  |  당일 등락: {chg_pct:+.1f}% ({direction})\n"
                    f"모니터링 사유: {reason or '없음'}\n\n"
                    f"ℹ️ 투자 검토 참고 자료 — 직접 분석 후 판단 필요",
                    code=code, name=name,
                )
                logger.info("[동향감지] %s(%s) %+.1f%%", name, code, chg_pct)

            except Exception as e:
                logger.debug("[동향감지] %s 체크 실패: %s", code, e)

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

    # 환율 급등 경보 — 4중 조건: ① 임계값 초과 ② 최소 0.5% 이상 상승
    # ③ KOSPI 동반 약세(외국인 이탈 진단의 근거) ④ 당일 미발송
    # chg_pct > 0 이었던 기존 조건은 +0.01% 소음에도 발동 → RISK_USD_KRW_MIN_CHANGE 필터 추가
    # KOSPI가 견조/강세인데 환율만 올랐다면 "외국인 이탈 위험" 진단 자체가 틀린 것 → KOSPI 동반 확인 필수
    usd = market_data.get("usd_krw", {})
    if isinstance(usd, dict):
        rate        = usd.get("close", 0) or 0
        chg_pct     = usd.get("change_pct", 0) or 0
        kospi_chg   = (kospi.get("change_pct", 0) or 0) if isinstance(kospi, dict) else 0
        if (
            rate >= RISK_USD_KRW
            and chg_pct >= RISK_USD_KRW_MIN_CHANGE
            and kospi_chg <= RISK_USD_KRW_KOSPI_CONFIRM
            and not _already_sent(today, "USDKRW", "crisis")
        ):
            _mark_sent(today, "USDKRW", "crisis")
            send_alert(
                TYPE_RISK,
                f"원달러 {rate:,.0f}원 상승 — 외국인 이탈 위험",
                f"환율 {rate:,.0f}원 ({chg_pct:+.2f}%). 원화 약세 지속 중.\n"
                f"KOSPI {kospi_chg:+.1f}% 동반 약세 — 외국인 이탈 정황 확인.\n\n"
                f"외국인 KOSPI 대규모 매도 우려.\n"
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


def check_market_opportunity_signals(market_data: dict, today: str) -> None:
    """시장 데이터 기반 기회 신호 감지 (뉴스 키워드와 별개)."""

    # 원달러 급락 = 원화 강세 = 외국인 유입 환경 (기회 신호)
    usd = market_data.get("usd_krw", {})
    if isinstance(usd, dict):
        rate    = usd.get("close", 0) or 0
        chg_pct = usd.get("change_pct", 0) or 0
        if chg_pct <= OPP_USD_KRW_DROP and not _already_sent(today, "USDKRW", "krw_strengthen"):
            _mark_sent(today, "USDKRW", "krw_strengthen")
            send_alert(
                TYPE_OPPORTUNITY,
                f"원화 강세 {chg_pct:+.2f}% — 외국인 유입 환경 감지",
                f"USD/KRW: {rate:,.0f}원 ({chg_pct:+.2f}%)\n\n"
                f"원화 강세는 외국인 KOSPI 순매수 환경을 조성합니다.\n"
                f"대형주·성장주(삼성전자·SK하이닉스) 외국인 수급 모니터링 권장.\n"
                f"내수주·원자재 수입기업 비용 절감 효과 병행 확인.\n\n"
                f"ℹ️ 투자 검토 참고 자료 — 직접 분석 후 판단 필요",
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


# ── 장중 급반전(트렌드 역전) 분석 ───────────────────────────────────
# 단순 임계값 알림(RISK/OPPORTUNITY)은 환율·KOSPI 같은 단일 지표만 보고 쏘기 때문에,
# "환율은 올랐지만 반도체 강세로 KOSPI가 오후에 급반등" 같은 날엔 거짓 경보가 된다.
# 당일 KOSPI 변동폭(저점·고점)을 추적해 뚜렷한 트렌드 반전이 감지되면,
# 단순 경보 대신 뉴스·반도체 대형주 동향을 묶어 "왜 반전됐는지 + 어떻게 대응할지"를 LLM으로 분석해 알린다.

REVERSAL_MIN_SWING  = 1.3   # 저점↔고점 대비 최소 반전폭 (%p)
REVERSAL_TROUGH_MAX = -1.0  # 반등 인정을 위한 최소 저점 (이 이하로 빠졌어야 "반등"으로 인정)
REVERSAL_PEAK_MIN   = 1.0   # 반락 인정을 위한 최소 고점
REVERSAL_LEADERS    = [("005930", "삼성전자"), ("000660", "SK하이닉스")]

_REVERSAL_SYSTEM = """당신은 시장 반전 원인 분석 전문가입니다.
오늘 장중 KOSPI가 뚜렷한 트렌드 반전(저점·고점 대비 큰 반등 또는 반락)을 보였습니다.
주어진 데이터만으로 반전 원인을 추정하고, 투자자가 지금 취해야 할 대응을 제시하세요.

[출력 형식]
📐 장중 반전 분석
오늘 흐름: [저점 X% → 고점 Y% → 현재 Z% (반전폭 W%p)]
추정 원인: [뉴스·반도체 대형주 동향·환율 등 근거 기반 1~2가지 — 확인 안 된 추측은 "추정"이라고 명시]
대응 전략: [지금 시점에서 포지션을 어떻게 가져가야 하는지 구체적으로]

근거가 부족하면 "데이터 부족으로 명확한 원인 특정 어려움. 추가 모니터링 필요"라고 명시할 것.
매매 신호 발생 아님 — 투자 판단을 위한 정보 제공."""


def _fmt_reversal_news(news_data: dict) -> str:
    lines = []
    for source, articles in (news_data or {}).items():
        for a in (articles or [])[:2]:
            if t := a.get("title"):
                lines.append(f"  [{source}] {t}")
        if len(lines) >= 8:
            break
    return "\n".join(lines) if lines else "없음"


def check_intraday_reversal(market_data: dict, news_data: dict, today: str) -> None:
    """KOSPI 당일 변동폭(저점·고점)을 추적해 뚜렷한 트렌드 반전 감지 시 원인·대응 분석 발송."""
    kospi = market_data.get("kospi", {})
    if not isinstance(kospi, dict):
        return
    chg = kospi.get("change_pct")
    if chg is None:
        return

    row = None
    try:
        with get_conn() as conn:
            row = conn.execute(
                text(
                    "SELECT min_kospi_chg, max_kospi_chg, reversal_alerted "
                    "FROM intraday_extremes WHERE date=:d"
                ),
                {"d": today},
            ).fetchone()

            if row is None:
                conn.execute(
                    text(
                        "INSERT INTO intraday_extremes "
                        "(date, min_kospi_chg, max_kospi_chg, reversal_alerted) "
                        "VALUES (:d, :c, :c, 0)"
                    ),
                    {"d": today, "c": chg},
                )
                return  # 오늘 첫 기록 — 비교 대상 없음

            prev_min, prev_max, alerted = row
            new_min = min(prev_min, chg)
            new_max = max(prev_max, chg)
            conn.execute(
                text(
                    "UPDATE intraday_extremes SET min_kospi_chg=:mn, max_kospi_chg=:mx, "
                    "updated_at=CURRENT_TIMESTAMP WHERE date=:d"
                ),
                {"mn": new_min, "mx": new_max, "d": today},
            )
    except Exception as e:
        logger.debug("[반전감지] DB 조회/갱신 실패: %s", e)
        return

    if alerted:
        return  # 오늘 이미 발송됨

    swing_up   = chg - new_min  # 저점 대비 회복폭
    swing_down = new_max - chg  # 고점 대비 하락폭

    is_rebound = new_min <= REVERSAL_TROUGH_MAX and swing_up >= REVERSAL_MIN_SWING
    is_selloff = new_max >= REVERSAL_PEAK_MIN  and swing_down >= REVERSAL_MIN_SWING
    if not (is_rebound or is_selloff):
        return

    direction = "반등" if is_rebound else "반락"
    swing     = swing_up if is_rebound else swing_down

    # 반도체 대형주 동향 (반전 원인 추정 보조 데이터)
    try:
        from clients.kis_client import KISClient
        kis = KISClient()
        leader_lines = []
        for code, name in REVERSAL_LEADERS:
            pd = kis.get_stock_price(code, market="J")
            if pd.get("price"):
                leader_lines.append(f"  {name}({code}): {pd.get('change_pct', 0):+.2f}%")
        leaders_text = "\n".join(leader_lines) if leader_lines else "조회 실패"
    except Exception as e:
        logger.debug("[반전감지] 반도체 대형주 조회 실패: %s", e)
        leaders_text = "조회 실패"

    # 반전 원인 추정용 뉴스 — 평소 5분 체크용(max_per=2)보다 넉넉히 수집 (드물게만 호출됨)
    try:
        from clients.news_client import fetch_static_rss
        reversal_news = fetch_static_rss(max_per=4)
    except Exception:
        reversal_news = news_data or {}

    context = (
        f"오늘 KOSPI 흐름: 저점 {new_min:+.2f}% → 고점 {new_max:+.2f}% → 현재 {chg:+.2f}% "
        f"({direction} {swing:.1f}%p)\n\n"
        f"[반도체 대형주 당일 등락]\n{leaders_text}\n\n"
        f"[최근 뉴스 헤드라인]\n{_fmt_reversal_news(reversal_news)}"
    )

    try:
        from clients.openai_client import chat
        analysis = chat(_REVERSAL_SYSTEM, context, max_tokens=500)
    except Exception as e:
        logger.warning("[반전감지] LLM 분석 실패: %s", e)
        return

    if not _send_telegram(f"📐 *장중 급{direction} 감지*\n\n{analysis}"):
        return

    try:
        with get_conn() as conn:
            conn.execute(
                text("UPDATE intraday_extremes SET reversal_alerted=1 WHERE date=:d"),
                {"d": today},
            )
    except Exception:
        pass

    logger.info("[반전감지] %s %.1f%%p 감지 — 분석 발송 완료", direction, swing)


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

    try:
        check_market_opportunity_signals(market_data, today)
    except Exception as e:
        logger.error("[알림] 시장 기회 신호 체크 실패: %s", e)

    # 2. 위험 감지
    try:
        check_risk_signals(market_data, news_data, today)
    except Exception as e:
        logger.error("[알림] 위험 체크 실패: %s", e)

    try:
        check_portfolio_risk(today)
    except Exception as e:
        logger.error("[알림] 포트폴리오 위험 체크 실패: %s", e)

    # 3. 장중 급반전 분석 (단순 임계값 알림과 별개 — 트렌드 역전 감지 시에만 발동)
    try:
        check_intraday_reversal(market_data, news_data, today)
    except Exception as e:
        logger.error("[알림] 반전 감지 체크 실패: %s", e)


# ── 하위 호환 (emergency_monitor_agent에서 호출) ───────────────────
def run_market_alert_check(market_data: dict, news_data: dict = None) -> None:
    run_full_alert_check(market_data, news_data)
