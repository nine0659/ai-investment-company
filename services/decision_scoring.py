"""CEO 종목 판단(신규편입·비중조정)의 사후 채점 — 판단 시점 대비 N거래일 뒤 KOSPI 초과수익으로 (2026-10-13).

기존 결과채점(decision_outcome_service)은 stock_recommendations(7건)만 봤다. CEO가 매번 내리는
'줄여라/늘려라/신규편입' 판단은 cio_decisions_log에 기록만 되고 한 번도 채점되지 않았다.
여기서는 그 판단이 옳았는지를 가격으로 소급 채점한다: 매수 계열은 이후 KOSPI를 이겨야 맞고,
매도 계열(축소·청산)은 이후 KOSPI에 졌어야 맞다. 계산은 전부 코드, 표본이 작으면 경고를 코드가 붙인다.
"""
import json
import logging
from datetime import datetime, timedelta

from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)

HORIZONS = (5, 10)             # 판단 후 거래일 수
BUY_ACTIONS = {"new", "add", "increase", "buy", "raise"}
SELL_ACTIONS = {"reduce", "trim", "exit", "sell", "decrease", "cut"}
MIN_FOR_PATTERN = 8            # 이 건수 미만이면 '경향'조차 말하지 않는다


def load_decisions() -> list[dict]:
    """cio_decisions_log → 개별 종목 판단 목록 [{date, run_type, code, name, action, kind}]."""
    try:
        with get_conn() as conn:
            rows = conn.execute(text("SELECT date, run_type, decisions_json FROM cio_decisions_log ORDER BY date")).fetchall()
    except Exception as e:
        logger.debug("[판단채점] 로그 조회 실패: %s", e)
        return []
    out, seen = [], set()
    for date, run_type, raw in rows:
        try:
            d = json.loads(raw or "{}")
        except (ValueError, TypeError):
            continue
        items = [("new", p) for p in d.get("new_positions", [])] + \
                [((c.get("action") or "").lower(), c) for c in d.get("position_changes", [])]
        for action, it in items:
            code = str(it.get("code", "")).strip()
            if not code or action not in BUY_ACTIONS | SELL_ACTIONS:
                continue
            key = (date, code, action)
            if key in seen:
                continue
            seen.add(key)
            out.append({"date": date, "run_type": run_type, "code": code, "name": it.get("name", code),
                        "action": action, "side": "buy" if action in BUY_ACTIONS else "sell"})
    return out


def _fetch_closes(ticker: str, start: str) -> dict[str, float]:
    try:
        import yfinance as yf
        h = yf.Ticker(ticker).history(start=start, interval="1d")
        return {i.strftime("%Y-%m-%d"): float(r["Close"]) for i, r in h.iterrows() if r["Close"] == r["Close"]}
    except Exception as e:
        logger.debug("[판단채점] %s 조회 실패: %s", ticker, e)
        return {}


def fetch_stock_closes(code: str, start: str) -> dict[str, float]:
    return _fetch_closes(f"{code}.KS", start) or _fetch_closes(f"{code}.KQ", start)


def fetch_index_closes(start: str) -> dict[str, float]:
    from clients.market_data_client import fill_missing_index_closes
    return fill_missing_index_closes(_fetch_closes("^KS11", start))


def score_decision(dec: dict, stock: dict[str, float], index: dict[str, float]) -> list[dict]:
    """판단 1건 → 호라이즌별 채점 목록. 기준가: 판단 직전 종가(마감 판단은 당일 종가). 데이터 부족한 호라이즌은 제외."""
    dates = sorted(set(stock) & set(index))
    if not dates:
        return []
    cutoff = dec["date"] if dec["run_type"] == "close_market" else (
        (datetime.strptime(dec["date"], "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d"))
    base_idx = max((i for i, d in enumerate(dates) if d <= cutoff), default=None)
    if base_idx is None:
        return []
    b = dates[base_idx]
    out = []
    for h in HORIZONS:
        if base_idx + h >= len(dates):
            continue
        e = dates[base_idx + h]
        s_ret = (stock[e] - stock[b]) / stock[b] * 100
        i_ret = (index[e] - index[b]) / index[b] * 100
        alpha = s_ret - i_ret
        correct = alpha > 0 if dec["side"] == "buy" else alpha < 0
        out.append({**dec, "horizon": h, "ret": round(s_ret, 1), "index_ret": round(i_ret, 1),
                    "alpha": round(alpha, 1), "correct": bool(correct), "end": e})
    return out


def score_all(decisions: list[dict] | None = None, stock_fetcher=fetch_stock_closes,
              index_fetcher=fetch_index_closes) -> list[dict]:
    decisions = load_decisions() if decisions is None else decisions
    if not decisions:
        return []
    start = (datetime.strptime(min(d["date"] for d in decisions), "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
    index = index_fetcher(start)
    if not index:
        return []
    results, cache = [], {}
    for d in decisions:
        if d["code"] not in cache:
            cache[d["code"]] = stock_fetcher(d["code"], start)
        if cache[d["code"]]:
            results += score_decision(d, cache[d["code"]], index)
    return results


def format_summary(results: list[dict]) -> str:
    """채점 결과를 프롬프트/복기용 사실 목록으로. 채점된 것이 없으면 빈 문자열."""
    if not results:
        return ""
    lines = ["[CEO 종목 판단 사후 채점 — 판단 후 N거래일 KOSPI 대비 초과수익을 코드가 계산. 이 숫자만 인용]"]
    for h in HORIZONS:
        sub = [r for r in results if r["horizon"] == h]
        if not sub:
            continue
        for side, label in (("buy", "신규편입·확대"), ("sell", "축소·청산")):
            s = [r for r in sub if r["side"] == side]
            if s:
                ok = sum(1 for r in s if r["correct"])
                avg = sum(r["alpha"] for r in s) / len(s)
                lines.append(f"{h}거래일 후 · {label}: {len(s)}건 중 판단이 맞은 건 {ok}건, 평균 초과수익 {avg:+.1f}%p")
    wrong = sorted((r for r in results if r["horizon"] == HORIZONS[0] and not r["correct"]),
                   key=lambda r: abs(r["alpha"]), reverse=True)[:3]
    for r in wrong:
        verb = "축소" if r["side"] == "sell" else "편입/확대"
        lines.append(f"- 빗나간 판단: {r['date']} {r['name']} {verb} → {HORIZONS[0]}거래일 후 KOSPI 대비 {r['alpha']:+.1f}%p")
    total = len({(r["date"], r["code"], r["action"]) for r in results})
    lines.append(f"※ 서로 다른 판단 {total}건뿐이다: " + ("패턴으로 단정하지 말고 '가능성'으로만 서술하라"
                                                    if total < MIN_FOR_PATTERN else "경향으로 참고하되 표본 편중에 주의하라"))
    return "\n".join(lines)


_CTX_CACHE: dict = {"ts": 0.0, "txt": ""}


def build_context() -> str:
    """CEO 입력용 요약 (6시간 캐시 — 종목마다 yfinance를 부르므로 브리핑마다 반복하지 않는다)."""
    import time
    if _CTX_CACHE["txt"] and time.time() - _CTX_CACHE["ts"] < 6 * 3600:
        return _CTX_CACHE["txt"]
    try:
        txt = format_summary(score_all())
        if txt:
            _CTX_CACHE.update(ts=time.time(), txt=txt)
        return txt
    except Exception as e:
        logger.debug("[판단채점] 컨텍스트 생성 실패: %s", e)
        return ""
