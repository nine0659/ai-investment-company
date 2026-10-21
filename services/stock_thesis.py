"""
services/stock_thesis.py — 종목 판단서 (보유 종목에 대한 '그래서 어떻게 하라'를 내놓는 분석)

숫자는 전부 코드가 모으고 계산한다: 가격 위치, 이익 추정치와 그 변화(리비전), 컨센서스 쏠림, 동종 비교,
시나리오별 가격 민감도, 내 계좌에서의 비중·감축 수량. LLM은 이 사실 블록 위에서 결론·해석·행동만 쓴다.

핵심 질문 하나: "시장은 지금 이 가격에 무엇을 반영하고 있고, 그게 맞는가?"
 - 가격은 오르내리는데 이익 추정은 다른 방향이면 → 시장이 컨센서스를 믿지 않는다는 뜻 (사이클 정점/바닥 논쟁)
 - 컨센서스가 한쪽으로 쏠려 있으면 → 그 의견은 이미 가격에 반영됐을 가능성
"""
import logging

logger = logging.getLogger(__name__)

# 동종 비교 대상 (같은 사이클을 타는 종목) — 없으면 비교 생략
PEERS = {
    "000660": [("MU", "마이크론"), ("005930.KS", "삼성전자")],
    "005930": [("MU", "마이크론"), ("000660.KS", "SK하이닉스")],
    "005380": [("000270.KS", "기아"), ("7203.T", "토요타")],
    "009150": [("6981.T", "무라타제작소"), ("005930.KS", "삼성전자")],
}


# ── 순수 계산 ──────────────────────────────────────────────────────

def pct(a: float | None, b: float | None) -> float | None:
    """b 대비 a의 변화율(%). 값이 없거나 b가 0이면 None."""
    if a is None or b in (None, 0):
        return None
    return round((a / b - 1) * 100, 1)


def per_at(price: float | None, eps: float | None) -> float | None:
    if not price or not eps or eps <= 0:
        return None
    return round(price / eps, 2)


def revision_view(trend: dict) -> dict:
    """eps_trend 한 행({current, 7daysAgo, 30daysAgo, ...}) → 7/30/90일 전 대비 변화율."""
    cur = trend.get("current")
    return {"d7": pct(cur, trend.get("7daysAgo")), "d30": pct(cur, trend.get("30daysAgo")),
            "d90": pct(cur, trend.get("90daysAgo"))}


def scenario_table(eps_low: float | None, eps_avg: float | None, eps_high: float | None,
                   price: float, pers: tuple = (3, 5, 7, 10)) -> list[dict]:
    """EPS(하·중·상) × PER 가정 → 주가와 현재가 대비 %. 어느 PER이 맞는지는 판단하지 않는다 — 민감도만."""
    rows = []
    for label, eps in (("하방(최저 추정)", eps_low), ("기본(평균 추정)", eps_avg), ("상방(최고 추정)", eps_high)):
        if not eps or eps <= 0:
            continue
        rows.append({"label": label, "eps": eps,
                     "cells": [{"per": p, "price": eps * p, "vs_now": pct(eps * p, price)} for p in pers]})
    return rows


def consensus_skew(buy: int, hold: int, sell: int) -> dict:
    n = buy + hold + sell
    if n == 0:
        return {}
    return {"n": n, "buy_pct": round(buy / n * 100), "sell_pct": round(sell / n * 100),
            "one_sided": n >= 8 and (buy / n >= 0.85 or sell / n >= 0.85)}


def implied_gap(price: float, target_low: float | None, target_mean: float | None) -> dict:
    return {"mean_vs_now": pct(target_mean, price), "low_vs_now": pct(target_low, price)}


def trim_math(value: float, total: float, cap_pct: float, price: float, avg: float) -> dict:
    from services.concentration_rule import _trim_plan
    return _trim_plan(value, total, cap_pct, price, avg)


# ── 데이터 수집 ────────────────────────────────────────────────────

def _row(df, period: str) -> dict:
    try:
        r = df.loc[period]
        return {k: (None if r[k] != r[k] else float(r[k])) for k in r.index if k != "currency"}
    except Exception:
        return {}


def fetch_estimates(symbol: str) -> dict:
    """yfinance에서 가격·52주 범위·목표가·투자의견 분포·이익 추정과 그 추세·리비전을 모은다. 실패 항목은 비운다."""
    out: dict = {"symbol": symbol}
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        try:
            info = t.info or {}
        except Exception:
            info = {}
        out.update(price=info.get("currentPrice") or info.get("regularMarketPrice"),
                   hi52=info.get("fiftyTwoWeekHigh"), lo52=info.get("fiftyTwoWeekLow"),
                   t_low=info.get("targetLowPrice"), t_mean=info.get("targetMeanPrice"),
                   t_high=info.get("targetHighPrice"), n_analysts=info.get("numberOfAnalystOpinions"))
        for key, attr in (("est", "earnings_estimate"), ("trend", "eps_trend"), ("rev", "eps_revisions")):
            try:
                df = getattr(t, attr)
                out[key] = {p: _row(df, p) for p in ("0q", "+1q", "0y", "+1y")} if df is not None else {}
            except Exception:
                out[key] = {}
        try:
            rs = t.recommendations_summary
            r0 = rs.iloc[0]
            out["recs"] = {"buy": int(r0["strongBuy"] + r0["buy"]), "hold": int(r0["hold"]),
                           "sell": int(r0["sell"] + r0["strongSell"])}
            r3 = rs.iloc[-1]
            out["recs_3m_ago"] = {"buy": int(r3["strongBuy"] + r3["buy"]), "hold": int(r3["hold"]),
                                  "sell": int(r3["sell"] + r3["strongSell"])}
        except Exception:
            out["recs"] = {}
        try:
            h = t.history(period="6mo")["Close"].dropna()
            if len(h) > 30:
                out["ret_1m"] = pct(float(h.iloc[-1]), float(h.iloc[-22]))
                out["ret_3m"] = pct(float(h.iloc[-1]), float(h.iloc[-64])) if len(h) > 64 else None
                out["price"] = out.get("price") or float(h.iloc[-1])
        except Exception:
            pass
    except Exception as e:
        logger.warning("[판단서] %s 추정치 수집 실패: %s", symbol, e)
    return out


def account_context(code: str) -> dict:
    """내 계좌에서의 이 종목 — 비중, 평단, 손익, 상한 대비 초과분과 감축 수량."""
    try:
        from services import portfolio_service as ps, concentration_rule as cr
        kis = None
        try:
            from clients.kis_client import KISClient
            kis = KISClient()
        except Exception:
            pass
        pos = ps.calculate_pnl(kis)
        # 시세 조회가 전부 실패하면 현재가가 평단으로 채워져 '손익 0%'로 보인다 — 그런 값으로 판단하지 않는다
        if pos and all(p.get("current_price") == p.get("avg_price") for p in pos):
            logger.warning("[판단서] 보유 종목 현재가 조회 실패 — 계좌 맥락 생략")
            return {}
        total = sum(p.get("current_val") or 0 for p in pos)
        me = next((p for p in pos if p["code"] == code), None)
        if not me or total <= 0:
            return {}
        caps = cr.get_caps()
        w = me["current_val"] / total * 100
        ctx = {"qty": me["quantity"], "avg": me["avg_price"], "price": me["current_price"], "pnl_pct": me["pnl_pct"],
               "weight": round(w, 1), "cap": caps.get("stock"), "total": total,
               "others": [(p["name"], round((p.get("current_val") or 0) / total * 100, 1), p["pnl_pct"])
                          for p in pos if p["code"] != code]}
        if caps.get("stock") and w > caps["stock"]:
            ctx["trim"] = trim_math(me["current_val"], total, caps["stock"], me["current_price"], me["avg_price"])
        return ctx
    except Exception as e:
        logger.warning("[판단서] 계좌 맥락 실패: %s", e)
        return {}


def find_signals(me: dict, peers: dict[str, dict]) -> list[str]:
    """사실들을 맞대어 코드가 찾는 신호. 해석(좋다/나쁘다)은 하지 않고 '무엇이 어긋나는지'만 적는다."""
    out = []
    est, trend = me.get("est") or {}, me.get("trend") or {}
    y0 = revision_view(trend.get("0y") or {}).get("d90")
    q0 = revision_view(trend.get("0q") or {}).get("d90")
    qn = revision_view(trend.get("+1q") or {}).get("d90")
    r3 = me.get("ret_3m")
    if r3 is not None and r3 <= -10 and y0 is not None and y0 >= 5:
        out.append(f"가격↔이익추정 괴리: 최근 3개월 가격 {r3:+.1f}%인데 올해 EPS 추정은 같은 기간 {y0:+.1f}% 상향 "
                   f"→ 시장이 컨센서스 이익을 그대로 믿지 않는 구간")
    if y0 is not None and y0 >= 5 and ((q0 is not None and q0 <= -2) or (qn is not None and qn <= -2)):
        out.append(f"연간 vs 분기 엇갈림: 올해 EPS 추정 90일 {y0:+.1f}% 상향이지만 분기 추정은 "
                   f"이번 분기 {q0:+.1f}%·다음 분기 {qn:+.1f}% (이미 반영된 실적이 연간을 끌어올렸고, 앞으로 분기 추정은 내려가는 중)")
    r = me.get("recs") or {}
    if r and consensus_skew(r["buy"], r["hold"], r["sell"]).get("one_sided") and r3 is not None and r3 <= -10:
        out.append(f"의견 쏠림 속 가격 약세: 매수 {r['buy']}·매도 {r['sell']}인데 3개월 {r3:+.1f}% — 컨센서스가 가격을 설명하지 못함")
    for name, pe in peers.items():
        p3 = pe.get("ret_3m")
        if r3 is not None and p3 is not None and abs(r3 - p3) >= 10:
            out.append(f"동종 대비: 3개월 수익률 {name} {p3:+.1f}% vs 이 종목 {r3:+.1f}% (차이 {r3 - p3:+.1f}%p)")
        py0 = revision_view((pe.get("trend") or {}).get("0y") or {}).get("d90")
        if y0 is not None and py0 is not None and abs(py0 - y0) >= 5:
            out.append(f"동종 이익추정 방향: {name} 올해 EPS 추정 90일 {py0:+.1f}% vs 이 종목 {y0:+.1f}%")
    return out


# ── 과거 이익 사이클 (DART 연간 순이익 + 주가 이력) ─────────────────

def cycle_peaks(ni: dict[int, float], closes: list[tuple[str, float]], shares: float) -> list[dict]:
    """연간 순이익이 직전·직후 해보다 큰 '이익 정점 해'마다, 그 시기 주가가 어떻게 움직였는지 계산한다.
    closes: [(YYYY-MM-DD, 종가)] 오래된→최신. shares: 주식수(현재값으로 근사 — 증자·소각이 크면 PER이 어긋난다).
    반환 각 행: year, ni, ni_yoy, hi_date, hi_price, ye_price, per_ye, dd_to_ye, dd_to_trough, ret_next_year, ni_next_yoy"""
    if not closes or not shares:
        return []

    def in_range(a: str, b: str):
        return [(d, c) for d, c in closes if a <= d <= b]

    def year_end(y: int):
        r = in_range(f"{y}-01-01", f"{y}-12-31")
        return r[-1][1] if r else None

    rows = []
    for y in sorted(ni):
        if (y - 1) not in ni or (y + 1) not in ni:
            continue
        if not (ni[y] > 0 and ni[y] > ni[y - 1] and ni[y] > ni[y + 1]):
            continue
        win = in_range(f"{y - 1}-01-01", f"{y}-12-31")
        ye, ye_next = year_end(y), year_end(y + 1)
        if not win or not ye:
            continue
        hi_d, hi_p = max(win, key=lambda x: x[1])
        after = in_range(hi_d, f"{y + 1}-12-31")
        trough = min(c for _, c in after) if after else None
        eps = ni[y] / shares
        rows.append({
            "year": y, "ni": ni[y], "ni_yoy": pct(ni[y], ni[y - 1]), "hi_date": hi_d, "hi_price": hi_p, "ye_price": ye,
            "per_ye": round(ye / eps, 1) if eps > 0 else None,
            "dd_to_ye": pct(ye, hi_p), "dd_to_trough": pct(trough, hi_p) if trough else None,
            "ret_next_year": pct(ye_next, ye) if ye_next else None,
            "ni_next_yoy": pct(ni[y + 1], ni[y]),
        })
    return rows


def per_band(ni: dict[int, float], closes: list[tuple[str, float]], shares: float | None) -> dict:
    """이 종목 자신의 과거 연말 PER(흑자 해) 사분위 — 시나리오 배수를 임의로 정하지 않고 자기 역사에서 가져온다.
    표본 5개 미만이면 빈 값."""
    if not shares or not ni or not closes:
        return {}
    pers = []
    for y, v in ni.items():
        ye = next((c for d, c in reversed(closes) if d <= f"{y}-12-31"), None)
        if v > 0 and ye:
            pers.append(ye / (v / shares))
    if len(pers) < 5:
        return {}
    pers.sort()
    n = len(pers)
    return {"n": n, "p25": round(pers[n // 4], 1), "p50": round(pers[n // 2], 1), "p75": round(pers[(3 * n) // 4], 1)}


def fetch_cycle(code: str, price: float | None, shares: float | None) -> dict:
    """DART 연간 순이익(최대 12년) + yfinance 주가 이력. 실패하면 빈 값."""
    try:
        from clients.dart_client import get_multi_year_financials
        import yfinance as yf
        fins = get_multi_year_financials(code, years=12)
        ni = {f["year"]: float(f["당기순이익"]) for f in fins
              if f.get("period") == "연간" and f.get("당기순이익") is not None}
        h = yf.Ticker(f"{code}.KS").history(period="max")["Close"].dropna()
        closes = [(str(i.date()), float(v)) for i, v in h.items()]
        return {"ni": ni, "closes": closes, "rows": cycle_peaks(ni, closes, shares or 0),
                "band": per_band(ni, closes, shares)}
    except Exception as e:
        logger.warning("[판단서] 이익 사이클 수집 실패 (%s): %s", code, e)
        return {}


def format_cycle(c: dict, me: dict, shares: float | None) -> list[str]:
    rows = c.get("rows") or []
    if not rows:
        return []
    out = ["[과거 이익 정점 때 주가는 어떻게 움직였나 — DART 연간 순이익 + 주가 (PER은 주식수를 현재값으로 근사)]"]
    for r in rows:
        out.append(
            f"  {r['year']}년 순이익 {r['ni'] / 1e12:,.1f}조(전년 {r['ni_yoy']:+.0f}%) 정점: 주가 고점 {r['hi_date']} {r['hi_price']:,.0f}원 → "
            f"정점 해 연말 {r['dd_to_ye']:+.0f}%(연말 PER {r['per_ye']}배), 이후 최저까지 {r['dd_to_trough']:+.0f}% | "
            f"다음 해 순이익 {r['ni_next_yoy']:+.0f}%, 연말 주가는 그 뒤 12개월 {r['ret_next_year']:+.0f}%"
            if r["ret_next_year"] is not None else
            f"  {r['year']}년 순이익 {r['ni'] / 1e12:,.1f}조(전년 {r['ni_yoy']:+.0f}%) 정점: 주가 고점 {r['hi_date']} {r['hi_price']:,.0f}원 → "
            f"정점 해 연말 {r['dd_to_ye']:+.0f}%(연말 PER {r['per_ye']}배)")
    closes = c.get("closes") or []
    p = me.get("price")
    if closes and p:
        last = closes[-1][0]
        recent = [(d, v) for d, v in closes if d >= f"{int(last[:4]) - 1}{last[4:]}"]
        if recent:
            hd, hv = max(recent, key=lambda x: x[1])
            est = me.get("est") or {}
            e0, e1 = (est.get("0y") or {}).get("avg"), (est.get("+1y") or {}).get("avg")
            tail = ""
            if e0 and e1:
                tail = (f" 올해 EPS 추정 기준 PER {per_at(p, e0)}배. 컨센서스 이익은 "
                        + ("내년까지 계속 늘어나는 것으로 잡혀 있어 아직 정점이 아님" if e1 > e0 else "내년에 줄어드는 것으로 잡혀 있음"))
            out.append(f"  지금: 최근 1년 주가 고점 {hd} {hv:,.0f}원 → 현재 {pct(p, hv):+.0f}%.{tail}")
    ni = c.get("ni") or {}
    if shares and ni:
        pers = []
        for y, v in ni.items():
            ye = next((cl for d, cl in reversed(closes) if d <= f"{y}-12-31"), None)
            if v > 0 and ye:
                pers.append(ye / (v / shares))
        if len(pers) >= 5:
            pers.sort()
            out.append(f"  참고: 순이익이 흑자였던 {len(pers)}개 연말의 PER 분포 — 최소 {pers[0]:.1f}배 · 중앙 {pers[len(pers) // 2]:.1f}배 · "
                       f"최대 {pers[-1]:.1f}배 (이익 사이클 업종은 이익이 클수록 PER이 낮게 찍히는 경향 — 일반론)")
    return out


# ── 사실 블록 ──────────────────────────────────────────────────────

def _won(x) -> str:
    return f"{x:,.0f}원" if x else "n/a"


def format_estimates(e: dict, label: str, krw: bool = True) -> list[str]:
    cur = "원" if krw else "$"
    f = (lambda v: f"{v:,.0f}") if krw else (lambda v: f"{v:,.2f}")
    p = e.get("price")
    lines = [f"[{label}] 가격 {f(p) if p else 'n/a'}{cur if krw else ''}"
             + (f" | 52주 고점 대비 {pct(p, e.get('hi52'))}%" if pct(p, e.get('hi52')) is not None else "")
             + (f" | 1개월 {e['ret_1m']:+.1f}%" if e.get("ret_1m") is not None else "")
             + (f" | 3개월 {e['ret_3m']:+.1f}%" if e.get("ret_3m") is not None else "")]
    est, trend, rev = e.get("est") or {}, e.get("trend") or {}, e.get("rev") or {}
    for per, name in (("0y", "올해"), ("+1y", "내년"), ("0q", "이번 분기"), ("+1q", "다음 분기")):
        row = est.get(per) or {}
        avg = row.get("avg")
        if not avg:
            continue
        rv = revision_view(trend.get(per) or {})
        rvt = ", ".join(f"{k[1:]}일 전 대비 {v:+.1f}%" for k, v in rv.items() if v is not None)
        rr = rev.get(per) or {}
        up30, dn30 = int(rr.get("upLast30days") or 0), int(rr.get("downLast30days") or 0)
        pe = per_at(p, avg)
        lines.append(f"  {name} EPS 추정 평균 {f(avg)}{'' if krw else '$'}(최저 {f(row['low'])} ~ 최고 {f(row['high'])}, "
                     f"{int(row.get('numberOfAnalysts') or 0)}명)"
                     + (f" → 현재가 기준 PER {pe}배" if pe else "")
                     + (f" | 추정 변화: {rvt}" if rvt else "")
                     + (f" | 30일 리비전 상향 {up30}·하향 {dn30}" if up30 or dn30 else ""))
    g0, g1 = (est.get("0y") or {}).get("avg"), (est.get("+1y") or {}).get("avg")
    if g0 and g1:
        lines.append(f"  내년 EPS 성장률(추정): {pct(g1, g0):+.1f}% (올해→내년)")
    if e.get("t_mean"):
        gap = implied_gap(p, e.get("t_low"), e.get("t_mean"))
        lines.append(f"  증권사 목표가: 평균 {f(e['t_mean'])} (현재가 대비 {gap['mean_vs_now']:+.0f}%), "
                     f"최저 {f(e['t_low']) if e.get('t_low') else 'n/a'}"
                     + (f" (현재가 대비 {gap['low_vs_now']:+.0f}%)" if gap.get("low_vs_now") is not None else ""))
    r = e.get("recs")
    if r:
        sk = consensus_skew(r["buy"], r["hold"], r["sell"])
        ago = e.get("recs_3m_ago") or {}
        lines.append(f"  투자의견: 매수 {r['buy']}·중립 {r['hold']}·매도 {r['sell']}"
                     + (f" (3개월 전 매수 {ago['buy']}·매도 {ago['sell']})" if ago else "")
                     + (" ← 한쪽으로 극단적 쏠림" if sk.get("one_sided") else ""))
    return lines


def format_scenarios(e: dict, band: dict | None = None) -> list[str]:
    est = e.get("est") or {}
    row = est.get("+1y") or {}
    p = e.get("price")
    if not p or not band:
        return []      # 자기 역사에서 가져온 배수가 없으면 임의의 PER로 시나리오를 만들지 않는다
    pers = (band["p25"], band["p50"], band["p75"])
    tbl = scenario_table(row.get("low"), row.get("avg"), row.get("high"), p, pers=pers)
    if not tbl:
        return []
    avg, low = row.get("avg"), row.get("low")
    imp = []
    for label, per in (("하위25%", pers[0]), ("중앙", pers[1])):
        eps_imp = p / per
        imp.append(f"PER {per}배({label})로 보면 현재가가 내포한 내년 EPS {eps_imp:,.0f}"
                   + (f" = 컨센서스 평균의 {eps_imp / avg * 100:.0f}%" if avg else "")
                   + (f"·최저 추정의 {eps_imp / low * 100:.0f}%" if low else ""))
    lines = [f"[내년 EPS × PER — 배수는 이 종목의 과거 {band['n']}개 연말 PER 하위25%·중앙·상위25%({pers[0]}·{pers[1]}·{pers[2]}배)."
             " 이익이 클 때 PER이 낮게 찍히는 업종이면 중앙·상위 배수는 낙관 편향. 어느 배수가 맞는지는 판단하지 않은 산술]",
             "  현재가가 내포한 내년 PER: " + " · ".join(f"{r['label'][:2]} EPS 기준 {per_at(p, r['eps'])}배" for r in tbl)]
    for r in tbl:
        cells = " | ".join(f"PER {c['per']}배 → {c['price']:,.0f}원({c['vs_now']:+.0f}%)" for c in r["cells"])
        lines.append(f"  {r['label']} EPS {r['eps']:,.0f}: {cells}")
    lines.append("  [역산 — 시장이 가격에 넣은 이익] " + " / ".join(imp)
                 + " (100%보다 낮을수록 시장이 컨센서스보다 훨씬 나쁜 이익을 가정한다는 뜻)")
    return lines


def format_account(a: dict, name: str) -> list[str]:
    if not a:
        return ["[내 계좌] 이 종목 보유 정보 없음"]
    lines = [f"[내 계좌] {name} {a['qty']}주 | 평단 {a['avg']:,.0f}원 → 현재가 {a['price']:,.0f}원 ({a['pnl_pct']:+.1f}%) | "
             f"포트폴리오 비중 {a['weight']}% (내 상한 규칙 {a['cap']}%)"]
    lines.append("  다른 보유: " + ", ".join(f"{n} {w}%({pl:+.0f}%)" for n, w, pl in a["others"]))
    t = a.get("trim")
    if t:
        lines.append(f"  상한까지 줄이려면: 현금화 시 {t['sell_cash_qty']}주(약 {t['sell_cash_won'] / 1e4:,.0f}만원), "
                     f"다른 종목으로 옮길 땐 {t['sell_reinvest_qty']}주. 하루 {t['per_day_qty']}주씩이면 "
                     f"{t['days_cash']}~{t['days_reinvest']}거래일. 지금 팔면 주당 {t['realized_per_share']:+,.0f}원 손익 확정")
    return lines


def build_facts(code: str, name: str) -> tuple[str, dict]:
    sym = f"{code}.KS"
    me = fetch_estimates(sym)
    lines = format_estimates(me, f"{name} — 시장·컨센서스(yfinance)")
    try:
        import yfinance as yf
        info = yf.Ticker(sym).info or {}
        shares = info.get("sharesOutstanding") or ((info.get("marketCap") / me["price"]) if info.get("marketCap") and me.get("price") else None)
    except Exception:
        shares = None
    cyc = fetch_cycle(code, me.get("price"), shares)
    lines += format_scenarios(me, cyc.get("band"))
    lines += format_cycle(cyc, me, shares)
    peers: dict[str, dict] = {}
    for psym, pname in PEERS.get(code, []):
        pe = fetch_estimates(psym)
        if pe.get("price"):
            peers[pname] = pe
            lines += format_estimates(pe, f"동종 비교: {pname}", krw=psym.endswith(".KS"))
    sig = find_signals(me, peers)
    if sig:
        lines += ["[코드가 찾은 신호 — 서로 어긋나는 사실]"] + [f"  ▸ {x}" for x in sig]
    lines += format_account(account_context(code), name)
    try:
        from services.macro_dashboard import build_macro_dashboard
        dash = build_macro_dashboard()
        if dash:
            lines.append(dash)
    except Exception as ex:
        logger.warning("[판단서] 매크로 대시보드 실패: %s", ex)
    try:
        from services import research_synthesis as rs
        ctx = rs.build_research_context()
        if ctx:
            lines.append(ctx)
    except Exception as ex:
        logger.warning("[판단서] 리서치 컨텍스트 실패: %s", ex)
    return "\n".join(lines), me


# ── LLM 서술 ───────────────────────────────────────────────────────

_SYSTEM = """당신은 세계 최고 수준의 투자자입니다. 아래 [사실]만을 근거로, 이 종목에 대해 사용자(중장기 가치투자자,
최종 판단은 본인이 함)에게 분명한 의견을 냅니다. 양쪽을 다 말하며 얼버무리지 말고 결론을 먼저, 근거를 뒤에 씁니다.
읽는 사람은 전문용어에 익숙하지 않으니 쉬운 말로, 숫자는 근거로만 붙입니다.

[규칙]
- 숫자는 [사실]에 있는 것만, 사실 블록의 표기 그대로(원 단위 전체 표기) 쓴다. 만원·억원 등으로 환산하지 않는다.
  없는 수치는 만들지 않는다. 확률(%)은 근거가 없으므로 쓰지 않는다.
- [사실]에 없는 일반 지식은 '(일반론)'이라고 표시한다.
- 증권사 의견·컨센서스는 참고일 뿐이다. 쏠림이 있으면 경계 신호로 다루고, 결론을 컨센서스에 기대지 않는다.
- '기대된다/유망하다' 같은 막연한 표현 금지. 판단마다 근거 숫자를 붙인다.
- 두 질문을 반드시 분리한다: (A) 이 종목이 지금 가격에서 매력적인가 — (B) 내 계좌 구조(비중·손실)가 어떤가.
  종목이 매력적이어도 비중이 과하면 줄여야 할 수 있고, 반대도 있다. 둘을 섞어 이유로 쓰지 않는다.
- 사실 블록의 [과거 이익 정점] 표가 있으면 반드시 인용해 '지금이 그때와 무엇이 같고 다른지'를 낙폭·PER 숫자로 비교한다.
  표본이 2개뿐이면 그렇다고 밝힌다. [코드가 찾은 신호]가 있으면 각각 해석한다.
- 매도·매수 수량은 [내 계좌]의 값을 그대로 인용한다. 범위를 지어내지 않는다.

[형식 — 짧게]
① 결론 두 줄 — "종목 판단:" [보유 유지/추가 매수/관망/정리] + 확신도(상·중·하) + 이유 한 줄 / "계좌 판단:" 비중에 대한 행동 한 줄
② 시장은 지금 무엇을 가격에 넣고 있나 — 사실 블록의 [역산]을 반드시 해석한다(시장이 가정한 이익이 컨센서스의 몇 %인지, 그게 무엇을 뜻하는지). 가격 움직임과 이익 추정·리비전·목표가·투자의견을 맞대어, 시장이 컨센서스를 믿는지 불신하는지와 그 이유 후보
③ 과거와 비교 — 과거 이익 정점 사례 대비 낙폭·PER·이익 방향의 같은 점/다른 점 (숫자로)
④ 내 시각 — 시장과 다른 점 최대 2개. 틀릴 수 있는 지점도 같이
⑤ 시나리오 — 하방/기본/상방(가격이 표보다 낮거나 높은 것도 그대로 해석. 예: 하방 가격대가 현재가보다 높으면 '시장은 최저 추정보다 더 나쁜 이익을 가정'이라고 쓴다): 어떤 조건이면 그 시나리오인지, 사실 블록 표의 가격대만(표가 없으면 '근거 있는 배수가 없어 가격대를 내지 않는다'고 쓴다). 다른 PER 배수를 임의로 쓰지 않는다
⑥ 내 계좌 기준 행동 — 조건부 계획으로(예: "~이하면 ~, ~이면 ~"). 감축이 필요하면 수량·속도를 [내 계좌] 값으로
⑦ 이 판단이 틀렸다는 신호 — 지표와 기준값(숫자). 측정 불가능한 문장 금지
⑧ 지금 데이터로 확인 못 하는 것 — 판단을 가장 크게 바꿀 수 있는 정보 2~3개"""


def _llm(system: str, user: str) -> str:
    """종합 판단에는 더 강한 모델을 쓴다(OpenRouter, 호출 1회 약 5센트). 실패하면 기본 CEO 모델로 폴백."""
    import os
    model = os.getenv("OPENROUTER_MODEL_THESIS", "anthropic/claude-sonnet-5.5")
    try:
        from clients.openai_client import get_fallback_client
        cli = get_fallback_client()
        if cli:
            r = cli.chat.completions.create(model=model, max_tokens=2600, temperature=0.3,
                                            messages=[{"role": "system", "content": system},
                                                      {"role": "user", "content": user}])
            txt = (r.choices[0].message.content or "").strip()
            if txt:
                return txt
    except Exception as e:
        logger.warning("[판단서] %s 호출 실패, 기본 모델로 폴백: %s", model, str(e)[:120])
    from clients.openai_client import chat_ceo
    return chat_ceo(system, user, max_tokens=2200)


def write_thesis(code: str, name: str) -> str:
    facts, _ = build_facts(code, name)
    from clients.openai_client import chat_ceo
    body = chat_ceo(_SYSTEM, f"종목: {name}({code})\n\n[사실]\n{facts}", max_tokens=2200)
    note = ""
    try:
        from services.output_fact_check import check_report_numbers, format_warning
        res = check_report_numbers(body, facts, _SYSTEM)
        note = format_warning(res) if res.get("warn") else ""
    except Exception as ex:
        logger.debug("[판단서] 숫자 대조 생략: %s", ex)
    return f"{body}\n{note}\n\n── 판단에 쓴 사실 ──\n{facts}"
