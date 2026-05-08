"""
차트 패턴 분석 서비스
yfinance로 일봉 데이터 수집 → 기술적 지표 계산 → 종목 점수 반영
"""
import logging
import yfinance as yf
import pandas as pd

logger = logging.getLogger(__name__)


def _ticker(code: str) -> yf.Ticker:
    """KOSPI .KS, KOSDAQ .KQ 자동 시도"""
    for suffix in (".KS", ".KQ"):
        t = yf.Ticker(f"{code}{suffix}")
        try:
            hist = t.history(period="5d", interval="1d")
            if not hist.empty:
                return t
        except Exception:
            continue
    return yf.Ticker(f"{code}.KS")


def analyze_chart(stock_code: str, stock_name: str = "") -> dict:
    """
    종목의 주요 차트 지표 계산.
    반환 dict:
        ma5, ma20, ma60       : 이동평균 (원)
        ma_aligned            : 정배열 여부 (ma5 > ma20 > ma60)
        golden_cross          : ma5 > ma20
        vol_ratio             : 오늘 거래량 / 5일 평균 거래량
        pos_52w               : 현재가의 52주 고가 대비 위치 (%)
        bb_upper, bb_lower    : 볼린저밴드 상단/하단
        bb_break_up           : 상단 돌파 여부
        bb_break_down         : 하단 이탈 여부
        chart_score           : 종합 차트 점수 (0~100)
        current_price         : 현재가
    """
    base = {"chart_score": 0, "ma_aligned": False, "golden_cross": False,
            "vol_ratio": 1.0, "pos_52w": 50.0,
            "bb_break_up": False, "bb_break_down": False}
    try:
        t    = _ticker(stock_code)
        hist = t.history(period="3mo", interval="1d")
        if len(hist) < 20:
            return base

        close = hist["Close"]
        vol   = hist["Volume"]

        # 이동평균
        ma5  = float(close.rolling(5).mean().iloc[-1])
        ma20 = float(close.rolling(20).mean().iloc[-1])
        ma60 = float(close.rolling(min(60, len(close))).mean().iloc[-1])
        cur  = float(close.iloc[-1])

        # 거래량 비율
        avg_vol5 = float(vol.rolling(5).mean().iloc[-2])  # 전일까지 5일 평균
        today_vol = float(vol.iloc[-1])
        vol_ratio = round(today_vol / avg_vol5, 2) if avg_vol5 > 0 else 1.0

        # 52주 위치
        high_52 = float(close.rolling(min(252, len(close))).max().iloc[-1])
        low_52  = float(close.rolling(min(252, len(close))).min().iloc[-1])
        pos_52w = round((cur - low_52) / (high_52 - low_52) * 100, 1) if high_52 != low_52 else 50.0

        # 볼린저밴드 (20일)
        std20    = float(close.rolling(20).std().iloc[-1])
        bb_upper = round(ma20 + 2 * std20, 0)
        bb_lower = round(ma20 - 2 * std20, 0)

        # 정배열·골든크로스
        ma_aligned   = ma5 > ma20 > ma60
        golden_cross = ma5 > ma20

        # 볼린저밴드 돌파
        bb_break_up   = cur >= bb_upper
        bb_break_down = cur <= bb_lower

        # 점수화 (0~100)
        score = 0.0
        if ma_aligned:       score += 30
        elif golden_cross:   score += 15
        if vol_ratio >= 2.0: score += 25
        elif vol_ratio >= 1.5: score += 15
        elif vol_ratio >= 1.2: score += 8
        if pos_52w >= 90:    score += 20
        elif pos_52w >= 70:  score += 12
        elif pos_52w >= 50:  score += 6
        if cur > ma20:       score += 10
        if bb_break_up:      score += 5
        if bb_break_down:    score -= 10

        return {
            "current_price": round(cur, 0),
            "ma5":    round(ma5, 0),
            "ma20":   round(ma20, 0),
            "ma60":   round(ma60, 0),
            "ma_aligned":   ma_aligned,
            "golden_cross": golden_cross,
            "vol_ratio":    vol_ratio,
            "pos_52w":      pos_52w,
            "bb_upper":     bb_upper,
            "bb_lower":     bb_lower,
            "bb_break_up":  bb_break_up,
            "bb_break_down": bb_break_down,
            "chart_score":  round(max(0, min(score, 100)), 1),
        }
    except Exception as e:
        logger.debug("차트 분석 실패 (%s %s): %s", stock_code, stock_name, e)
        return base


def format_chart_summary(code: str, name: str, ch: dict) -> str:
    """차트 분석 결과를 프롬프트용 한 줄 요약"""
    trend = "정배열✨" if ch.get("ma_aligned") else ("골든크로스" if ch.get("golden_cross") else "약세배열")
    vol   = f"거래량{ch.get('vol_ratio', 1):.1f}배"
    pos   = f"52주고점대비{ch.get('pos_52w', 50):.0f}%"
    bb    = "BB상단돌파🔥" if ch.get("bb_break_up") else ("BB하단이탈⚠️" if ch.get("bb_break_down") else "")
    parts = [trend, vol, pos]
    if bb:
        parts.append(bb)
    return f"{name}({code}): {', '.join(parts)} [차트점수 {ch.get('chart_score', 0)}]"
