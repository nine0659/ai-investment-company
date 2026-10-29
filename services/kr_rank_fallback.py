"""KIS 순위 조회(거래량·거래대금·등락률) 실패 시 네이버 전 종목 시세로 대체 (2026-10-28).

배경: kis_client의 순위 메서드는 실패하면 빈 리스트만 돌려줬다(폴백 0개) — KIS 장애·시간외·서킷브레이커 때
반등 스크리너·브리핑의 거래대금/등락률 상위가 통째로 비었다. 네이버 전 종목 수집(universe_screener.fetch_universe)이
종가·등락률·거래량·거래대금을 이미 갖고 있어 같은 순위를 코드로 만들 수 있다.

반환 행은 KIS 응답과 같은 필드명(stck_shrn_iscd 등)을 써서 기존 소비자가 그대로 읽는다.
외국인·기관 순매수 순위는 네이버 모바일 종목별 수급 API로 거래대금 상위 종목 안에서만 근사한다(rank_investor_from_naver, 2026-10-29).
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

import requests

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


_TREND_URL = "https://m.stock.naver.com/api/stock/{code}/trend"
_INVESTOR_POOL = 80   # 시장별로 수급을 조회할 거래대금 상위 종목 수 — 전 종목 순위가 아니라 근사다


def _signed_int(v) -> int | None:
    try:
        return int(str(v).replace(",", "").replace("+", ""))
    except (TypeError, ValueError):
        return None


def _latest_flow(code: str) -> dict | None:
    """종목의 가장 최근 영업일 외국인·기관 순매수 수량. 실패하면 None."""
    try:
        r = requests.get(_TREND_URL.format(code=code), headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        r.raise_for_status()
        row = (r.json() or [None])[0]
        if not row:
            return None
        return {"foreign": _signed_int(row.get("foreignerPureBuyQuant")),
                "institution": _signed_int(row.get("organPureBuyQuant")),
                "bizdate": row.get("bizdate")}
    except Exception:
        return None


def rank_investor_from_naver(who: str, market: str = "J", top_n: int = 20) -> list[dict]:
    """who: 'foreign' | 'institution'. 거래대금 상위 종목 안에서 순매수 금액(수량×종가) 상위를 KIS 응답 필드명으로 반환.
    KIS 전 종목 순위의 근사다(거래대금이 작은 종목은 빠짐). 조회 성공 종목이 절반 미만이면 믿을 수 없어 []."""
    try:
        mkt = _MARKET.get(market)
        if mkt is None or who not in ("foreign", "institution"):
            return []
        pool = sorted((r for r in _universe_rows() if r["market"] == mkt),
                      key=lambda r: r.get("value_eok") or 0, reverse=True)[:_INVESTOR_POOL]
        if not pool:
            return []
        with ThreadPoolExecutor(max_workers=8) as ex:
            flows = list(ex.map(lambda r: _latest_flow(r["code"]), pool))
        ok = [(r, f) for r, f in zip(pool, flows) if f and f.get(who) is not None]
        if len(ok) < len(pool) / 2:
            logger.warning("[순위폴백] 수급 조회 %d/%d만 성공 — 순위 생략 (%s %s)", len(ok), len(pool), who, mkt)
            return []
        ok = [(r, f) for r, f in ok if f[who] > 0]
        ok.sort(key=lambda rf: rf[1][who] * rf[0]["close"], reverse=True)
        field = "frgn_ntby_qty" if who == "foreign" else "inst_ntby_qty"
        out = []
        for i, (r, f) in enumerate(ok[:top_n]):
            row = _to_kis_row(r, i + 1)
            row[field] = str(f[who])
            row["source"] = "naver(거래대금 상위 %d종목 내)" % _INVESTOR_POOL
            row["flow_bizdate"] = f["bizdate"]
            out.append(row)
        if out:
            logger.info("[순위폴백] KIS 수급 순위 비어 네이버로 대체: %s %s (%d건)", who, mkt, len(out))
        return out
    except Exception as e:
        logger.warning("[순위폴백] 수급 대체 실패 (%s): %s", who, e)
        return []
