"""네이버 금융 공개 JSON 클라이언트 — yfinance/KIS와 독립된 제2 출처.

용도: ① 교차검증(가격·환율·지수) ② KRX 투자자별 순매수 금액(억원) ③ yfinance가
놓친 최신 봉 보강. 모든 함수는 실패 시 None/빈 값을 반환하고 예외를 올리지 않는다
(호출부는 기존 출처 값을 그대로 쓴다 — 전부-아니면-전무 방지).
"""
import logging

logger = logging.getLogger(__name__)
_UA = {"User-Agent": "Mozilla/5.0"}


def _get(url: str):
    try:
        import requests
        r = requests.get(url, headers=_UA, timeout=8)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.debug("네이버 조회 실패 (%s): %s", url, e)
    return None


def num(v) -> float | None:
    try:
        return float(str(v).replace(",", "").replace("+", ""))
    except (TypeError, ValueError):
        return None


def stock_quote(code: str) -> dict:
    """종목 현재가/등락률. {price, change_pct, status, time} 또는 {}."""
    j = _get(f"https://m.stock.naver.com/api/stock/{code}/basic")
    if not isinstance(j, dict):
        return {}
    price, pct = num(j.get("closePrice")), num(j.get("fluctuationsRatio"))
    if price is None or pct is None:
        return {}
    return {"price": price, "change_pct": pct, "status": j.get("marketStatus", ""),
            "time": j.get("localTradedAt", "")}


def fx_usdkrw(n: int = 3) -> list[dict]:
    """원/달러 일별(최신순). [{date, close, change_pct}]"""
    j = _get("https://m.stock.naver.com/front-api/marketIndex/prices"
             "?category=exchange&reutersCode=FX_USDKRW&page=1")
    out = []
    for row in ((j or {}).get("result") or [])[:n]:
        c, p = num(row.get("closePrice")), num(row.get("fluctuationsRatio"))
        if c is not None and p is not None:
            out.append({"date": row.get("localTradedAt", ""), "close": c, "change_pct": p})
    return out


def investor_trend(market: str = "KOSPI") -> dict:
    """투자자별 순매수(억원, +매수/-매도). {date, personal, foreign, institution} 또는 {}.
    장중이면 date가 오늘이고 '누적 중간값'이다(호출부가 장 마감 전 여부로 표기)."""
    j = _get(f"https://m.stock.naver.com/api/index/{market}/trend")
    if not isinstance(j, dict):
        return {}
    p, f, i = num(j.get("personalValue")), num(j.get("foreignValue")), num(j.get("institutionalValue"))
    if f is None or i is None:
        return {}
    b = str(j.get("bizdate", ""))
    date = f"{b[:4]}-{b[4:6]}-{b[6:8]}" if len(b) == 8 else ""
    return {"date": date, "personal": p, "foreign": f, "institution": i}


def market_value_page(market: str, page: int, size: int = 100) -> dict:
    """시가총액순 종목 목록 한 페이지. market=KOSPI|KOSDAQ. 실패 시 {} (size는 100이 상한)."""
    j = _get(f"https://m.stock.naver.com/api/stocks/marketValue/{market}?page={page}&pageSize={size}")
    return j if isinstance(j, dict) and isinstance(j.get("stocks"), list) else {}


def stock_integration(code: str) -> dict:
    """종목 상세(PER·PBR·EPS·추정치·52주·수급·컨센서스·업종코드). 실패 시 {}."""
    j = _get(f"https://m.stock.naver.com/api/stock/{code}/integration")
    return j if isinstance(j, dict) and j.get("totalInfos") else {}
