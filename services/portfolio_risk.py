"""포트폴리오 위험 계산기 — '이 계좌가 얼마나 위험한가'를 숫자로 (LLM 아님, 전부 코드 계산).

AI가 "반도체 비중이 과도하다"고만 말하고 정작 "그래서 반도체가 -10%면 내 계좌는 -몇 %"인지
숫자가 없었다. 여기서 비중·섹터 쏠림·종목 간 동반 하락·시나리오 손실을 계산해 CEO 컨텍스트와
/risk 명령에 쓴다. 가격 이력이 없는 종목은 계산에서 빼고 그 사실을 표시한다(만들어내지 않음).
"""
import logging
import math

logger = logging.getLogger(__name__)

TRADING_DAYS = 252
Z95 = 1.645


def load_positions(kis=None) -> list[dict]:
    """보유 종목 + 현재가. 현재가는 KIS(폴백 포함)→네이버 순으로 확보하고 둘이 크게 다르면 네이버를 쓴다."""
    from services.portfolio_service import calculate_pnl
    pnl = calculate_pnl(kis)
    out = []
    for p in pnl:
        price = float(p.get("current_price") or p.get("avg_price") or 0)
        src = p.get("price_label", "")
        try:
            from clients import naver_client as nv
            q = nv.stock_quote(p["code"])
            if q and price and abs(q["price"] - price) / price * 100 > 1.5:
                if "평균단가" not in src:   # 현재가 조회 실패로 평단이 들어온 경우는 정상 대체 — 경고 불필요
                    logger.warning("[위험계산] %s 현재가 %s ≠ 네이버 %s — 네이버 사용", p["code"], price, q["price"])
                price, src = q["price"], "현재가(네이버)"
            elif q and not price:
                price, src = q["price"], "현재가(네이버)"
        except Exception:
            pass
        qty = p.get("quantity") or 0
        out.append({"code": p["code"], "name": p["name"], "qty": qty,
                    "avg": float(p.get("avg_price") or 0), "price": price,
                    "value": price * qty, "sector": p.get("sector") or "기타", "price_src": src})
    return out


def weights(positions: list[dict]) -> dict:
    total = sum(p["value"] for p in positions)
    if total <= 0:
        return {"total": 0, "by_stock": {}, "by_sector": {}, "top1": 0.0, "hhi": 0.0}
    by_stock = {p["code"]: p["value"] / total for p in positions}
    by_sector: dict[str, float] = {}
    for p in positions:
        by_sector[p["sector"]] = by_sector.get(p["sector"], 0) + p["value"] / total
    return {"total": total, "by_stock": by_stock, "by_sector": by_sector,
            "top1": max(by_stock.values()), "hhi": sum(w * w for w in by_stock.values())}


def fetch_returns(codes: list[str], period: str = "6mo") -> dict[str, list[float]]:
    """종목별 일간 수익률(소수). 시장 지수는 키 'KOSPI'. 실패 종목은 키 없음."""
    out: dict[str, list[float]] = {}
    try:
        import yfinance as yf
        for sym, key in [("^KS11", "KOSPI")] + [(f"{c}.KS", c) for c in codes]:
            try:
                h = yf.Ticker(sym).history(period=period, interval="1d")["Close"].dropna()
                if len(h) < 40 and key != "KOSPI":
                    h = yf.Ticker(sym.replace(".KS", ".KQ")).history(period=period, interval="1d")["Close"].dropna()
                if len(h) >= 40:
                    out[key] = {d.strftime("%Y-%m-%d"): float(v) for d, v in h.items()}
            except Exception:
                continue
    except Exception as e:
        logger.debug("[위험계산] 가격 이력 실패: %s", e)
    # 날짜 정렬 후 수익률로 변환(공통 날짜만)
    if "KOSPI" not in out:
        return {}
    dates = sorted(set.intersection(*[set(v) for v in out.values()]))
    rets = {}
    for k, ser in out.items():
        px = [ser[d] for d in dates]
        rets[k] = [px[i] / px[i - 1] - 1 for i in range(1, len(px))]
    return rets


def _cov(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    ma, mb = sum(a[:n]) / n, sum(b[:n]) / n
    return sum((a[i] - ma) * (b[i] - mb) for i in range(n)) / (n - 1)


def risk_metrics(positions: list[dict], rets: dict[str, list[float]]) -> dict:
    """연환산 변동성, 1일 95% VaR(원), 종목별 베타, 계산 제외 종목. rets 없으면 빈 지표."""
    w = weights(positions)
    covered = [p for p in positions if p["code"] in rets]
    missing = [p["name"] for p in positions if p["code"] not in rets]
    if not covered or "KOSPI" not in rets:
        return {"vol_ann": None, "var95": None, "betas": {}, "missing": missing, "covered_weight": 0.0}
    cw = sum(w["by_stock"][p["code"]] for p in covered)
    ws = {p["code"]: w["by_stock"][p["code"]] / cw for p in covered}   # 계산 대상 안에서 재정규화
    var = 0.0
    for a in covered:
        for b in covered:
            var += ws[a["code"]] * ws[b["code"]] * _cov(rets[a["code"]], rets[b["code"]])
    vol_d = math.sqrt(max(var, 0))
    mvar = _cov(rets["KOSPI"], rets["KOSPI"]) or 1e-12
    betas = {p["code"]: _cov(rets[p["code"]], rets["KOSPI"]) / mvar for p in covered}
    covered_value = sum(p["value"] for p in covered)
    return {"vol_ann": vol_d * math.sqrt(TRADING_DAYS) * 100, "var95": Z95 * vol_d * covered_value,
            "betas": betas, "missing": missing, "covered_weight": cw}


def stress(positions: list[dict], betas: dict[str, float]) -> list[dict]:
    """표준 시나리오별 계좌 손익(%·원). 베타 없는 종목은 1.0 가정(표시)."""
    w = weights(positions)
    total = w["total"]
    if total <= 0:
        return []
    out = []
    # 1) 시장 -10% (베타 반영)
    loss = sum(w["by_stock"][p["code"]] * betas.get(p["code"], 1.0) * -10 for p in positions)
    out.append({"name": "KOSPI -10%", "acct_pct": loss, "acct_won": total * loss / 100,
                "note": "베타 미산출 종목은 1.0 가정" if any(p["code"] not in betas for p in positions) else ""})
    # 2) 섹터별 -10% (비중 큰 순 상위 2개)
    for sec, sw in sorted(w["by_sector"].items(), key=lambda x: -x[1])[:2]:
        out.append({"name": f"{sec} 섹터 -10%", "acct_pct": -10 * sw, "acct_won": -total * sw * 0.10, "note": ""})
    # 3) 최대 비중 종목 -20%
    top = max(positions, key=lambda p: p["value"])
    tw = w["by_stock"][top["code"]]
    out.append({"name": f"{top['name']} -20%", "acct_pct": -20 * tw, "acct_won": -total * tw * 0.20, "note": ""})
    return out


def build_report(kis=None, with_history: bool = True) -> dict:
    positions = [p for p in load_positions(kis) if p["value"] > 0]
    if not positions:
        return {"positions": [], "weights": weights([]), "metrics": {}, "stress": []}
    rets = fetch_returns([p["code"] for p in positions]) if with_history else {}
    m = risk_metrics(positions, rets)
    return {"positions": positions, "weights": weights(positions), "metrics": m,
            "stress": stress(positions, m.get("betas", {}))}


def format_report(rep: dict) -> str:
    """사람/LLM이 읽는 요약. 모든 수치는 위 코드 계산값."""
    pos = rep.get("positions") or []
    if not pos:
        return ""
    w, m = rep["weights"], rep.get("metrics") or {}
    lines = [f"[내 계좌 위험 — 코드 계산, 투자금 {w['total']:,.0f}원 기준(현금 제외)]"]
    for p in sorted(pos, key=lambda x: -x["value"]):
        pl = (p["price"] / p["avg"] - 1) * 100 if p["avg"] else 0
        lines.append(f"  {p['name']}: 비중 {w['by_stock'][p['code']]*100:.1f}% · 평단 대비 {pl:+.1f}%")
    sec = ", ".join(f"{k} {v*100:.0f}%" for k, v in sorted(w["by_sector"].items(), key=lambda x: -x[1]))
    lines.append(f"  섹터: {sec} · 최대 종목 비중 {w['top1']*100:.0f}%")
    if m.get("vol_ann") is not None:
        lines.append(f"  연환산 변동성 {m['vol_ann']:.0f}% · 내일 하루 95% 확률로 손실이 {m['var95']:,.0f}원 이내")
        if m.get("missing"):
            lines.append(f"  (가격 이력 없어 계산 제외: {', '.join(m['missing'])})")
    else:
        lines.append("  변동성/VaR: 가격 이력 부족으로 계산 불가")
    for s in rep.get("stress", []):
        lines.append(f"  시나리오 {s['name']} → 계좌 {s['acct_pct']:+.1f}% ({s['acct_won']:+,.0f}원){' ※'+s['note'] if s['note'] else ''}")
    return "\n".join(lines)
