"""시장 국면 기저율 — '중립/횡보'를 말하기 전에 실제로 이 시장이 얼마나 움직이는지 코드가 계산해 준다 (2026-10-13).

배경: 소급 채점 결과 최근 50거래일 중 KOSPI가 하루 ±1% 이상 움직인 날이 35일이었는데도
브리핑은 '중립'을 반복했고, 10/12(-3.65%) 직전일에도 -2.62% 급락 직후 중립이었다.
LLM은 기저율을 모른 채 서술하므로 (절대원칙 2) 변동성·급변 후 재변동 확률을 코드가 계산해 주입한다.
yfinance 일봉만 사용하며 실패하면 빈 문자열(CEO 입력이 바뀌지 않음).
"""
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

SHOCK_PCT = 2.0         # 전일 이 이상 움직이면 '급변'으로 보고 과거 유사 사례를 조회
MIN_SHOCK_CASES = 5     # 유사 사례가 이보다 적으면 확률을 말하지 않는다
_CACHE: dict = {"ts": 0.0, "closes": {}}
_CACHE_SEC = 6 * 3600


def _changes(closes: dict[str, float]) -> list[tuple[str, float]]:
    out, prev = [], None
    for d in sorted(closes):
        if prev and prev > 0:
            out.append((d, (closes[d] - prev) / prev * 100))
        prev = closes[d]
    return out


def regime_stats(closes: dict[str, float], window: int = 20) -> dict:
    """최근 window거래일 변동성·방향 분포와 급변 후 재변동 기저율. 데이터가 모자라면 {}."""
    ch = _changes(closes)
    if len(ch) < window + 1:
        return {}
    recent = [c for _, c in ch[-window:]]
    last_date, last_chg = ch[-1]
    stats = {
        "last_date": last_date,
        "last_chg": round(last_chg, 2),
        "window": window,
        "avg_abs": round(sum(abs(c) for c in recent) / window, 2),
        "big_days": sum(1 for c in recent if abs(c) >= 1.0),
        "flat_days": sum(1 for c in recent if abs(c) < 0.3),
        "up_days": sum(1 for c in recent if c >= 0.3),
        "down_days": sum(1 for c in recent if c <= -0.3),
        "sum5": round(sum(c for c in [x for _, x in ch[-5:]]), 2),
    }
    # 과거 유사 사례: 전일이 ±SHOCK_PCT 이상이었던 날의 '다음 날' 움직임 (마지막 날은 다음 날이 없어 제외)
    if abs(last_chg) >= SHOCK_PCT:
        same_sign = last_chg > 0
        nxt = [ch[i + 1][1] for i in range(len(ch) - 1)
               if abs(ch[i][1]) >= SHOCK_PCT and (ch[i][1] > 0) == same_sign]
        if len(nxt) >= MIN_SHOCK_CASES:
            stats["shock"] = {
                "n": len(nxt),
                "re_big": round(sum(1 for c in nxt if abs(c) >= 1.0) / len(nxt) * 100),
                "same_dir": round(sum(1 for c in nxt if (c > 0) == same_sign) / len(nxt) * 100),
                "flat": round(sum(1 for c in nxt if abs(c) < 0.3) / len(nxt) * 100),
            }
    return stats


def format_regime(st: dict) -> str:
    if not st:
        return ""
    w = st["window"]
    lines = [
        f"[시장 국면 기저율 — 코드가 KOSPI 일봉으로 계산한 사실. 이 숫자만 인용] 기준일 {st['last_date']}",
        f"전일 KOSPI {st['last_chg']:+.2f}% / 최근 5거래일 누적 {st['sum5']:+.2f}%",
        f"최근 {w}거래일: 평균 하루 변동폭 {st['avg_abs']}%, ±1% 이상 움직인 날 {st['big_days']}일, "
        f"±0.3% 이내 횡보로 끝난 날 {st['flat_days']}일 (상승 {st['up_days']} / 하락 {st['down_days']})",
    ]
    sh = st.get("shock")
    if sh:
        lines.append(
            f"과거 전일 ±{SHOCK_PCT:.0f}% 이상 같은 방향 급변 {sh['n']}건 뒤: 다음 날 다시 ±1% 이상 움직임 {sh['re_big']}%, "
            f"같은 방향 지속 {sh['same_dir']}%, 횡보 {sh['flat']}%")
    if st["flat_days"] <= w * 0.2:
        lines.append(f"→ 최근 {w}일 중 횡보로 끝난 날이 {st['flat_days']}일뿐이다: '중립/횡보'를 기본값으로 쓰지 말고, "
                     "어느 쪽으로 이탈할 가능성이 더 큰지와 그 조건을 시나리오로 서술하라")
    if sh and sh["flat"] < 25:
        lines.append("→ 급변 직후에는 횡보가 드물었다: 전일 급변을 '이미 반영됐다'고 단정하지 말고 재변동 대비를 서술하라")
    return "\n".join(lines)


def fetch_closes(period: str = "2y") -> dict[str, float]:
    """yfinance ^KS11 일봉 (6시간 캐시). 실패 시 빈 dict."""
    now = time.time()
    if _CACHE["closes"] and now - _CACHE["ts"] < _CACHE_SEC:
        return _CACHE["closes"]
    try:
        import yfinance as yf
        h = yf.Ticker("^KS11").history(period=period, interval="1d")
        closes = {i.strftime("%Y-%m-%d"): float(r["Close"]) for i, r in h.iterrows() if r["Close"] == r["Close"]}
        # 장이 끝나기 전의 오늘 봉은 미확정 값이라 제외한다 (KST 15:45 전)
        kst = datetime.now(ZoneInfo("Asia/Seoul"))
        if kst.hour * 60 + kst.minute < 15 * 60 + 45:
            closes.pop(kst.strftime("%Y-%m-%d"), None)
        from clients.market_data_client import fill_missing_index_closes
        closes = fill_missing_index_closes(closes)
        if closes:
            _CACHE.update(ts=now, closes=closes)
        return closes
    except Exception as e:
        logger.debug("[국면] KOSPI 일봉 조회 실패: %s", e)
        return {}


def build_context() -> str:
    """CEO 입력용 국면 기저율 블록. 실패·데이터 부족이면 빈 문자열."""
    try:
        return format_regime(regime_stats(fetch_closes()))
    except Exception as e:
        logger.debug("[국면] 컨텍스트 생성 실패: %s", e)
        return ""
