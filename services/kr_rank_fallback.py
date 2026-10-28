"""KIS 순위 조회(거래량·거래대금·등락률) 실패 시 네이버 전 종목 시세로 대체 (2026-10-28).

배경: kis_client의 순위 메서드는 실패하면 빈 리스트만 돌려줬다(폴백 0개) — KIS 장애·시간외·서킷브레이커 때
반등 스크리너·브리핑의 거래대금/등락률 상위가 통째로 비었다. 네이버 전 종목 수집(universe_screener.fetch_universe)이
종가·등락률·거래량·거래대금을 이미 갖고 있어 같은 순위를 코드로 만들 수 있다.

반환 행은 KIS 응답과 같은 필드명(stck_shrn_iscd 등)을 써서 기존 소비자가 그대로 읽는다.
외국인·기관 순매수 순위는 종목별 수급이 없어 대체하지 않는다(빈 리스트 유지).
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_CACHE_SEC = 120
_cache: dict = {"ts": 0.0, "rows": None}
_MARKET = {"J": "KOSPI", "Q": "KOSDAQ"}


def _universe_rows() -> list[dict]:
    if _cache["rows"] is not None and time.time() - _cache["ts"] < _CACHE_SEC:
        return _cache["rows"]
    from services.universe_screener import fetch_universe
    uni = fetch_universe()
    rows = uni.get("rows") or []
    if not uni.get("complete"):
        logger.warning("[순위폴백] 네이버 전 종목 수신 불완전 (%s/%s)", uni.get("fetched"), uni.get("expected"))
    _cache.update(ts=time.time(), rows=rows)
    return rows


def _to_kis_row(r: dict, rank: int) -> dict:
    return {
        "stck_shrn_iscd": r["code"], "mksc_shrn_iscd": r["code"], "hts_kor_isnm": r["name"],
        "stck_prpr": str(int(r["close"])), "prdy_ctrt": str(r["chg_pct"] if r["chg_pct"] is not None else 0),
        "acml_vol": str(int(r.get("volume") or 0)), "acml_tr_pbmn": str(int((r.get("value_eok") or 0) * 1e8)),
        "data_rank": str(rank), "source": "naver",
    }


def rank_from_naver(kind: str, market: str = "J", rise: bool = True, top_n: int = 20) -> list[dict]:
    """kind: 'volume' | 'amount' | 'fluctuation'. 실패하면 [] (예외를 밖으로 던지지 않는다)."""
    try:
        mkt = _MARKET.get(market)
        if mkt is None:
            return []
        rows = [r for r in _universe_rows() if r["market"] == mkt]
        if kind == "volume":
            rows = sorted(rows, key=lambda r: r.get("volume") or 0, reverse=True)
        elif kind == "amount":
            rows = sorted(rows, key=lambda r: r.get("value_eok") or 0, reverse=True)
        elif kind == "fluctuation":
            rows = [r for r in rows if r.get("chg_pct") is not None]
            rows = sorted(rows, key=lambda r: r["chg_pct"], reverse=rise)
        else:
            return []
        out = [_to_kis_row(r, i + 1) for i, r in enumerate(rows[:top_n])]
        if out:
            logger.info("[순위폴백] KIS 순위 비어 네이버로 대체: %s %s (%d건)", kind, mkt, len(out))
        return out
    except Exception as e:
        logger.warning("[순위폴백] 네이버 대체 실패 (%s): %s", kind, e)
        return []
