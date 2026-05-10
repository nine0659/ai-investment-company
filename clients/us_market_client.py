"""
미국 시장 섹터별 ETF 등락률 + 52주 신고가 종목 수집
Yahoo Finance(yfinance) 기반
"""
import logging
import yfinance as yf

logger = logging.getLogger(__name__)

# 미국 섹터 대표 ETF
SECTOR_ETFS: dict[str, str] = {
    "반도체":   "SOXX",
    "기술/IT":  "XLK",
    "전기차":   "DRIV",
    "방산":     "XAR",
    "바이오":   "IBB",
    "금융":     "XLF",
    "에너지":   "XLE",
    "로봇/AI":  "ROBO",
    "자동차":   "CARZ",
    "소프트웨어": "IGV",
}

# 52주 신고가 체크용 확장 감시 목록 (us_stock_client 의 WATCHLIST + 추가)
_52W_WATCHLIST: dict[str, str] = {
    "NVDA": "엔비디아", "AMD": "AMD", "INTC": "인텔", "QCOM": "퀄컴",
    "AMAT": "어플라이드머티리얼즈", "MU": "마이크론", "AVGO": "브로드컴",
    "ARM": "ARM홀딩스", "TSM": "TSMC", "LRCX": "램리서치", "KLAC": "KLA",
    "AAPL": "애플", "MSFT": "마이크로소프트", "GOOGL": "알파벳",
    "META": "메타", "AMZN": "아마존",
    "TSLA": "테슬라", "RIVN": "리비안", "GM": "GM", "F": "포드",
    "LMT": "록히드마틴", "RTX": "레이시온", "NOC": "노스롭그루만",
    "JPM": "JP모건", "GS": "골드만삭스", "BAC": "뱅크오브아메리카",
    "XOM": "엑슨모빌", "CVX": "셰브론",
    "PLTR": "팔란티어", "SMCI": "슈퍼마이크로", "CRM": "세일즈포스",
}


def fetch_us_sectors() -> dict[str, dict]:
    """미국 섹터 ETF 전일 등락률 수집.
    반환: {"반도체": {"symbol": "SOXX", "close": 250.1, "change_pct": 2.3, "volume": 1000000}, ...}
    """
    result: dict[str, dict] = {}
    symbols = list(SECTOR_ETFS.values())
    try:
        bundle = yf.Tickers(" ".join(symbols))
        for sector, symbol in SECTOR_ETFS.items():
            try:
                t = bundle.tickers.get(symbol) or yf.Ticker(symbol)
                hist = t.history(period="3d", interval="1d")
                if len(hist) < 2:
                    continue
                latest = hist.iloc[-1]
                prev   = hist.iloc[-2]
                close  = float(latest["Close"])
                prev_c = float(prev["Close"])
                chg_pct = (close - prev_c) / prev_c * 100 if prev_c else 0.0
                result[sector] = {
                    "symbol":     symbol,
                    "close":      round(close, 2),
                    "change_pct": round(chg_pct, 2),
                    "volume":     int(latest.get("Volume", 0)),
                }
            except Exception as e:
                logger.debug("섹터 ETF 파싱 실패 (%s/%s): %s", sector, symbol, e)
    except Exception as e:
        logger.error("US 섹터 데이터 수집 실패: %s", e)
        # 개별 재시도
        for sector, symbol in SECTOR_ETFS.items():
            if sector not in result:
                try:
                    t = yf.Ticker(symbol)
                    hist = t.history(period="3d", interval="1d")
                    if len(hist) >= 2:
                        latest = hist.iloc[-1]
                        prev   = hist.iloc[-2]
                        close  = float(latest["Close"])
                        prev_c = float(prev["Close"])
                        chg_pct = (close - prev_c) / prev_c * 100 if prev_c else 0.0
                        result[sector] = {
                            "symbol":     symbol,
                            "close":      round(close, 2),
                            "change_pct": round(chg_pct, 2),
                            "volume":     int(latest.get("Volume", 0)),
                        }
                except Exception:
                    pass

    logger.info("[US섹터] %d개 섹터 수집 완료", len(result))
    return result


def fetch_us_52w_highs() -> list[dict]:
    """52주 신고가 근접(98% 이상) 미국 종목 수집."""
    result: list[dict] = []
    symbols = list(_52W_WATCHLIST.keys())
    try:
        bundle = yf.Tickers(" ".join(symbols))
        for ticker, name in _52W_WATCHLIST.items():
            try:
                t = bundle.tickers.get(ticker) or yf.Ticker(ticker)
                hist = t.history(period="252d", interval="1d")
                if len(hist) < 10:
                    continue
                high_52w = float(hist["High"].max())
                latest   = hist.iloc[-1]
                close    = float(latest["Close"])
                prev_c   = float(hist.iloc[-2]["Close"]) if len(hist) > 1 else close
                chg_pct  = (close - prev_c) / prev_c * 100 if prev_c else 0.0
                if high_52w > 0 and close >= high_52w * 0.98:
                    result.append({
                        "ticker":        ticker,
                        "name":          name,
                        "close":         round(close, 2),
                        "high_52w":      round(high_52w, 2),
                        "pct_from_high": round((close / high_52w - 1) * 100, 2),
                        "change_pct":    round(chg_pct, 2),
                    })
            except Exception as e:
                logger.debug("52w 파싱 실패 (%s): %s", ticker, e)
    except Exception as e:
        logger.error("52주 신고가 수집 실패: %s", e)

    result.sort(key=lambda x: x["pct_from_high"], reverse=True)
    logger.info("[US 52w] %d개 신고가 종목 발견", len(result))
    return result
