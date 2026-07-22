import logging
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf
from utils.retry import with_retry

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")


# 2026-07-22: 무검증으로 yfinance 이상치(예: 1080.36/-7.18%처럼 인접 두 봉이
# 어긋난 값)가 그대로 "KOSPI200선물"로 표시돼 브리핑을 오염시킨 사고 이후 추가.
# 단일세션 20% 초과 변동은 2026-07 최악의 크래시(-8%대)도 넘는 수준이라 데이터
# 오류로 간주해 제외한다 (실제 크래시를 오탐 차단하려는 게 아니라 명백한 이상치만 컷).
_KS200_MAX_DAILY_CHANGE = 20.0


def fetch_kospi200_futures() -> dict:
    """KOSPI200 지수(전일종가)를 야간선물 방향 판단 기준값으로 반환.

    야간선물(KRX 18:00~05:00 세션)은 무료 공개 API로 직접 수집 불가.
    KOSPI200 지수 전일종가(^KS200)를 기준값으로 사용하며,
    실제 야간선물 방향은 미국 선물(ES=F, NQ=F) 오버나잇 변화로 추정.
    이름과 달리 "선물"이 아니라 지수 종가 기준이므로 오늘 갭 방향의 선행
    지표는 아니다 — 호출부에서 "전일 종가 등락"으로 라벨링할 것.
    반환: {close, change, change_pct, high, low, symbol, name, is_index}
    """
    try:
        hist = yf.Ticker("^KS200").history(period="5d", interval="1d")
        if len(hist) < 2:
            return {}
        close      = float(hist.iloc[-1]["Close"])
        prev_close = float(hist.iloc[-2]["Close"])
        if close <= 0 or prev_close <= 0:
            logger.warning("KOSPI200 지수 값 비정상(0 이하) — 수집 제외")
            return {}
        change     = close - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0
        if abs(change_pct) > _KS200_MAX_DAILY_CHANGE:
            logger.warning(
                "KOSPI200 지수 변동률 비정상: %.2f%% (close=%.2f, prev=%.2f) — 수집 제외",
                change_pct, close, prev_close,
            )
            return {}
        return {
            "close":      round(close, 2),
            "change":     round(change, 2),
            "change_pct": round(change_pct, 2),
            "high":       round(float(hist.iloc[-1]["High"]), 2),
            "low":        round(float(hist.iloc[-1]["Low"]), 2),
            "symbol":     "^KS200",
            "name":       "KOSPI200지수(전일종가)",
            "is_index":   True,
            "data_date":  hist.index[-1].strftime("%Y-%m-%d"),
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
    "us3m":            "^IRX",   # 13주(3개월) T-Bill — 수익률 곡선 단기 기준
    # 원자재
    "gold":            "GC=F",
    "oil_wti":         "CL=F",
    "copper":          "HG=F",      # 구리 선물 — 글로벌 경기 선행 지표 (Dr. Copper)
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
    # 글로벌 자금 흐름 ETF — 외국인 수급 선행 지표
    "ewy":             "EWY",       # iShares MSCI South Korea ETF (한국 외국인 선행)
    "eem":             "EEM",       # iShares MSCI Emerging Markets ETF (신흥국 자금)
    "lit":             "LIT",       # Global X Lithium & Battery Tech ETF (2차전지 섹터)
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
            "data_date":  hist.index[-1].strftime("%Y-%m-%d"),
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
    """KOSPI·KOSDAQ 장중 실시간 현재 지수 수집.
    전일 종가를 일봉(5d)에서 가져와 변동률을 정확히 계산.
    반환: {"kospi": {current, prev_close, change_pct}, "kosdaq": {...}}
    """
    # yfinance가 간헐적으로 오염된 값을 반환함 — 유효 범위로 필터
    # 2026-07-22: KOSPI 상단을 6,000으로 두면 2026-07 실제 지수(6,500~9,000대,
    # 6월 고점 9,114)가 매번 "비정상"으로 걸려 실시간 값이 항상 일봉 종가로
    # 대체됐다 — "실시간" 라벨을 달고 사실상 매번 지난 종가만 나간 원인.
    # 범위는 이상치(0, 음수, 자릿수 오류) 컷용이지 역사적 상한 고정용이 아니다.
    _VALID_RANGE = {
        "^KS11": (1_000, 15_000),  # KOSPI — 이상치 컷 (상한을 지수 성장 여유 있게)
        "^KQ11": (300,   5_000),   # KOSDAQ — 이상치 컷
    }
    # 한국 서킷브레이커 ±8% → 그 이상은 yfinance 오류 데이터
    _MAX_DAILY_CHANGE = 10.0

    result: dict = {}
    key_map = {"^KS11": "kospi", "^KQ11": "kosdaq"}
    for sym, key in key_map.items():
        lo, hi = _VALID_RANGE[sym]
        try:
            # 전일 종가: 일봉 5일치에서 안정적으로 확보
            daily = yf.Ticker(sym).history(period="5d", interval="1d")
            if len(daily) < 2:
                continue
            prev_close = float(daily.iloc[-2]["Close"])  # 전일 종가

            # 장중 현재가: 5분봉 최근 1일
            intra = yf.Ticker(sym).history(period="1d", interval="5m")
            raw_current = float(intra.iloc[-1]["Close"]) if not intra.empty else float(daily.iloc[-1]["Close"])

            # 1차 검증: 지수값 자체가 유효 범위 밖이면 일봉 최신 종가로 대체
            if not (lo < raw_current < hi):
                logger.warning(
                    "한국 지수 실시간 값 비정상 (%s): %.2f — 일봉 종가로 대체", sym, raw_current
                )
                raw_current = float(daily.iloc[-1]["Close"])

            chg_pct = (raw_current - prev_close) / prev_close * 100 if prev_close else 0

            # 2차 검증: 일간 변동률이 서킷브레이커 한계 초과이면 오류 데이터로 간주해 스킵
            if abs(chg_pct) > _MAX_DAILY_CHANGE:
                logger.warning(
                    "한국 지수 변동률 비정상 (%s): %.2f%% — 경보 방지를 위해 해당 데이터 제외", sym, chg_pct
                )
                continue

            result[key] = {
                "current":    round(raw_current, 2),
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
    """한국 개별 종목 기술적 지표 계산.
    symbol: yfinance 심볼 (예: "000660.KS" 또는 "042700.KQ")
    반환: {rsi14, ma5, ma20, above_ma20, close, bb_upper, bb_lower, bb_pct,
           vol_ratio, golden_cross, dead_cross}
    """
    try:
        hist = yf.Ticker(symbol).history(period="3mo", interval="1d")
        if len(hist) < 21:
            return {}
        closes  = hist["Close"]
        volumes = hist["Volume"]
        close   = float(closes.iloc[-1])
        ma5     = float(closes.iloc[-5:].mean())
        ma20    = float(closes.iloc[-20:].mean())

        # RSI 14
        delta  = closes.diff()
        gain   = delta.clip(lower=0)
        loss   = (-delta).clip(lower=0)
        avg_g  = gain.ewm(com=13, adjust=False).mean()
        avg_l  = loss.ewm(com=13, adjust=False).mean()
        rs     = avg_g / avg_l.replace(0, float("nan"))
        rsi    = 100 - 100 / (1 + rs)
        rsi14  = float(rsi.iloc[-1])

        # 볼린저밴드 (20일, 2σ)
        std20    = float(closes.iloc[-20:].std())
        bb_upper = round(ma20 + 2 * std20, 0)
        bb_lower = round(ma20 - 2 * std20, 0)
        bb_range = bb_upper - bb_lower
        bb_pct   = round((close - bb_lower) / bb_range * 100, 1) if bb_range > 0 else 50.0

        # 거래량: 오늘 vs 5일 평균 비율
        vol_today = float(volumes.iloc[-1]) if len(volumes) >= 1 else 0
        vol_ma5   = float(volumes.iloc[-6:-1].mean()) if len(volumes) >= 6 else vol_today
        vol_ratio = round(vol_today / vol_ma5 * 100, 0) if vol_ma5 > 0 else 100.0

        # MA5/MA20 골든크로스·데드크로스 (전일 대비)
        prev_ma5  = float(closes.iloc[-6:-1].mean()) if len(closes) >= 6 else ma5
        prev_ma20 = float(closes.iloc[-21:-1].mean()) if len(closes) >= 21 else ma20
        golden_cross = bool(prev_ma5 < prev_ma20 and ma5 >= ma20)
        dead_cross   = bool(prev_ma5 > prev_ma20 and ma5 <= ma20)

        return {
            "close":        round(close, 0),
            "ma5":          round(ma5, 0),
            "ma20":         round(ma20, 0),
            "rsi14":        round(rsi14, 1),
            "above_ma20":   close > ma20,
            "bb_upper":     bb_upper,
            "bb_lower":     bb_lower,
            "bb_pct":       bb_pct,        # 0=하단, 100=상단
            "vol_ratio":    vol_ratio,     # 5일 평균 대비 오늘 거래량 %
            "golden_cross": golden_cross,  # MA5가 MA20 상향돌파
            "dead_cross":   dead_cross,    # MA5가 MA20 하향돌파
        }
    except Exception as e:
        logger.debug("기술적 지표 계산 실패 (%s): %s", symbol, e)
        return {}


@with_retry(max_attempts=3, base_delay=2.0, exceptions=(Exception,))
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


# ── 핵심 지표 — 신선도 점검 대상
_FRESHNESS_KEYS = [
    "sp500_futures", "nasdaq_futures", "kospi", "usd_krw", "vix", "us10y",
]


def _biz_days_ago(data_date: date, today: date) -> int:
    """data_date가 today 기준 몇 거래일(공휴일 제외 평일) 전인지 반환."""
    if data_date >= today:
        return 0
    # 한국 공휴일 로드 (가능하면)
    kr_holidays: set = set()
    try:
        import holidays
        kr = holidays.KR(years={data_date.year, today.year})
        kr_holidays = set(kr.keys())
    except ImportError:
        pass

    count = 0
    d = today
    while d > data_date:
        d -= timedelta(days=1)
        if d.weekday() < 5 and d not in kr_holidays:
            count += 1
    return count


def check_data_freshness(raw_market_data: dict) -> dict:
    """수집된 시장 데이터의 신선도를 점검.

    반환:
      latest_date  — 수집된 데이터 중 가장 최신 날짜 (str)
      biz_days_old — 오늘(KST) 기준 영업일 수 (0=오늘, 1=전일 정상, 2+=비정상)
      stale_keys   — 2거래일 이상 오래된 핵심 지표 목록
      label        — 브리핑 첫 줄에 쓸 날짜 레이블 (예: "2026-05-14(전일)")
      warning      — 이상 감지 시 경고 문자열, 정상이면 ""
    """
    today = date.today()  # 로컬(KST) 오늘 날짜

    dates_found: list[date] = []
    stale_keys: list[str] = []

    for key in _FRESHNESS_KEYS:
        d = raw_market_data.get(key, {})
        dd = d.get("data_date")
        if not dd:
            continue
        try:
            parsed = date.fromisoformat(dd)
            dates_found.append(parsed)
            age = _biz_days_ago(parsed, today)
            if age >= 3:
                stale_keys.append(f"{key}({dd})")
        except ValueError:
            pass

    if not dates_found:
        return {
            "latest_date":  "",
            "biz_days_old": -1,
            "stale_keys":   [],
            "label":        "날짜 미확인",
            "warning":      "⚠️ 시장 데이터 날짜를 확인할 수 없습니다.",
        }

    latest = max(dates_found)
    age    = _biz_days_ago(latest, today)

    if age == 0:
        label = f"{latest}(오늘)"
    elif age == 1:
        label = f"{latest}(전일 — 정상)"
    else:
        label = f"{latest}({age}거래일 전 ⚠️)"

    warning = ""
    if stale_keys:
        warning = (
            f"⚠️ 데이터 신선도 경고: {', '.join(stale_keys)} 가 "
            f"3거래일 이상 오래됨 — yfinance API 오류 또는 연속 공휴일 가능성"
        )

    return {
        "latest_date":  str(latest),
        "biz_days_old": age,
        "stale_keys":   stale_keys,
        "label":        label,
        "warning":      warning,
    }
