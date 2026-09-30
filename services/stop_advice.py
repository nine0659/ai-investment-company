"""
services/stop_advice.py — 손절선 도달 시 '결론·행동·수량'이 앞에 오는 권고 메시지 (LLM 없음)

배경(2026-09-30): 기존 위험 알림은 "손절 조건 즉시 확인. 원인 파악 후 대응 결정" 같은
문구로 판단을 전부 사용자에게 넘겼고, check_portfolio_risk는 '하루 -7%' 급락에만
반응해 평단 대비 누적손실이 -15%를 넘어도(현대차 -22%) 울리지 않았다.

정책(사용자 승인, 옵션 B): 손절선(-15%, CEO 헌장의 재검토 의무 기준) 도달 시 절반 정리
권고, 투자 논지가 훼손됐다고 확인되면 전량 정리 권고. 자동매도는 하지 않는다
(2026-07 드로다운 사고 이후 확립 — 권고 메시지까지만).

모든 수치(수익률·수량·금액)는 여기서 코드로 계산한다. 논지 훼손 여부는 코드가 알 수
없으므로 단정하지 않고 '확인 필요'로 명시한다 — 없는 근거를 만들어 확신을 꾸미지 않는다.
"""
from __future__ import annotations

import math

STOP_PCT = -15.0                       # 손절선 (평단 대비 %) — CEO 헌장 재검토 의무 기준
TIERS = (-15.0, -20.0, -25.0, -30.0)   # 재알림 구간 (구간을 새로 넘을 때마다 1회)


def loss_tier(pnl_pct: float) -> float | None:
    """pnl이 넘어선 가장 깊은 구간(예: -22% → -20). 손절선 위면 None."""
    hit = [t for t in TIERS if pnl_pct <= t]
    return min(hit) if hit else None


def build_advice(quantity: int, avg_price: float, price: float,
                 thesis_broken: bool = False, stop_pct: float = STOP_PCT) -> dict | None:
    """평단 대비 손절선을 넘었으면 권고 dict, 아니면 None."""
    if not quantity or quantity <= 0 or not avg_price or not price or avg_price <= 0:
        return None
    pnl = (price - avg_price) / avg_price * 100
    if pnl > stop_pct:
        return None

    sell_qty = quantity if thesis_broken else math.ceil(quantity / 2)
    sell_qty = min(sell_qty, quantity)
    return {
        "pnl_pct": round(pnl, 2),
        "over_pct": round(stop_pct - pnl, 2),          # 손절선을 넘어선 폭(%p)
        "action": "all" if sell_qty == quantity else "half",
        "sell_qty": sell_qty,
        "keep_qty": quantity - sell_qty,
        "proceeds": round(sell_qty * price),
        "realized_loss": round(sell_qty * (price - avg_price)),
        "unrealized_loss": round(quantity * (price - avg_price)),
        # 안 팔고 버텼을 때 추가 -10% 하락 시 늘어나는 손실(현재가 기준 전 수량)
        "further_10pct_loss": round(quantity * price * 0.10),
        "stop_pct": stop_pct,
    }


def format_stop_alert(name: str, code: str, quantity: int, avg_price: float,
                      price: float, advice: dict, thesis_note: str = "") -> str:
    """결론 → 행동 → 근거 → 안 하면 → 재검토 조건 순서의 텔레그램 메시지."""
    a = advice
    if a["action"] == "all":
        act = f"{a['sell_qty']}주 전량 매도"
    else:
        act = f"{quantity}주 중 {a['sell_qty']}주 매도, {a['keep_qty']}주는 유지하며 재검토"
    thesis = thesis_note or "투자 논지 훼손 여부는 자동 판단 불가 — 훼손됐다고 판단되면 전량 정리"
    return (
        f"🛑 *[손절선 도달] {name}({code})  {a['pnl_pct']:+.1f}%*\n\n"
        f"▶ 결론: 손절선({a['stop_pct']:.0f}%)을 {a['over_pct']:.1f}%p 넘어섰습니다. "
        f"규칙상 정리 대상입니다.\n"
        f"▶ 권고: *{act}* (약 {a['proceeds']:,}원 회수)\n\n"
        f"근거\n"
        f"① 평단 {avg_price:,.0f}원 → 현재 {price:,.0f}원 ({a['pnl_pct']:+.1f}%)\n"
        f"② 이번 매도 확정 손익 {a['realized_loss']:+,}원 / 보유분 전체 평가손익 {a['unrealized_loss']:+,}원\n"
        f"③ {thesis}\n\n"
        f"▶ 그대로 두면: 여기서 추가 -10% 하락 시 손실이 {a['further_10pct_loss']:,}원 더 늘어납니다.\n"
        f"▶ 뒤집는 조건: 논지가 훼손되지 않았다는 새 근거(실적·수급)가 확인될 때만 유지.\n\n"
        f"_자동 주문은 나가지 않습니다. 체결 후 `/holdings`로 원장을 갱신하세요._"
    )
