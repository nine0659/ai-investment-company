"""
clients/consensus_client.py

yfinance를 통해 애널리스트 목표주가 컨센서스를 수집한다.
(네이버 금융 스크래핑 방식에서 변경 — company_list.naver는 목표주가 컬럼이 없음)
"""
import logging
import time as _time_module

import yfinance as yf

logger = logging.getLogger(__name__)

# 컨센서스 신뢰도 최소 애널리스트 수
_MIN_TRUST_COUNT = 3

# KOSPI 종목코드 → yfinance 접미사 .KS, KOSDAQ → .KQ
# 기본은 .KS 시도 후 실패하면 .KQ 시도
_MARKET_SUFFIX = {
    "KOSPI": ".KS",
    "KOSDAQ": ".KQ",
}


def _to_yf_ticker(code: str, market: str | None = None) -> str:
    suffix = _MARKET_SUFFIX.get(market, ".KS") if market else ".KS"
    return f"{code}{suffix}"


def _determine_opinion(rec_summary) -> str:
    """recommendations_summary 첫 행으로 투자의견 결정."""
    if rec_summary is None or rec_summary.empty:
        return ""
    row = rec_summary.iloc[0]
    strong_buy = int(row.get("strongBuy", 0))
    buy        = int(row.get("buy", 0))
    hold       = int(row.get("hold", 0))
    sell       = int(row.get("sell", 0))
    strong_sell = int(row.get("strongSell", 0))

    positive = strong_buy + buy
    negative = sell + strong_sell
    if positive > hold + negative:
        return "매수"
    if hold >= positive + negative:
        return "중립"
    return "매도"


def fetch_analyst_targets(
    code: str,
    market: str | None = None,
) -> dict:
    """
    yfinance를 통해 단일 종목의 애널리스트 컨센서스 목표주가를 조회한다.

    Parameters
    ----------
    code : str
        6자리 종목코드 (예: "005930")
    market : str | None
        "KOSPI" 또는 "KOSDAQ". None이면 KOSPI(.KS) 먼저 시도.

    Returns
    -------
    dict
        {
            "code": str,
            "avg_target": int,
            "median_target": int,
            "analyst_count": int,
            "max_target": int,
            "min_target": int,
            "consensus_opinion": str,
            "low_confidence": bool,
        }
        데이터 없으면 빈 dict 반환.
    """
    suffixes = [_MARKET_SUFFIX.get(market, ".KS")] if market else [".KS", ".KQ"]

    for suffix in suffixes:
        ticker_str = f"{code}{suffix}"
        try:
            t = yf.Ticker(ticker_str)
            apt = t.analyst_price_targets  # dict: mean/median/high/low/current
            if not apt or not apt.get("mean"):
                continue

            mean_target   = apt.get("mean", 0)
            median_target = apt.get("median", 0)
            max_target    = apt.get("high", 0)
            min_target    = apt.get("low", 0)

            if not mean_target or mean_target < 1000:
                continue

            rec_summary = t.recommendations_summary
            opinion = _determine_opinion(rec_summary)

            # 애널리스트 수 — recommendations_summary 현재 월 합산
            analyst_count = 0
            if rec_summary is not None and not rec_summary.empty:
                row = rec_summary.iloc[0]
                analyst_count = int(
                    row.get("strongBuy", 0) + row.get("buy", 0)
                    + row.get("hold", 0) + row.get("sell", 0)
                    + row.get("strongSell", 0)
                )

            result = {
                "code": code,
                "avg_target": round(mean_target),
                "median_target": round(median_target) if median_target else round(mean_target),
                "analyst_count": analyst_count,
                "max_target": round(max_target) if max_target else 0,
                "min_target": round(min_target) if min_target else 0,
                "consensus_opinion": opinion,
                "low_confidence": analyst_count < _MIN_TRUST_COUNT,
            }
            logger.info(
                "[컨센서스] %s(%s) 수집 완료: 애널 %d명, 평균목표 %s원, 의견=%s",
                code, ticker_str, analyst_count, f"{round(mean_target):,}", opinion,
            )
            return result

        except Exception as e:
            logger.debug("[컨센서스] %s%s 조회 실패: %s", code, suffix, e)
            continue

    logger.debug("[컨센서스] %s 유효 목표주가 없음", code)
    return {}


def fetch_consensus_batch(
    codes: list[str],
    delay: float = 0.3,
    market_map: dict[str, str] | None = None,
) -> dict[str, dict]:
    """
    여러 종목의 컨센서스를 순차적으로 수집한다.

    Parameters
    ----------
    codes : list[str]
        종목코드 목록
    delay : float
        요청 간 대기 시간(초) — yfinance rate limit 준수
    market_map : dict[str, str] | None
        {code: "KOSPI"/"KOSDAQ"} 시장 정보. 없으면 KOSPI 우선 시도.

    Returns
    -------
    dict[str, dict]
        {code: consensus_dict, ...}  수집 실패 종목은 제외.
    """
    market_map = market_map or {}
    result: dict[str, dict] = {}
    for i, code in enumerate(codes):
        market = market_map.get(code)
        data = fetch_analyst_targets(code, market=market)
        if data:
            result[code] = data
        if i < len(codes) - 1 and delay > 0:
            _time_module.sleep(delay)
    logger.info("[컨센서스] 배치 수집 완료: %d/%d 성공", len(result), len(codes))
    return result
