"""내 계좌 성과 — 내 평단 기준 손익을 '시장 요인'과 '종목·타이밍 요인'으로 나눠 보여준다.

단순히 -25%라고만 하면 시장 하락 탓인지 내 선택 탓인지 알 수 없다. 여기서는
 ① 종목별 손익과 계좌 총손실에서 차지하는 비중(기여도)
 ② 같은 기간 KOSPI·베타로 계산한 '시장이 한 몫'과 나머지('종목·타이밍 몫')
 ③ 실현손익(승률·손익비) — KIS 동기화 사고로 만들어진 가짜 매도 기록은 제외
 ④ 최근 기준점 이후 계좌 vs KOSPI
 ⑤ AI 추천 성적표(시장 대비)
를 전부 코드로 계산한다. 매입일은 원장 등록일이라 실제와 다를 수 있어 그 사실을 표시한다.
"""
import logging
from datetime import datetime

from sqlalchemy import text

from db.database import get_conn

logger = logging.getLogger(__name__)

KIS_ARTIFACT_MARK = "KIS 동기화"     # 2026-08-14 사고로 생긴 가짜 매도 기록 표식


def kospi_return_since(entry_date: str, closes: dict[str, float]) -> float | None:
    """entry_date 이후 첫 거래일 종가 → 가장 최근 종가 수익률(%). 데이터 부족 시 None."""
    if not closes or not entry_date:
        return None
    days = sorted(d for d in closes if d >= entry_date[:10])
    if len(days) < 2:
        return None
    a, b = closes[days[0]], closes[days[-1]]
    return (b / a - 1) * 100 if a else None


def compute(positions: list[dict], closes: dict[str, float], entries: dict[str, str],
            betas: dict[str, float] | None = None) -> dict:
    """positions: portfolio_risk.load_positions 결과. entries: {code: entry_date}."""
    betas = betas or {}
    rows, tot_cost, tot_val, idx_gap, mkt_part = [], 0.0, 0.0, 0.0, 0.0
    for p in positions:
        cost = p["avg"] * p["qty"]
        if cost <= 0:
            continue
        pnl = p["value"] - cost
        ret = (p["price"] / p["avg"] - 1) * 100
        since = entries.get(p["code"], "")
        idx = kospi_return_since(since, closes)
        beta = betas.get(p["code"])
        exp_ret = beta * idx if (idx is not None and beta is not None) else (idx if idx is not None else None)
        rows.append({"name": p["name"], "ret": ret, "pnl": pnl, "cost": cost, "idx": idx, "since": since,
                     "beta": beta, "beta_assumed": beta is None and idx is not None,
                     "vs_idx_won": (p["value"] - cost * (1 + idx / 100)) if idx is not None else None,
                     "mkt_won": (cost * exp_ret / 100) if exp_ret is not None else None})
        tot_cost += cost
        tot_val += p["value"]
    total_pnl = tot_val - tot_cost
    loss_sum = sum(-r["pnl"] for r in rows if r["pnl"] < 0)
    for r in rows:
        r["share"] = (-r["pnl"] / loss_sum * 100) if (r["pnl"] < 0 and loss_sum > 0) else 0.0
    known = [r for r in rows if r["mkt_won"] is not None]
    mkt_part = sum(r["mkt_won"] for r in known)
    known_pnl = sum(r["pnl"] for r in known)
    return {"rows": rows, "cost": tot_cost, "value": tot_val, "pnl": total_pnl,
            "ret": (tot_val / tot_cost - 1) * 100 if tot_cost else 0.0,
            "mkt_part": mkt_part, "sel_part": known_pnl - mkt_part, "decomp_n": len(known),
            "complete": len(known) == len(rows) and bool(rows)}


def realized_stats(rows: list[dict]) -> dict:
    """portfolio_history 행 → 실현손익 요약. KIS 동기화로 생긴 가짜 기록은 제외하고 그 건수를 알린다."""
    real = [r for r in rows if KIS_ARTIFACT_MARK not in (r.get("memo") or "")]
    excluded = len(rows) - len(real)
    pnls = [r["quantity"] * (r["exit_price"] - r["avg_price"]) for r in real
            if r.get("exit_price") and r.get("avg_price")]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    return {"n": len(pnls), "wins": len(wins), "total": sum(pnls), "excluded": excluded,
            "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
            "avg_loss": (sum(losses) / len(losses)) if losses else 0.0}


def load_history() -> list[dict]:
    try:
        with get_conn() as conn:
            rs = conn.execute(text("SELECT code, name, quantity, avg_price, exit_price, exit_date, memo "
                                   "FROM portfolio_history ORDER BY exit_date")).fetchall()
        return [dict(zip(("code", "name", "quantity", "avg_price", "exit_price", "exit_date", "memo"), r)) for r in rs]
    except Exception as e:
        logger.debug("[계좌성과] 매매 이력 실패: %s", e)
        return []


def since_last_reset(nav: list[dict]) -> dict | None:
    """NAV 이력에서 마지막 '기준점 재설정'(평가액==매입액인 날) 이후 계좌 vs KOSPI.
    과거 사고로 NAV가 0%로 재설정된 날이 있어 그 이전과 이어 붙이면 틀린 수익률이 나온다."""
    valid = [h for h in nav if (h.get("total_cost") or 0) > 0 and (h.get("total_value") or 0) > 0]
    if len(valid) < 2:
        return None
    ref_i = max((i for i, h in enumerate(valid) if abs(h["total_value"] - h["total_cost"]) < 1), default=0)
    ref, last = valid[ref_i], valid[-1]
    if ref is last:
        return None
    acct = (last["total_value"] / last["total_cost"]) / (ref["total_value"] / ref["total_cost"]) - 1
    k0 = ref.get("kospi_close") or 0
    k1 = next((h["kospi_close"] for h in reversed(valid) if (h.get("kospi_close") or 0) > 0
               and h["kospi_close"] == h["kospi_close"]), 0)
    mk = (k1 / k0 - 1) * 100 if k0 > 0 and k1 > 0 else None
    return {"since": ref["date"], "acct": acct * 100, "kospi": mk}


def _won(x: float) -> str:
    return f"{x/10000:+,.0f}만원"


def format_perf(c: dict, realized: dict | None = None, reset: dict | None = None,
                scorecard_txt: str = "") -> str:
    if not c.get("rows"):
        return ""
    lines = ["[내 계좌 성과 — 내 평단 기준, 코드 계산]",
             f"합계: 투자 {c['cost']/10000:,.0f}만원 → 평가 {c['value']/10000:,.0f}만원 ({c['ret']:+.1f}%, {_won(c['pnl'])})"]
    lines.append("종목별 (손익률 · 손익 · 총손실 중 비중 · 같은 기간 KOSPI):")
    for r in sorted(c["rows"], key=lambda x: x["pnl"]):
        idx = f"{r['idx']:+.1f}%" if r["idx"] is not None else "비교불가"
        share = f" · 손실의 {r['share']:.0f}%" if r["share"] else ""
        lines.append(f"  {r['name']}: {r['ret']:+.1f}% · {_won(r['pnl'])}{share} · KOSPI {idx} (등록일 {r['since'] or '?'})")
    if c["decomp_n"]:
        tot = c["mkt_part"] + c["sel_part"]
        lines.append(f"손익 분해(비교 가능 {c['decomp_n']}종목): 시장 몫 {_won(c['mkt_part'])} + 종목·타이밍 몫 {_won(c['sel_part'])} = {_won(tot)}")
        if any(r["beta_assumed"] for r in c["rows"]):
            lines.append("  ※ 베타를 못 구한 종목은 시장과 같이 움직인다고(베타 1) 가정")
    if reset:
        k = f", 같은 기간 KOSPI {reset['kospi']:+.1f}%" if reset.get("kospi") is not None else ""
        lines.append(f"기준점({reset['since']}) 이후 계좌 {reset['acct']:+.1f}%{k}  ※ 그 이전 기록은 과거 사고로 0% 재설정돼 이어붙이지 않음")
    if realized and realized["n"]:
        wr = realized["wins"] / realized["n"] * 100
        lines.append(f"실현손익: {realized['n']}건 중 이익 {realized['wins']}건({wr:.0f}%) · 합계 {_won(realized['total'])}"
                     f" · 평균 이익 {_won(realized['avg_win'])} / 평균 손실 {_won(realized['avg_loss'])}"
                     + (f" (동기화 사고로 생긴 가짜 매도 {realized['excluded']}건 제외)" if realized["excluded"] else ""))
    if scorecard_txt:
        lines.append(scorecard_txt)
    lines.append("※ 매입일은 '원장 등록일'입니다. 실제 매수일이 다르면 `/holdings date 코드 YYYY-MM-DD`로 고치면 지수 비교가 정확해집니다.")
    return "\n".join(lines)


def _index_closes() -> dict[str, float]:
    try:
        import yfinance as yf
        from clients.market_data_client import fill_missing_index_closes
        start = (datetime.now().replace(year=datetime.now().year - 1)).strftime("%Y-%m-%d")
        h = yf.Ticker("^KS11").history(start=start, interval="1d")
        return fill_missing_index_closes({i.strftime("%Y-%m-%d"): float(r["Close"]) for i, r in h.iterrows()})
    except Exception as e:
        logger.debug("[계좌성과] 지수 이력 실패: %s", e)
        return {}


def build(kis=None) -> str:
    from services import portfolio_risk, rec_scorecard
    from services.portfolio_service import get_portfolio
    from services.nav_service import get_nav_history
    rep = portfolio_risk.build_report(kis)
    positions = [p for p in rep.get("positions", []) if p["value"] > 0]
    if not positions:
        return ""
    closes = _index_closes()
    entries = {p["code"]: (p.get("entry_date") or "") for p in get_portfolio()}
    betas = (rep.get("metrics") or {}).get("betas") or {}
    c = compute(positions, closes, entries, betas)
    try:
        from clients import naver_client as nv
        sc = rec_scorecard.format_scorecard(rec_scorecard.score(rec_scorecard.load_recs(), closes, nv.stock_quote))
    except Exception as e:
        logger.debug("[계좌성과] 성적표 실패: %s", e)
        sc = ""
    return format_perf(c, realized_stats(load_history()), since_last_reset(get_nav_history(120)), sc)
