"""
services/alert_service.py
긴급 알림 서비스 — 명확한 기준 정의 + 텔레그램·카카오톡 발송

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[긴급사항 정의 — Alert Level 기준]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🚨 LEVEL 1 — CRITICAL (즉시 발송, 30초 이내)
  시장 충격:
    - KOSPI or KOSDAQ 단일일 -2.5% 이상 하락
    - VIX 32 이상 급등 (전일 대비 +5pt 이상)
    - 원달러 환율 1,450원 돌파
    - 미국 시장 서킷브레이커 발동
  지정학:
    - 키워드 감지: 전쟁선포 / 핵 / 미군기지 공격 / 이란 공습 / 호르무즈 봉쇄
    - 중동 분쟁 확전 → 원유 +5% 이상 급등
  포트폴리오:
    - 보유 종목 단일일 -7% 이상 하락
    - 손절가 도달 (realtime_monitor_agent에서 별도 처리)

⚠️ LEVEL 2 — URGENT (장 시작 30분 전 또는 즉시)
  시장:
    - KOSPI or KOSDAQ -1.5% 이상
    - 미국 선물 -1.5% 이상 (장 전 브리핑 보완)
    - 원달러 환율 1,420원 돌파
  수급:
    - 국민연금·연기금 대규모 매도 기사 감지
    - 외국인 KOSPI 5,000억 이상 단일일 순매도
  포트폴리오:
    - 보유 종목 단일일 -4% 이상 하락
    - 목표가 도달 (realtime_monitor_agent에서 별도 처리)

📌 LEVEL 3 — IMPORTANT (정규 스케줄 알림)
  - 워치리스트 진입 신호 (realtime_monitor_agent 처리)
  - DART 주요 공시 (dart_alert_agent 처리)
  - 관심종목 RSI 과매도 진입 신호

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[카카오톡 연동 설정]
  환경변수: KAKAO_ACCESS_TOKEN (카카오 REST API 토큰)
  미설정 시: 텔레그램으로만 발송
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

# ── 레벨 상수 ──────────────────────────────────────────────────

LEVEL_CRITICAL  = 1   # 🚨 즉시 발송
LEVEL_URGENT    = 2   # ⚠️ 긴급
LEVEL_IMPORTANT = 3   # 📌 중요 (정규)

_LEVEL_EMOJI = {
    LEVEL_CRITICAL:  "🚨🚨🚨 *[긴급 CRITICAL]*",
    LEVEL_URGENT:    "⚠️ *[URGENT 긴급]*",
    LEVEL_IMPORTANT: "📌 *[IMPORTANT 중요]*",
}

# ── 긴급 기준 상수 ──────────────────────────────────────────────

CRITICAL_KOSPI_DROP      = -2.5   # KOSPI/KOSDAQ 하락률 기준 (%)
CRITICAL_VIX_LEVEL       = 32.0   # VIX 절대 수준
CRITICAL_VIX_SPIKE       = 5.0    # VIX 하루 상승폭 (pt)
CRITICAL_USD_KRW         = 1450   # 원달러 환율 기준 (원)
CRITICAL_OIL_SPIKE       = 5.0    # WTI 원유 단일일 상승률 (%)
CRITICAL_PORTFOLIO_DROP  = -7.0   # 보유 종목 단일일 하락 기준 (%)

URGENT_KOSPI_DROP        = -1.5   # KOSPI 하락률
URGENT_FUTURES_DROP      = -1.5   # 미국 선물 하락률
URGENT_USD_KRW           = 1420   # 원달러 환율 기준
URGENT_FOREIGN_SELL      = -500_000_000_000  # 외국인 순매도 5,000억 (원)
URGENT_PORTFOLIO_DROP    = -4.0   # 보유 종목 단일일 하락 기준 (%)

# 지정학 리스크 핵심 키워드 (이 키워드가 뉴스에 포함되면 CRITICAL)
GEOPOLITICAL_KEYWORDS = [
    "전쟁선포", "핵공격", "핵폭탄", "미군기지 공격", "이란 공습", "호르무즈 봉쇄",
    "이란 미군", "iran attack", "us military strike", "nuclear", "war declaration",
    "북한 핵", "북한 미사일", "서킷브레이커",
]

# 기관수급 위험 키워드 (URGENT)
INSTITUTIONAL_KEYWORDS = [
    "국민연금 매도", "국민연금 주식 비율", "연기금 대규모", "공적자금 매도",
    "외국인 대규모 매도", "외국인 순매도 급증",
]


# ── 텔레그램 발송 ───────────────────────────────────────────────

def _send_telegram(message: str) -> bool:
    try:
        from clients.telegram_client import send_message
        send_message(message)
        return True
    except Exception as e:
        logger.error("텔레그램 발송 실패: %s", e)
        return False


# ── 카카오톡 발송 ───────────────────────────────────────────────

def _send_kakao(message: str) -> bool:
    """카카오톡 나에게 보내기 (REST API).
    환경변수 KAKAO_ACCESS_TOKEN 필요.
    발급: https://developers.kakao.com → 내 애플리케이션 → 카카오 로그인 → 토큰 발급
    """
    token = os.getenv("KAKAO_ACCESS_TOKEN", "")
    if not token:
        return False
    try:
        import urllib.request
        import urllib.parse
        import json
        payload = {
            "object_type": "text",
            "text": message[:2000],  # 카카오 메시지 최대 2000자
            "link": {"web_url": "", "mobile_web_url": ""},
            "button_title": "확인",
        }
        data = urllib.parse.urlencode({
            "template_object": json.dumps(payload, ensure_ascii=False)
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://kapi.kakao.com/v2/api/talk/memo/default/send",
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            if result.get("result_code") == 0:
                logger.info("카카오톡 발송 완료")
                return True
            else:
                logger.warning("카카오톡 발송 실패: %s", result)
                return False
    except Exception as e:
        logger.error("카카오톡 발송 오류: %s", e)
        return False


# ── 통합 발송 함수 ──────────────────────────────────────────────

def send_alert(level: int, title: str, body: str, code: str = "", name: str = "") -> None:
    """긴급 알림 발송 + DB 저장.

    level: LEVEL_CRITICAL / LEVEL_URGENT / LEVEL_IMPORTANT
    """
    today    = datetime.now(_KST).strftime("%Y-%m-%d")
    emoji    = _LEVEL_EMOJI.get(level, "📌")
    message  = f"{emoji}\n*{title}*\n\n{body}"

    # DB 저장 (웹 UI 표시용)
    alert_type = {1: "critical", 2: "urgent", 3: "important"}.get(level, "important")
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
        logger.debug("알림 DB 저장 실패: %s", e)

    # 텔레그램은 모든 레벨 발송
    _send_telegram(message)

    # 카카오톡은 CRITICAL/URGENT만 발송
    if level <= LEVEL_URGENT:
        _send_kakao(message)

    logger.info("[AlarmService] Level%d 발송: %s", level, title)


# ── 중복 발송 방지 ───────────────────────────────────────────────

def _already_sent(today: str, alert_key: str) -> bool:
    try:
        with get_conn() as conn:
            row = conn.execute(
                text("SELECT 1 FROM price_alert_log WHERE date=:d AND code=:c AND type=:t"),
                {"d": today, "c": "ALERT", "t": alert_key},
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _mark_sent(today: str, alert_key: str) -> None:
    try:
        with get_conn() as conn:
            conn.execute(
                text("""
                    INSERT INTO price_alert_log (date, code, type)
                    VALUES (:d, 'ALERT', :t)
                    ON CONFLICT (date, code, type) DO UPDATE SET sent_at=CURRENT_TIMESTAMP
                """),
                {"d": today, "t": alert_key},
            )
    except Exception:
        pass


# ── 시장 긴급 상황 체크 함수들 ──────────────────────────────────

def check_market_crash(market_data: dict) -> None:
    """KOSPI/KOSDAQ 급락, VIX 급등, 환율 급등 체크."""
    today = datetime.now(_KST).strftime("%Y-%m-%d")

    kospi = market_data.get("kospi", {})
    if kospi:
        chg = kospi.get("change_pct", 0) or 0
        if chg <= CRITICAL_KOSPI_DROP:
            key = "kospi_crash"
            if not _already_sent(today, key):
                send_alert(
                    LEVEL_CRITICAL,
                    f"KOSPI 급락 {chg:+.1f}%",
                    f"KOSPI가 하루 {chg:+.1f}% 폭락하고 있습니다.\n"
                    f"현재 지수: {kospi.get('current', 0):,.2f}\n"
                    f"즉시 포지션 점검 및 손절 준비 필요합니다.",
                )
                _mark_sent(today, key)
        elif chg <= URGENT_KOSPI_DROP:
            key = "kospi_drop"
            if not _already_sent(today, key):
                send_alert(
                    LEVEL_URGENT,
                    f"KOSPI 하락 {chg:+.1f}%",
                    f"KOSPI가 {chg:+.1f}% 하락 중입니다.\n"
                    f"포지션 점검 권고. 손절 조건 확인 필요.",
                )
                _mark_sent(today, key)

    vix = market_data.get("vix", {})
    if vix:
        vix_val = vix.get("close", 0) or 0
        if vix_val >= CRITICAL_VIX_LEVEL:
            key = "vix_critical"
            if not _already_sent(today, key):
                send_alert(
                    LEVEL_CRITICAL,
                    f"VIX 공포지수 {vix_val:.1f} — 패닉 구간",
                    f"공포지수(VIX)가 {vix_val:.1f}로 패닉 수준에 도달했습니다.\n"
                    f"RISK-OFF 전환. 신규 포지션 전면 중단 권고.\n"
                    f"현금 비중 최대화. 역발상 매수 기회는 VIX 30 하향 후 검토.",
                )
                _mark_sent(today, key)

    usd_krw = market_data.get("usd_krw", {})
    if usd_krw:
        rate = usd_krw.get("close", 0) or 0
        if rate >= CRITICAL_USD_KRW:
            key = "usd_krw_critical"
            if not _already_sent(today, key):
                send_alert(
                    LEVEL_CRITICAL,
                    f"원달러 환율 {rate:,.0f}원 돌파",
                    f"원달러 환율이 {rate:,.0f}원을 돌파했습니다.\n"
                    f"외국인 대규모 KOSPI 매도 압력 우려.\n"
                    f"환율 연동 수출주(전자·자동차) 수혜, 수입 비중 높은 업종 불리.",
                )
                _mark_sent(today, key)
        elif rate >= URGENT_USD_KRW:
            key = "usd_krw_urgent"
            if not _already_sent(today, key):
                send_alert(
                    LEVEL_URGENT,
                    f"원달러 환율 {rate:,.0f}원 — 외국인 수급 주의",
                    f"원달러 환율 {rate:,.0f}원. 외국인 순매도 전환 가능성.\n"
                    f"환율 1,450원 돌파 시 CRITICAL 경보 발령.",
                )
                _mark_sent(today, key)


def check_geopolitical_news(news_data: dict) -> None:
    """지정학 리스크 키워드 감지 → CRITICAL 발령."""
    today = datetime.now(_KST).strftime("%Y-%m-%d")
    key = "geopolitical_critical"
    if _already_sent(today, key):
        return

    all_titles = []
    for source, items in news_data.items():
        for item in items:
            all_titles.append(item.get("title", "").lower())

    matched = []
    for kw in GEOPOLITICAL_KEYWORDS:
        for title in all_titles:
            if kw.lower() in title:
                matched.append(kw)
                break

    if matched:
        send_alert(
            LEVEL_CRITICAL,
            "지정학 리스크 — 긴급 뉴스 감지",
            f"다음 위험 키워드가 뉴스에서 감지되었습니다:\n"
            + "\n".join(f"  - {kw}" for kw in matched[:5])
            + "\n\n즉각 포지션 점검 필요. 원유·방산·금 가격 확인.",
        )
        _mark_sent(today, key)


def check_institutional_news(news_data: dict) -> None:
    """국민연금·연기금 대규모 수급 이슈 감지 → URGENT 발령."""
    today = datetime.now(_KST).strftime("%Y-%m-%d")
    key = "institutional_urgent"
    if _already_sent(today, key):
        return

    all_titles = []
    for source, items in news_data.items():
        for item in items:
            all_titles.append(item.get("title", ""))

    matched = []
    for kw in INSTITUTIONAL_KEYWORDS:
        for title in all_titles:
            if kw in title:
                matched.append((kw, title[:80]))
                break

    if matched:
        send_alert(
            LEVEL_URGENT,
            "국내 기관 수급 이슈 감지",
            "다음 기관 수급 관련 뉴스가 감지되었습니다:\n"
            + "\n".join(f"  - {kw}: {title}" for kw, title in matched[:3])
            + "\n\n국민연금·연기금의 수급 동향이 KOSPI에 직접 영향을 줄 수 있습니다.\n"
            "외국인 수급과 동반 악화 시 주의.",
        )
        _mark_sent(today, key)


def run_market_alert_check(market_data: dict, news_data: dict = None) -> None:
    """시장 데이터 + 뉴스 종합 긴급 체크. 스케줄러에서 호출."""
    try:
        check_market_crash(market_data)
    except Exception as e:
        logger.error("시장 급락 체크 실패: %s", e)

    if news_data:
        try:
            check_geopolitical_news(news_data)
        except Exception as e:
            logger.error("지정학 리스크 체크 실패: %s", e)

        try:
            check_institutional_news(news_data)
        except Exception as e:
            logger.error("기관 수급 체크 실패: %s", e)
