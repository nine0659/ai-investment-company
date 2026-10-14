"""다중 출처 교차검증 (2026-10-14 — 단일 출처가 하루치 봉을 빠뜨려 폭락을 놓친 사고 이후).

원칙: 두 출처가 어긋나면 어느 쪽이 맞는지 가려내거나(KRX를 중계하는 네이버 우선),
못 가리면 값을 쓰지 않고 '불일치'로 표시한다. 수정이 아니라 대체 또는 제거.
"""
import logging

logger = logging.getLogger(__name__)

FX_CLOSE_TOL_PCT = 1.0      # 환율 종가 차이 허용(%)
PRICE_TOL_PCT = 1.5         # 종목가 차이 허용(%) — 장중 시차 감안


def _pct_gap(a: float, b: float) -> float:
    return abs(a - b) / b * 100 if b else 0.0


def validate_fx(raw: dict) -> list[str]:
    """yfinance USDKRW를 네이버 환율과 대조. 크게 다르면 네이버 값으로 대체. 반환: 경고 목록."""
    from clients import naver_client as nv
    warns = []
    cur = (raw or {}).get("usd_krw")
    rows = nv.fx_usdkrw(2)
    if not rows:
        return warns
    latest = rows[0]
    if not cur:
        raw["usd_krw"] = {"close": latest["close"], "change_pct": latest["change_pct"],
                          "data_date": latest["date"], "symbol": "USDKRW(naver)", "source": "naver"}
        return ["환율 yfinance 누락 — 네이버 값 사용"]
    c = cur.get("close")
    if isinstance(c, (int, float)) and _pct_gap(c, latest["close"]) > FX_CLOSE_TOL_PCT:
        warns.append(f"환율 yfinance {c:,.1f} ≠ 네이버 {latest['close']:,.1f} — 네이버 값 사용")
        cur.update(close=latest["close"], change_pct=latest["change_pct"],
                   data_date=latest["date"], source="naver")
    return warns


def validate_holdings_prices(prices: dict[str, float]) -> dict[str, dict]:
    """보유 종목 {코드: 가격}을 네이버 시세와 대조. 반환: {코드: {ok, naver, gap_pct}}"""
    from clients import naver_client as nv
    out = {}
    for code, p in prices.items():
        q = nv.stock_quote(code)
        if not q:
            out[code] = {"ok": None, "naver": None, "gap_pct": None}
            continue
        gap = _pct_gap(p, q["price"])
        out[code] = {"ok": gap <= PRICE_TOL_PCT, "naver": q["price"], "gap_pct": round(gap, 2)}
    return out
