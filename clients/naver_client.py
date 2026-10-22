"""네이버 금융 공개 JSON 클라이언트 — yfinance/KIS와 독립된 제2 출처.

용도: ① 교차검증(가격·환율·지수) ② KRX 투자자별 순매수 금액(억원) ③ yfinance가
놓친 최신 봉 보강. 모든 함수는 실패 시 None/빈 값을 반환하고 예외를 올리지 않는다
(호출부는 기존 출처 값을 그대로 쓴다 — 전부-아니면-전무 방지).
"""
import logging

logger = logging.getLogger(__name__)
_UA = {"User-Agent": "Mozilla/5.0"}


_SESSION = None


def _session():
    """공유 세션 — 호출마다 requests.get으로 연결을 새로 맺으면 Windows에서 20건에 17초·CPU 16초,
    세션을 재사용하면 1.2초였다(2026-10-22 실측: 프록시 조회+TLS 핸드셰이크 반복). 전 종목 스크리너가 수천 건을 부른다."""
    global _SESSION
    if _SESSION is None:
        import requests
        from requests.adapters import HTTPAdapter
        _SESSION = requests.Session()
        _SESSION.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=16))
    return _SESSION


def _get(url: str):
    try:
        r = _session().get(url, headers=_UA, timeout=8)
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


def industry_groups() -> list[dict]:
    """네이버 업종 그룹 목록 [{no, name, totalCount, changeRate, riseCount, fallCount, ...}]. 실패 시 []."""
    # pageSize 상한은 100 (200이면 400 에러 → 빈 목록 → 업종 이름이 '업종282'로 폴백되던 사고, 2026-10-22). 전체 79개 업종.
    j = _get("https://m.stock.naver.com/api/stocks/industry?page=1&pageSize=100")
    return [g for g in (j or {}).get("groups", []) if isinstance(g, dict) and g.get("no") is not None]


def stock_integration(code: str) -> dict:
    """종목 상세(PER·PBR·EPS·추정치·52주·수급·컨센서스·업종코드). 실패 시 {}."""
    j = _get(f"https://m.stock.naver.com/api/stock/{code}/integration")
    return j if isinstance(j, dict) and j.get("totalInfos") else {}
