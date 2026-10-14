"""내 계좌 성과 — 내 평단 기준으로 '지수 ETF를 샀다면 / 현금이었다면'과 비교한 기회비용.

추천 시점 기준 가상 수익률(2026-10-02 사고)과 구분해, 여기서는 오직 '내 매입가·내 매입일'을 쓴다.
매입일(entry_date)은 원장 등록일일 수 있어 '원장 기준'이라고 표시한다.
"""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def kospi_return_since(entry_date: str, closes: dict[str, float]) -> float | None:
    """entry_date 이후 첫 거래일 종가 → 가장 최근 종가 수익률(%). 데이터 부족 시 None."""
    if not closes or not entry_date:
        return None
    days = sorted(d for d in closes if d >= entry_date[:10])
    if len(days) < 2:
        return None
    a, b = closes[days[0]], closes[days[-1]]
    return (b / a - 1) * 100 if a else None


def compute(positions: list[dict], closes: dict[str, float], entries: dict[str, str]) -> dict:
    """positions: portfolio_risk.load_positions 결과. entries: {code: entry_date}."""
    rows, tot_cost, tot_val, tot_idx_val = [], 0.0, 0.0, 0.0
    for p in positions:
        cost = p["avg"] * p["qty"]
        if cost <= 0:
            continue
        ret = (p["price"] / p["avg"] - 1) * 100
        idx = kospi_return_since(entries.get(p["code"], ""), closes)
        idx_val = cost * (1 + idx / 100) if idx is not None else None
        rows.append({"name": p["name"], "ret": ret, "idx": idx, "since": entries.get(p["code"], ""),
                     "vs_idx_won": (p["value"] - idx_val) if idx_val is not None else None})
        tot_cost += cost
        tot_val += p["value"]
        if idx_val is not None:
            tot_idx_val += idx_val
        else:
            tot_idx_val += cost   # 비교 불가분은 중립(원금)으로 두되 아래서 표시
    return {"rows": rows, "cost": tot_cost, "value": tot_val,
            "ret": (tot_val / tot_cost - 1) * 100 if tot_cost else 0.0,
            "vs_idx_won": tot_val - tot_idx_val if rows else 0.0,
            "complete": all(r["idx"] is not None for r in rows)}


def format_perf(c: dict) -> str:
    if not c.get("rows"):
        return ""
    lines = ["[내 계좌 성과 — 내 평단·원장 등록일 기준, 코드 계산]"]
    for r in c["rows"]:
        idx = f" · 같은 기간 KOSPI {r['idx']:+.1f}% → 지수였다면 대비 {r['vs_idx_won']:+,.0f}원" if r["idx"] is not None else " · 지수 비교 불가"
        lines.append(f"  {r['name']}: {r['ret']:+.1f}%{idx}")
    lines.append(f"  합계: 투자 {c['cost']:,.0f}원 → 평가 {c['value']:,.0f}원 ({c['ret']:+.1f}%)"
                 + (f", 지수 대비 {c['vs_idx_won']:+,.0f}원" if c["complete"] else " (일부 종목 지수 비교 불가)"))
    lines.append("  ※ 현금이었다면 0원 변화. 위 수치는 AI 추천 성과가 아니라 '내 매매' 성과입니다.")
    return "\n".join(lines)


def build(kis=None) -> str:
    from services.portfolio_risk import load_positions
    from services.portfolio_service import get_portfolio
    try:
        import yfinance as yf
        from clients.market_data_client import fill_missing_index_closes
        start = (datetime.now().replace(year=datetime.now().year - 1)).strftime("%Y-%m-%d")
        h = yf.Ticker("^KS11").history(start=start, interval="1d")
        closes = fill_missing_index_closes({i.strftime("%Y-%m-%d"): float(r["Close"]) for i, r in h.iterrows()})
    except Exception as e:
        logger.debug("[계좌성과] 지수 이력 실패: %s", e)
        closes = {}
    entries = {p["code"]: (p.get("entry_date") or "") for p in get_portfolio()}
    return format_perf(compute(load_positions(kis), closes, entries))
