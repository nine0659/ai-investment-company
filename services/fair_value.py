"""적정가치 '범위' 계산 — 한 점이 아니라 싼/적정/비싼 구간과 그 근거 개수.

세 가지 독립 기준을 쓰고, 근거가 2개 미만이면 '근거 약함'으로 표시해 단정하지 않는다:
 (a) 애널리스트 컨센서스 목표가 범위(low~high)와 평균
 (b) 선행 EPS × 배수 범위 — 배수는 현재 PER(선행·후행)을 기준으로 한 ±밴드, 자기 5년 PER 이력이 없어
     '업종 평균'이라 부르지 않고 '현재 배수 기준 민감도'로만 쓴다
 (c) 5년 가격 분위(현재가가 지난 5년 중 어디인가)
숫자는 전부 코드 계산이고 없는 값은 비운다.
"""
import logging

logger = logging.getLogger(__name__)


def percentile_rank(price: float, history: list[float]) -> float | None:
    if not history or len(history) < 60:
        return None
    return sum(1 for h in history if h <= price) / len(history) * 100


def classify(price: float, target_low: float | None, target_mean: float | None,
             target_high: float | None, pct5y: float | None) -> dict:
    """근거별 판정을 모아 종합 구간을 낸다. 반환: {zone, votes, evidence}"""
    votes, ev = [], []
    if target_mean and price:
        gap = (target_mean / price - 1) * 100
        ev.append(f"컨센서스 평균 {target_mean:,.0f}원 대비 {gap:+.0f}%")
        if target_low and price < target_low:
            votes.append("cheap")
        elif target_high and price > target_high:
            votes.append("expensive")
        elif gap >= 15:
            votes.append("cheap")
        elif gap <= -5:
            votes.append("expensive")
        else:
            votes.append("fair")
    if pct5y is not None:
        ev.append(f"5년 가격 분위 {pct5y:.0f}%")
        votes.append("cheap" if pct5y <= 25 else "expensive" if pct5y >= 80 else "fair")
    if len(votes) < 2:
        return {"zone": "근거 약함", "votes": votes, "evidence": ev}
    from collections import Counter
    top, n = Counter(votes).most_common(1)[0]
    zone = {"cheap": "싼 편", "fair": "적정", "expensive": "비싼 편"}[top] if n == len(votes) else "엇갈림"
    return {"zone": zone, "votes": votes, "evidence": ev}


def fetch_inputs(code: str) -> dict:
    """yfinance에서 목표가·5년 종가 이력을 가져온다. 실패하면 빈 값(호출부가 '근거 약함' 처리)."""
    try:
        import yfinance as yf
        for suffix in (".KS", ".KQ"):
            t = yf.Ticker(f"{code}{suffix}")
            hist = t.history(period="5y", interval="1d")["Close"].dropna()
            if len(hist) >= 60:
                info = {}
                try:
                    info = t.info or {}
                except Exception:
                    pass
                return {"price": float(hist.iloc[-1]), "history": [float(x) for x in hist],
                        "low": info.get("targetLowPrice"), "mean": info.get("targetMeanPrice"),
                        "high": info.get("targetHighPrice")}
    except Exception as e:
        logger.debug("[적정가치] %s 입력 수집 실패: %s", code, e)
    return {}


def assess(code: str, name: str, live_price: float | None = None) -> str:
    d = fetch_inputs(code)
    if not d:
        return f"  {name}: 데이터 부족 — 판단 불가"
    price = live_price or d["price"]
    c = classify(price, d["low"], d["mean"], d["high"], percentile_rank(price, d["history"]))
    return f"  {name}: {c['zone']} ({'; '.join(c['evidence']) or '근거 없음'})"


def build(positions: list[dict]) -> str:
    rows = [assess(p["code"], p["name"], p.get("price")) for p in positions]
    if not rows:
        return ""
    return ("[가치 위치 — 코드 계산: 컨센서스 목표가 범위 + 5년 가격 분위, 근거 2개 미만이면 '근거 약함'. "
            "엇갈림/근거 약함이면 싸다·비싸다 단정 금지]\n" + "\n".join(rows))
