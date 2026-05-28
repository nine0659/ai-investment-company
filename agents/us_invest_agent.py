"""
미국 주식 주간 투자 추천 에이전트
매주 일요일 20:30 KST 실행
성장주 TOP3 + 배당 ETF TOP2 + 섹터 ETF TOP2 추천
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf

from clients.openai_client import chat_ceo
from clients.telegram_client import send_message
from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

# 성장주 후보
_GROWTH_STOCKS = [
    ("NVDA", "엔비디아"),
    ("META", "메타"),
    ("GOOGL", "알파벳"),
    ("MSFT", "마이크로소프트"),
    ("AMZN", "아마존"),
    ("TSLA", "테슬라"),
    ("AAPL", "애플"),
    ("AMD", "AMD"),
    ("PLTR", "팔란티어"),
    ("CRWD", "크라우드스트라이크"),
    ("SNOW", "스노우플레이크"),
    ("SMCI", "슈퍼마이크로"),
]

# 배당 ETF 후보
_DIVIDEND_ETFS = [
    ("SCHD", "Schwab US Dividend Equity ETF"),
    ("VYM", "Vanguard High Dividend Yield ETF"),
    ("HDV", "iShares Core High Dividend ETF"),
    ("DGRO", "iShares Core Dividend Growth ETF"),
    ("DVY", "iShares Select Dividend ETF"),
]

# 섹터 ETF 후보
_SECTOR_ETFS = [
    ("XLK", "기술"),
    ("XLV", "헬스케어"),
    ("XLE", "에너지"),
    ("XLF", "금융"),
    ("XLI", "산업재"),
    ("SOXX", "반도체"),
    ("ARKK", "혁신/디스럽션"),
    ("GLD", "금"),
    ("TLT", "미국장기채"),
]


def _fetch_stock_data(ticker: str) -> dict:
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="3mo")
        if hist.empty:
            return {}
        info = t.info or {}
        cur = float(hist["Close"].iloc[-1])
        w1_ago = float(hist["Close"].iloc[-6]) if len(hist) >= 6 else cur
        m1_ago = float(hist["Close"].iloc[-22]) if len(hist) >= 22 else cur
        vol_avg = float(hist["Volume"].tail(20).mean()) if len(hist) >= 20 else 0
        vol_cur = float(hist["Volume"].iloc[-1])

        chg_1w = (cur - w1_ago) / w1_ago * 100 if w1_ago else 0
        chg_1m = (cur - m1_ago) / m1_ago * 100 if m1_ago else 0
        vol_ratio = vol_cur / vol_avg if vol_avg else 1.0

        return {
            "ticker": ticker,
            "price": cur,
            "change_1w": round(chg_1w, 2),
            "change_1m": round(chg_1m, 2),
            "vol_ratio": round(vol_ratio, 2),
            "market_cap": info.get("marketCap", 0),
            "pe_ratio": info.get("trailingPE"),
            "dividend_yield": info.get("dividendYield"),
            "sector": info.get("sector", ""),
        }
    except Exception as e:
        logger.debug("[US투자] %s 데이터 조회 실패: %s", ticker, e)
        return {}


def _score_growth(d: dict) -> float:
    score = 0.0
    score += min(d.get("change_1w", 0) * 3, 30)
    score += min(d.get("change_1m", 0) * 1.5, 30)
    vr = d.get("vol_ratio", 1.0)
    if vr >= 2.0:
        score += 25
    elif vr >= 1.5:
        score += 15
    elif vr >= 1.2:
        score += 8
    pe = d.get("pe_ratio")
    if pe and 10 < pe < 50:
        score += 15
    return round(score, 1)


def _score_dividend(d: dict) -> float:
    score = 0.0
    dy = (d.get("dividend_yield") or 0) * 100
    score += min(dy * 10, 40)
    score += min(d.get("change_1m", 0) * 2, 30)
    score += min(d.get("change_1w", 0) * 2, 20)
    vr = d.get("vol_ratio", 1.0)
    score += min((vr - 1.0) * 10, 10)
    return round(score, 1)


def _score_sector(d: dict) -> float:
    score = 0.0
    score += min(d.get("change_1w", 0) * 4, 40)
    score += min(d.get("change_1m", 0) * 2, 30)
    vr = d.get("vol_ratio", 1.0)
    if vr >= 2.0:
        score += 30
    elif vr >= 1.5:
        score += 20
    elif vr >= 1.2:
        score += 10
    return round(score, 1)


def collect_candidates() -> dict:
    """각 카테고리 후보 데이터 수집 및 스코어링."""
    growth_data, div_data, sector_data = [], [], []

    for ticker, name in _GROWTH_STOCKS:
        d = _fetch_stock_data(ticker)
        if d:
            d["name"] = name
            d["score"] = _score_growth(d)
            growth_data.append(d)

    for ticker, name in _DIVIDEND_ETFS:
        d = _fetch_stock_data(ticker)
        if d:
            d["name"] = name
            d["score"] = _score_dividend(d)
            div_data.append(d)

    for ticker, name in _SECTOR_ETFS:
        d = _fetch_stock_data(ticker)
        if d:
            d["name"] = name
            d["score"] = _score_sector(d)
            sector_data.append(d)

    return {
        "growth": sorted(growth_data, key=lambda x: -x["score"])[:5],
        "dividend": sorted(div_data, key=lambda x: -x["score"])[:3],
        "sector": sorted(sector_data, key=lambda x: -x["score"])[:4],
    }


def _format_candidates(candidates: dict) -> str:
    lines = []
    for cat, label in [("growth", "성장주"), ("dividend", "배당ETF"), ("sector", "섹터ETF")]:
        lines.append(f"\n[{label} 후보]")
        for d in candidates.get(cat, []):
            dy_str = f" | 배당수익률 {d['dividend_yield']*100:.1f}%" if d.get("dividend_yield") else ""
            pe_str = f" | PER {d['pe_ratio']:.1f}" if d.get("pe_ratio") else ""
            lines.append(
                f"  {d['ticker']} ({d['name']}) | 현재가 ${d['price']:.2f}"
                f" | 1주 {d['change_1w']:+.1f}% | 1달 {d['change_1m']:+.1f}%"
                f" | 거래량배율 {d['vol_ratio']:.1f}x{dy_str}{pe_str} | 점수 {d['score']}"
            )
    return "\n".join(lines)


_SYSTEM = """당신은 미국 주식 투자 전문 애널리스트입니다.
제공된 데이터를 바탕으로 이번 주 최적 투자 종목을 선정하세요.

선정 기준:
- 성장주: 기술적 모멘텀(주간/월간 상승), 거래량 증가, 밸류에이션 적정성
- 배당 ETF: 배당수익률, 안정성, 최근 가격 흐름
- 섹터 ETF: 현재 시장 트렌드와 모멘텀 일치 여부

출력 형식 (반드시 준수):
## 성장주 TOP3
1. [티커] (종목명) | 매수가 $XXX | 목표가 $XXX | 손절가 $XXX
   근거: (2줄 이내)

2. [티커] (종목명) | 매수가 $XXX | 목표가 $XXX | 손절가 $XXX
   근거: (2줄 이내)

3. [티커] (종목명) | 매수가 $XXX | 목표가 $XXX | 손절가 $XXX
   근거: (2줄 이내)

## 배당 ETF TOP2
1. [티커] (ETF명) | 매수가 $XXX | 배당수익률 X.X% | 보유기간 X개월 권장
   근거: (1줄)

2. [티커] (ETF명) | 매수가 $XXX | 배당수익률 X.X% | 보유기간 X개월 권장
   근거: (1줄)

## 섹터 ETF TOP2
1. [티커] (섹터명) | 매수가 $XXX | 목표가 $XXX
   근거: (1줄)

2. [티커] (섹터명) | 매수가 $XXX | 목표가 $XXX
   근거: (1줄)

## 이번 주 미국 시장 전망
(3줄 이내)

## 투자 전략 요약
(2줄 이내)"""


def run() -> str:
    logger.info("[US투자] 주간 추천 생성 시작")
    now = datetime.now(_KST)
    date = now.strftime("%Y-%m-%d")

    try:
        candidates = collect_candidates()
        logger.info("[US투자] 후보 수집 완료 — 성장주 %d개 배당ETF %d개 섹터ETF %d개",
                    len(candidates["growth"]), len(candidates["dividend"]), len(candidates["sector"]))
    except Exception as e:
        logger.error("[US투자] 후보 수집 실패: %s", e)
        return ""

    candidate_text = _format_candidates(candidates)
    report = chat_ceo(_SYSTEM, f"오늘 날짜: {date}\n\n{candidate_text}", max_tokens=2000)

    # DB 저장
    try:
        with get_conn() as conn:
            for cat, items in candidates.items():
                for d in items:
                    conn.execute(
                        text(
                            "INSERT INTO us_invest_recommendations "
                            "(date, category, ticker, name, price, change_1w, change_1m, score) "
                            "VALUES (:date, :cat, :ticker, :name, :price, :w1, :m1, :score)"
                        ),
                        {"date": date, "cat": cat, "ticker": d["ticker"],
                         "name": d.get("name", ""), "price": d.get("price"),
                         "w1": d.get("change_1w"), "m1": d.get("change_1m"),
                         "score": d.get("score")},
                    )
        logger.info("[US투자] DB 저장 완료")
    except Exception as e:
        logger.warning("[US투자] DB 저장 실패: %s", e)

    # 텔레그램 발송
    try:
        header = f"🇺🇸 *미국 주식 주간 추천* ({now.strftime('%Y.%m.%d')})\n\n"
        send_message(header + report)
        logger.info("[US투자] 텔레그램 발송 완료")
    except Exception as e:
        logger.error("[US투자] 텔레그램 발송 실패: %s", e)

    return report


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s %(message)s")
    run()
