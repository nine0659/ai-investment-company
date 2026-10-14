"""'만약' 시나리오 엔진 — "하이닉스를 20% 팔면 / 안 팔면"을 가격 경로별로 계산 (LLM 아님).

의사결정은 '맞고 틀림'이 아니라 '틀렸을 때 얼마를 잃고 맞았을 때 얼마를 놓치는가'의 비교다.
매도 비용은 아래 가정치이며 증권사·세법에 따라 다르다 — 출력에도 '가정'이라고 표시한다.
"""
SELL_TAX_STOCK = 0.0020   # 증권거래세+농특세 가정(개별주). 실제 세율은 연도별로 확인 필요
SELL_TAX_ETF = 0.0        # 국내 상장 ETF는 거래세 면제
FEE = 0.00015             # 증권사 수수료 가정 (매도 시)
DEFAULT_MONTH_SIGMA = 0.10


def month_sigma(daily_returns: list[float] | None) -> float:
    """21거래일 변동폭(1σ). 이력이 없으면 기본값(표시용)."""
    if daily_returns and len(daily_returns) >= 40:
        n = len(daily_returns)
        m = sum(daily_returns) / n
        sd = (sum((r - m) ** 2 for r in daily_returns) / (n - 1)) ** 0.5
        return sd * (21 ** 0.5)
    return DEFAULT_MONTH_SIGMA


def what_if_sell(position: dict, sell_pct: float, sigma: float | None = None,
                 account_total: float | None = None, is_etf: bool = False) -> dict:
    """position: {name, qty, avg, price}. sell_pct: 보유 수량 중 파는 비율(%).
    경로: 한 달 뒤 가격이 -2σ/-1σ/0/+1σ/+2σ일 때 '지금 판다'와 '그대로 둔다'의 차이."""
    qty, avg, price = position["qty"], position["avg"], position["price"]
    sell_qty = int(qty * sell_pct / 100)
    if sell_qty <= 0 or price <= 0:
        return {"ok": False, "reason": "매도 수량이 0주이거나 현재가 없음"}
    sigma = sigma if sigma is not None else DEFAULT_MONTH_SIGMA
    tax = SELL_TAX_ETF if is_etf else SELL_TAX_STOCK
    cost = sell_qty * price * (tax + FEE)
    realized = sell_qty * (price - avg) - cost
    rows = []
    for k in (-2, -1, 0, 1, 2):
        path_price = price * (1 + k * sigma)
        # 지금 팔면 남는 이득 = 하락 방어분(-) 또는 상승 놓친 분(+):  판 수량 × (path - price)를 안 받음
        avoided = -sell_qty * (path_price - price)       # 팔았기에 피한 손실(+)/놓친 이익(-)
        net = avoided - cost
        rows.append({"k": k, "price": path_price, "net_vs_hold": net,
                     "acct_pct": (net / account_total * 100) if account_total else None})
    return {"ok": True, "name": position["name"], "sell_qty": sell_qty, "keep_qty": qty - sell_qty,
            "price": price, "realized": realized, "cost": cost, "sigma": sigma, "rows": rows,
            "tax_note": "ETF 거래세 면제 가정" if is_etf else f"거래세·농특세 {tax*100:.2f}% + 수수료 {FEE*100:.3f}% 가정(실제와 다를 수 있음)"}


def format_what_if(r: dict) -> str:
    if not r.get("ok"):
        return f"계산 불가: {r.get('reason')}"
    lines = [f"🔎 {r['name']} {r['sell_qty']}주 매도(남기는 수량 {r['keep_qty']}주) 가정 — 한 달 뒤 가격 시나리오 (1σ=±{r['sigma']*100:.0f}%)",
             f"  지금 판 실현손익 {r['realized']:+,.0f}원 (비용 {r['cost']:,.0f}원 포함) · {r['tax_note']}",
             "  한 달 뒤 가격 → '팔기'가 '그대로 두기'보다 나은 금액:"]
    label = {-2: "-2σ", -1: "-1σ", 0: "변화없음", 1: "+1σ", 2: "+2σ"}
    for x in r["rows"]:
        acc = f" (계좌 {x['acct_pct']:+.1f}%)" if x["acct_pct"] is not None else ""
        lines.append(f"   {label[x['k']]:>5} {x['price']:>12,.0f}원: {x['net_vs_hold']:+,.0f}원{acc}")
    lines.append("  ※ 양수=판 게 이득, 음수=그대로 둘 걸 후회. 미래 가격 예측이 아니라 범위 비교입니다.")
    return "\n".join(lines)
