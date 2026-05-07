import logging
import yfinance as yf

logger = logging.getLogger(__name__)

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
        hist = ticker.history(period="2d", interval="1d")
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
    return result
