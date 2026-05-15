import logging

import yfinance as yf

logger = logging.getLogger(__name__)


def fetch_kospi200_futures() -> dict:
    """KOSPI200 지수(전일종가)를 야간선물 방향 판단 기준값으로 반환.

    야간선물(KRX 18:00~05:00 세션)은 무료 공개 API로 직접 수집 불가.
    KOSPI200 지수 전일종가(^KS200)를 기준값으로 사용하며,
    실제 야간선물 방향은 미국 선물(ES=F, NQ=F) 오버나잇 변화로 추정.
    반환: {close, change, change_pct, high, low, symbol, name, is_index}
    """
    try:
        hist = yf.Ticker("^KS200").history(period="5d", interval="1d")
        if len(hist) < 2:
            return {}
        close      = float(hist.iloc[-1]["Close"])
        prev_close = float(hist.iloc[-2]["Close"])
        change     = close - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0
        return {
            "close":      round(close, 2),
            "change":     round(change, 2),
            "change_pct": round(change_pct, 2),
            "high":       round(float(hist.iloc[-1]["High"]), 2),
            "low":        round(float(hist.iloc[-1]["Low"]), 2),
            "symbol":     "^KS200",
            "name":       "KOSPI200지수(전일종가)",
            "is_index":   True,
        }
    except Exception as e:
        logger.debug("KOSPI200 지수 조회 실패: %s", e)
    return {}

TICKERS: dict[str, str] = {
    # 미국 선물
    "sp500_futures":   "ES=F",
    "nasdaq_futures":  "NQ=F",
    "dow_futures":     "YM=F",
    # 환율
    "usd_krw":         "USDKRW=X",
    "dxy":             "DX-Y.NYB",
    # 금리
    "us10y":           "^TNX",
    "us2y":            "^IRX",
    # 원자재
    "gold":            "GC=F",
    "oil_wti":         "CL=F",
    # 변동성
    "vix":             "^VIX",
    # 한국
    "kospi":           "^KS11",
    "kosdaq":          "^KQ11",
    # 아시아
    "nikkei":          "^N225",
    "hang_seng":       "^HSI",
    "shanghai":        "000001.SS",
    # 미국 현물
    "sp500":           "^GSPC",
    "nasdaq":          "^IXIC",
    "dow":             "^DJI",
    # 반도체
    "sox":             "^SOX",
    "nvda":            "NVDA",
    "tsmc":            "TSM",
}


def _parse(ticker: yf.Ticker) -> dict:
    try:
        # period="5d" → 휴일/주말 있어도 최근 2영업일 데이터 안전 확보
        hist = ticker.history(period="5d", interval="1d")
        if hist.empty:
            return {}
        latest = hist.iloc[-1]
        prev   = hist.iloc[-2] if len(hist) > 1 else latest
        close  = float(latest["Close"])
        prev_c = float(prev["Close"])
        chg    = close - prev_c
        chg_pct = (chg / prev_c * 100) if prev_c else 0
        return {
            "close":      round(close, 4),
            "change":     round(chg, 4),
            "change_pct": round(chg_pct, 2),
            "high":       round(float(latest["High"]), 4),
            "low":        round(float(latest["Low"]), 4),
            "volume":     int(latest.get("Volume", 0)),
        }
    except Exception as e:
        logger.debug("ticker parse error: %s", e)
        return {}


_FUTURES_REALTIME = {
    "ES=F":  "S&P500선물",
    "NQ=F":  "나스닥선물",
}

_INDEX_REALTIME = {
    "^KS11": "KOSPI",
    "^KQ11": "KOSDAQ",
}


def fetch_kr_index_realtime() -> dict:
    """KOSPI·KOSDAQ 장중 실시간 현재 지수 수집 (5분봉 최근 1일).
    intra 브리핑(10:00, 13:00 KST)에서 현재 지수 수준 파악용.
    반환: {"kospi": {current, prev_close, change_pct}, "kosdaq": {...}}
    """
    result: dict = {}
    key_map = {"^KS11": "kospi", "^KQ11": "kosdaq"}
    for sym, key in key_map.items():
        try:
            hist = yf.Ticker(sym).history(period="1d", interval="5m")
            if hist.empty:
                continue
            current    = float(hist.iloc[-1]["Close"])
            prev_close = float(hist.iloc[0]["Open"])
            chg_pct    = (current - prev_close) / prev_close * 100 if prev_close else 0
            result[key] = {
                "current":    round(current, 2),
                "prev_close": round(prev_close, 2),
                "change_pct": round(chg_pct, 2),
            }
        except Exception as e:
            logger.debug("한국 지수 실시간 조회 실패 (%s): %s", sym, e)
    return result


def fetch_futures_realtime() -> dict:
    """S&P500·나스닥 선물 오버나잇 실시간 수준 수집 (30분봉 최근 1일).
    장전 브리핑(08:20 KST)에서 야간 선물 방향 파악용.
    반환: {sym: {current, prev_close, change_pct, label}}
    """
    result: dict = {}
    for sym, label in _FUTURES_REALTIME.items():
        try:
            hist = yf.Ticker(sym).history(period="1d", interval="30m")
            if hist.empty or len(hist) < 2:
                continue
            current    = float(hist.iloc[-1]["Close"])
            prev_close = float(hist.iloc[0]["Open"])
            chg_pct    = (current - prev_close) / prev_close * 100 if prev_close else 0
            result[sym] = {
                "current":    round(current, 2),
                "prev_close": round(prev_close, 2),
                "change_pct": round(chg_pct, 2),
                "label":      label,
            }
        except Exception as e:
            logger.debug("선물 실시간 조회 실패 (%s): %s", sym, e)
    return result


def fetch_kr_stock_technicals(symbol: str) -> dict:
    """한국 개별 종목 기술적 지표 계산 (RSI14, MA5, MA20, 현재가 대비 MA 위치).
    symbol: yfinance 심볼 (예: "000660.KS" 또는 "042700.KQ")
    반환: {rsi14, ma5, ma20, above_ma20, close}
    """
    try:
        hist = yf.Ticker(symbol).history(period="3mo", interval="1d")
        if len(hist) < 20:
            return {}
        closes = hist["Close"]
        close  = float(closes.iloc[-1])
        ma5    = float(closes.iloc[-5:].mean())
        ma20   = float(closes.iloc[-20:].mean())

        # RSI 14
        delta  = closes.diff()
        gain   = delta.clip(lower=0)
        loss   = (-delta).clip(lower=0)
        avg_g  = gain.ewm(com=13, adjust=False).mean()
        avg_l  = loss.ewm(com=13, adjust=False).mean()
        rs     = avg_g / avg_l.replace(0, float("nan"))
        rsi    = 100 - 100 / (1 + rs)
        rsi14  = float(rsi.iloc[-1])

        return {
            "close":      round(close, 0),
            "ma5":        round(ma5, 0),
            "ma20":       round(ma20, 0),
            "rsi14":      round(rsi14, 1),
            "above_ma20": close > ma20,
        }
    except Exception as e:
        logger.debug("기술적 지표 계산 실패 (%s): %s", symbol, e)
        return {}


def fetch_global_market_data() -> dict:
    result: dict = {}
    symbols = list(TICKERS.values())
    try:
        bundle = yf.Tickers(" ".join(symbols))
        for key, symbol in TICKERS.items():
            t = bundle.tickers.get(symbol) or yf.Ticker(symbol)
            data = _parse(t)
            if data:
                result[key] = {**data, "symbol": symbol}
    except Exception as e:
        logger.error("시장 데이터 수집 실패: %s", e)
        # 개별 재시도
        for key, symbol in TICKERS.items():
            if key not in result:
                try:
                    data = _parse(yf.Ticker(symbol))
                    if data:
                        result[key] = {**data, "symbol": symbol}
                except Exception:
                    pass

    # 미국 선물 실시간 데이터 병합 (장전 브리핑 오버나잇 방향 반영)
    try:
        futures_rt = fetch_futures_realtime()
        for sym, d in futures_rt.items():
            key = "sp500_futures" if sym == "ES=F" else "nasdaq_futures"
            if key in result:
                result[key]["realtime_pct"] = d["change_pct"]
                result[key]["realtime_current"] = d["current"]
            else:
                result[key] = {
                    "close": d["current"], "change_pct": d["change_pct"],
                    "high": d["current"], "low": d["current"], "volume": 0,
                    "symbol": sym, "realtime_pct": d["change_pct"],
                }
    except Exception as e:
        logger.debug("미국 선물 실시간 병합 실패: %s", e)

    # KOSPI200 야간선물 데이터 병합 (한국 시장 직접 선행 신호)
    try:
        k200 = fetch_kospi200_futures()
        if k200:
            result["kospi200_futures"] = k200
            logger.debug("KOSPI200 야간선물 수집 완료: %s", k200)
    except Exception as e:
        logger.debug("KOSPI200 야간선물 병합 실패: %s", e)

    return result
