"""
KOSPI 주간 추세 분석 서비스
매주 일요일 18:00 KST 실행
KOSPI 지수 + 시총 상위 6종목 기술적 분석 → 추세 판단 + 투자 방향성
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf

from clients.openai_client import chat
from clients.telegram_client import send_message

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

_KOSPI_TICKER = "^KS11"

_BLUE_CHIPS = [
    {"code": "005930.KS", "name": "삼성전자",        "sector": "반도체"},
    {"code": "000660.KS", "name": "SK하이닉스",      "sector": "반도체"},
    {"code": "373220.KS", "name": "LG에너지솔루션",   "sector": "2차전지"},
    {"code": "207940.KS", "name": "삼성바이오로직스",  "sector": "바이오"},
    {"code": "005380.KS", "name": "현대차",           "sector": "자동차"},
    {"code": "005490.KS", "name": "POSCO홀딩스",     "sector": "소재"},
]

# 추세 → 이모지
_TREND_EMOJI = {
    "대세상승": "🚀",
    "상승국면": "📈",
    "단기조정": "⚠️",
    "횡보":     "➡️",
    "하락추세": "📉",
    "대세하락": "🚨",
}

# 투자 판단 → 이모지
_STANCE_EMOJI = {
    "투자확대":    "🟢",
    "투자지속":    "🟢",
    "저가매수준비": "🟡",
    "관망":        "🟡",
    "투자축소":    "🔴",
    "투자정지":    "🔴",
}


# ── 기술적 지표 계산 ─────────────────────────────────────────────

def _ma(closes: list[float], n: int) -> float:
    if len(closes) < n:
        return closes[-1] if closes else 0.0
    return sum(closes[-n:]) / n


def _ma_slope(closes: list[float], n: int, lookback: int = 5) -> float:
    """n일 이동평균의 최근 lookback일 변화율(%)"""
    if len(closes) < n + lookback:
        return 0.0
    curr = sum(closes[-n:]) / n
    prev = sum(closes[-(n + lookback):-lookback]) / n
    if prev == 0:
        return 0.0
    return (curr - prev) / prev * 100


def _rsi(closes: list[float], period: int = 14) -> float:
    """Wilder's smoothed RSI"""
    if len(closes) < period + 2:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_g / avg_l), 1)


# ── 추세 분류 ────────────────────────────────────────────────────

def _classify_trend(
    price: float,
    ma20: float, ma60: float, ma120: float,
    ma60_slope: float, ma120_slope: float,
    rsi: float,
) -> str:
    # 대세하락: 120MA 아래 + 120MA 하락 중
    if price < ma120 and ma120_slope < -0.3:
        return "대세하락"

    # 하락추세: 60MA 아래
    if price < ma60:
        return "하락추세"

    # 이하 60MA 위

    # 횡보: 60MA 위 + 60MA 방향성 없음
    if abs(ma60_slope) <= 0.2:
        return "횡보"

    # 60MA 하락(but 60MA 위) — 고점 이탈 초기
    if ma60_slope < 0:
        return "횡보"

    # 이하 60MA 위 + 60MA 상승

    # 단기조정: 60MA 위·상승 but 20MA 아래
    if price < ma20:
        return "단기조정"

    # 20MA + 60MA + 120MA 위, 60MA 상승
    if price > ma120 and ma120_slope > 0 and rsi > 55:
        return "대세상승"

    return "상승국면"


def _determine_stance(trend: str, rsi: float) -> str:
    if trend == "대세상승":
        return "투자확대" if rsi < 72 else "투자지속"
    if trend == "상승국면":
        return "투자지속"
    if trend == "단기조정":
        return "저가매수준비" if rsi < 45 else "관망"
    if trend == "횡보":
        return "관망"
    if trend == "하락추세":
        return "투자축소"
    if trend == "대세하락":
        return "투자정지"
    return "관망"


def _ma_align_label(price: float, ma20: float, ma60: float, ma120: float) -> str:
    if price > ma20 > ma60 > ma120:
        return "완전 정배열 ✅"
    if price > ma60 > ma120:
        return "중기 정배열"
    if price > ma120:
        return "장기선 위"
    if price < ma20 < ma60 < ma120:
        return "완전 역배열 ⚠️"
    return "혼조"


# ── 리포트 생성 ──────────────────────────────────────────────────

def generate_trend_report() -> str:
    now = datetime.now(_KST)

    # KOSPI 지수 데이터 (약 130 거래일 = 6개월)
    try:
        hist = yf.Ticker(_KOSPI_TICKER).history(period="6mo", interval="1d")
    except Exception as e:
        return f"📊 KOSPI 추세 분석 실패 — 데이터 수집 오류: {e}"

    if hist.empty or len(hist) < 25:
        return "📊 KOSPI 추세 분석 실패 — 데이터 부족"

    closes = hist["Close"].tolist()
    price  = closes[-1]

    ma5   = _ma(closes, 5)
    ma20  = _ma(closes, 20)
    ma60  = _ma(closes, 60)
    ma120 = _ma(closes, 120)

    ma60_slope  = _ma_slope(closes, 60)
    ma120_slope = _ma_slope(closes, 120)
    rsi         = _rsi(closes)

    weekly_ret  = (closes[-1] / closes[-6]  - 1) * 100 if len(closes) >= 6  else 0.0
    monthly_ret = (closes[-1] / closes[-22] - 1) * 100 if len(closes) >= 22 else 0.0

    trend  = _classify_trend(price, ma20, ma60, ma120, ma60_slope, ma120_slope, rsi)
    stance = _determine_stance(trend, rsi)

    # ── 대표 종목 분석 ───────────────────────────────────────────
    stock_lines    = []
    stock_summaries = []
    for chip in _BLUE_CHIPS:
        try:
            sh = yf.Ticker(chip["code"]).history(period="2mo", interval="1d")
            if sh.empty or len(sh) < 6:
                continue
            sc    = sh["Close"].tolist()
            sp    = sc[-1]
            sw    = (sc[-1] / sc[-6]  - 1) * 100 if len(sc) >= 6  else 0.0
            sm    = (sc[-1] / sc[-22] - 1) * 100 if len(sc) >= 22 else 0.0
            sma20 = _ma(sc, 20)
            vs20  = "▲" if sp > sma20 else "▼"
            srsi  = _rsi(sc)
            stock_lines.append(
                f"  {chip['name']}({chip['code'].replace('.KS', '')}): "
                f"{sp:,.0f}원 ({'+' if sw >= 0 else ''}{sw:.1f}%주간) {vs20}20MA  RSI {srsi}"
            )
            stock_summaries.append(
                f"{chip['name']}({chip['sector']}): {sp:,.0f}원 | "
                f"주간{'+' if sw >= 0 else ''}{sw:.1f}% | "
                f"월간{'+' if sm >= 0 else ''}{sm:.1f}% | "
                f"20MA {'위' if sp > sma20 else '아래'} | RSI {srsi}"
            )
        except Exception as e:
            logger.warning("[추세분석] 종목 데이터 실패 %s: %s", chip["code"], e)

    # ── GPT 심층 분석 ────────────────────────────────────────────
    gpt_data = (
        f"분석 기준일: {now.strftime('%Y-%m-%d')}\n"
        f"KOSPI: {price:,.2f}pt\n"
        f"MA5={ma5:,.2f}  MA20={ma20:,.2f}  MA60={ma60:,.2f}  MA120={ma120:,.2f}\n"
        f"MA60 기울기: {ma60_slope:+.2f}% (5일 변화)\n"
        f"MA120 기울기: {ma120_slope:+.2f}%\n"
        f"RSI14: {rsi}\n"
        f"주간 등락: {weekly_ret:+.2f}%\n"
        f"월간 등락: {monthly_ret:+.2f}%\n"
        f"추세 판단: {trend}\n"
        f"투자 판단: {stance}\n\n"
        f"대표 종목 현황:\n" + "\n".join(stock_summaries)
    )

    gpt_prompt = f"""KOSPI 시장 추세 분석 데이터를 토대로 투자자에게 실질적인 인사이트를 제공하세요.

{gpt_data}

다음 항목을 분석하세요:
1. 현재 추세({trend})가 지속될 가능성과 전환될 조건 (기술적 근거 명시)
2. 대세상승·조정·하락 판단 근거 — MA 배열, RSI, 대표 종목 동향을 종합
3. 다음 주 투자 전략 — {stance} 판단의 구체적 실행 방안 (매수/보유/축소 기준)
4. 반드시 주의할 리스크 또는 긍정 신호 1~2가지

출력 형식: 4줄 이내, 텔레그램 전송용 간결한 한국어, 이모지 적절히 활용"""

    analysis = chat(
        "당신은 주식시장 기술적 분석 전문가입니다. 핵심만 간결하게 분석합니다.",
        gpt_prompt,
        max_tokens=500,
    )

    # ── 리포트 조립 ──────────────────────────────────────────────
    align = _ma_align_label(price, ma20, ma60, ma120)

    report = (
        f"📊 *KOSPI 주간 추세 분석* ({now.strftime('%Y.%m.%d')} 기준)\n\n"
        f"━━ 지수 현황 ━━\n"
        f"KOSPI: {price:,.2f}pt  |  주간: {weekly_ret:+.2f}%  월간: {monthly_ret:+.2f}%\n"
        f"MA5: {ma5:,.0f}  MA20: {ma20:,.0f}  MA60: {ma60:,.0f}  MA120: {ma120:,.0f}\n"
        f"RSI14: {rsi}  |  MA 배열: {align}\n\n"
        f"━━ 추세 판단 ━━\n"
        f"{_TREND_EMOJI.get(trend, '❓')} *{trend}*\n"
        f"(MA60 기울기 {ma60_slope:+.2f}% / RSI {rsi})\n\n"
        f"━━ 대표 종목 동향 ━━\n"
        + "\n".join(stock_lines or ["  데이터 수집 실패"]) + "\n\n"
        f"━━ 투자 판단 ━━\n"
        f"{_STANCE_EMOJI.get(stance, '❓')} *{stance}*\n\n"
        f"━━ AI 분석 & 다음 주 전략 ━━\n{analysis}"
    )
    return report


def send_trend_report():
    logger.info("[추세분석] 주간 KOSPI 추세 리포트 시작")
    try:
        report = generate_trend_report()
        send_message(report)
        logger.info("[추세분석] 발송 완료")
    except Exception as e:
        logger.error("[추세분석] 실패: %s", e)
