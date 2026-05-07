"""
미국 나스닥/S&P500 거래량 급증·등락률 상위 종목 수집
Yahoo Finance(yfinance) 기반
"""
import logging
import yfinance as yf

logger = logging.getLogger(__name__)

# 한국 시장 영향도가 높은 미국 주요 종목 감시 목록
US_WATCHLIST: dict[str, str] = {
    # 반도체
    "NVDA":  "엔비디아",
    "AMD":   "AMD",
    "INTC":  "인텔",
    "QCOM":  "퀄컴",
    "AMAT":  "어플라이드머티리얼즈",
    "MU":    "마이크론",
    "AVGO":  "브로드컴",
    "ARM":   "ARM홀딩스",
    "TSM":   "TSMC",
    "LRCX":  "램리서치",
    "KLAC":  "KLA",
    # 빅테크·플랫폼
    "AAPL":  "애플",
    "MSFT":  "마이크로소프트",
    "GOOGL": "알파벳",
    "META":  "메타",
    "AMZN":  "아마존",
    "NFLX":  "넷플릭스",
    # EV·배터리
    "TSLA":  "테슬라",
    "RIVN":  "리비안",
    # 금융·기타
    "JPM":   "JP모건",
    "GS":    "골드만삭스",
}

# 미국 종목 → 연관 한국 종목 매핑 (코드, 이름, 영향 유형)
US_TO_KR_MAP: dict[str, list[dict]] = {
    "NVDA": [
        {"code": "000660", "name": "SK하이닉스",  "reason": "HBM 최대 공급사"},
        {"code": "005930", "name": "삼성전자",    "reason": "HBM·파운드리 경쟁·수혜"},
        {"code": "042700", "name": "한미반도체",  "reason": "HBM 패키징 장비 공급"},
    ],
    "AMD": [
        {"code": "005930", "name": "삼성전자",  "reason": "파운드리·DRAM 공급사"},
        {"code": "000660", "name": "SK하이닉스", "reason": "DDR5·HBM 공급사"},
    ],
    "INTC": [
        {"code": "005930", "name": "삼성전자", "reason": "파운드리 경쟁·DRAM 공급"},
    ],
    "QCOM": [
        {"code": "005930", "name": "삼성전자", "reason": "파운드리 수주·AP 공급"},
        {"code": "009150", "name": "삼성전기", "reason": "스마트폰 부품 연동"},
    ],
    "AMAT": [
        {"code": "240810", "name": "원익IPS",    "reason": "반도체 장비 동종업"},
        {"code": "005930", "name": "삼성전자",   "reason": "장비 수요 연동"},
        {"code": "000660", "name": "SK하이닉스", "reason": "장비 수요 연동"},
    ],
    "LRCX": [
        {"code": "240810", "name": "원익IPS",    "reason": "식각·증착 장비 동종업"},
        {"code": "005930", "name": "삼성전자",   "reason": "장비 수요 연동"},
    ],
    "KLAC": [
        {"code": "240810", "name": "원익IPS",    "reason": "계측·검사 장비 동종업"},
    ],
    "MU": [
        {"code": "005930", "name": "삼성전자",   "reason": "DRAM 경쟁사 실적 지표"},
        {"code": "000660", "name": "SK하이닉스", "reason": "DRAM 업황 동행"},
    ],
    "AVGO": [
        {"code": "000660", "name": "SK하이닉스", "reason": "AI 가속기 메모리 수요"},
        {"code": "005930", "name": "삼성전자",   "reason": "AI 반도체 공급망"},
    ],
    "ARM": [
        {"code": "005930", "name": "삼성전자",   "reason": "ARM 아키텍처 라이선스 연동"},
        {"code": "000660", "name": "SK하이닉스", "reason": "모바일 AP 메모리 수요"},
    ],
    "TSM": [
        {"code": "005930", "name": "삼성전자",   "reason": "파운드리 직접 경쟁사"},
        {"code": "000660", "name": "SK하이닉스", "reason": "파운드리 업황 동행"},
    ],
    "AAPL": [
        {"code": "011070", "name": "LG이노텍",  "reason": "카메라 모듈 최대 공급사"},
        {"code": "009150", "name": "삼성전기",  "reason": "MLCC·기판 공급사"},
        {"code": "000660", "name": "SK하이닉스", "reason": "모바일 NAND 공급사"},
    ],
    "TSLA": [
        {"code": "373220", "name": "LG에너지솔루션", "reason": "배터리 셀 공급사"},
        {"code": "006400", "name": "삼성SDI",       "reason": "배터리 셀 공급사"},
        {"code": "003670", "name": "포스코퓨처엠",  "reason": "양극재 공급사"},
        {"code": "247540", "name": "에코프로비엠",  "reason": "양극재 공급사"},
    ],
    "RIVN": [
        {"code": "373220", "name": "LG에너지솔루션", "reason": "배터리 단독 공급사"},
        {"code": "006400", "name": "삼성SDI",        "reason": "배터리 공급 기대"},
    ],
}


def fetch_us_top_movers(n: int = 5) -> list[dict]:
    """
    나스닥/S&P500 거래량 급증·등락률 상위 종목 TOP N 반환.
    score = |change_pct| * 0.6 + vol_ratio * 0.4
    """
    symbols = list(US_WATCHLIST.keys())
    movers: list[dict] = []

    try:
        bundle = yf.Tickers(" ".join(symbols))
        for ticker, kor_name in US_WATCHLIST.items():
            try:
                t = bundle.tickers.get(ticker) or yf.Ticker(ticker)
                hist = t.history(period="10d", interval="1d")
                if len(hist) < 2:
                    continue

                latest  = hist.iloc[-1]
                prev    = hist.iloc[-2]
                close   = float(latest["Close"])
                prev_c  = float(prev["Close"])
                chg_pct = (close - prev_c) / prev_c * 100 if prev_c else 0.0
                vol     = int(latest.get("Volume", 0))
                avg_vol = int(hist["Volume"].mean())
                vol_ratio = vol / avg_vol if avg_vol > 0 else 1.0

                movers.append({
                    "ticker":     ticker,
                    "name":       kor_name,
                    "close":      round(close, 2),
                    "change_pct": round(chg_pct, 2),
                    "volume":     vol,
                    "vol_ratio":  round(vol_ratio, 2),
                    "score":      round(abs(chg_pct) * 0.6 + vol_ratio * 0.4, 2),
                })
            except Exception as e:
                logger.debug("종목 파싱 실패 (%s): %s", ticker, e)

    except Exception as e:
        logger.error("미국 상위 종목 수집 실패: %s", e)

    movers.sort(key=lambda x: x["score"], reverse=True)
    return movers[:n]


def map_to_korean_stocks(movers: list[dict]) -> list[dict]:
    """
    미국 상위 종목 → 연관 한국 종목 매핑.
    반환: [{"us_ticker", "us_name", "change_pct", "kr_stocks": [...]}]
    """
    result = []
    seen_kr: set[str] = set()  # 중복 한국 종목 방지

    for m in movers:
        kr_stocks = US_TO_KR_MAP.get(m["ticker"], [])
        if not kr_stocks:
            continue
        # 이미 등장한 한국 종목 제외하고 신규만 포함
        unique_kr = [k for k in kr_stocks if k["code"] not in seen_kr]
        for k in unique_kr:
            seen_kr.add(k["code"])
        result.append({
            "us_ticker":  m["ticker"],
            "us_name":    m["name"],
            "change_pct": m["change_pct"],
            "vol_ratio":  m["vol_ratio"],
            "kr_stocks":  unique_kr,
        })

    return result


def format_us_impact_for_prompt(movers: list[dict]) -> str:
    """미국 상위 종목 + 한국 연관 종목을 프롬프트용 텍스트로 변환"""
    mapped = map_to_korean_stocks(movers)
    if not mapped:
        return "미국 시장 특이 종목 없음"

    lines = []
    for item in mapped:
        direction = "▲" if item["change_pct"] >= 0 else "▼"
        lines.append(
            f"{direction} {item['us_name']}({item['us_ticker']}) "
            f"{item['change_pct']:+.2f}%  거래량 {item['vol_ratio']:.1f}배"
        )
        for kr in item["kr_stocks"]:
            lines.append(f"   → {kr['name']}({kr['code']}) | {kr['reason']}")
    return "\n".join(lines)
