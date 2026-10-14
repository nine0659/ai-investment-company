"""판단 습관 점검 — 사용자가 원장을 바꾸는 순간 감정적 결정의 전형 패턴을 짧게 되묻는다.

투자 손실의 상당수는 종목 선택이 아니라 행동 편향(물타기, 공황 매도, 쏠림)에서 온다. 막지 않고
(결정은 사용자 몫) 사실 숫자와 함께 한두 줄 질문만 한다. 판단은 전부 코드, 문구는 고정.
"""
AVG_DOWN_LOSS_PCT = -15.0     # 이만큼 손실 중인 종목을 더 사면 '물타기'
PANIC_DROP_PCT = -7.0         # 이 이상 빠진 날/구간에 파는 것은 '공황 구간 매도'
CONCENTRATION_PCT = 50.0      # 한 섹터 비중 경고선


def check_buy(position: dict | None, buy_price: float, buy_qty: int,
              sector_weights_after: dict[str, float] | None = None, sector: str = "") -> list[str]:
    """position: 기존 보유 {name, qty, avg} 또는 None(신규)."""
    out = []
    if position and position.get("avg") and position.get("qty"):
        pl = (buy_price / position["avg"] - 1) * 100
        if pl <= AVG_DOWN_LOSS_PCT:
            new_avg = (position["qty"] * position["avg"] + buy_qty * buy_price) / (position["qty"] + buy_qty)
            out.append(f"⚠️ 물타기 확인: {position['name']}은 평단 대비 {pl:+.1f}% 손실 중입니다. "
                       f"추가 매수로 평단은 {position['avg']:,.0f}→{new_avg:,.0f}원이 되지만 손실이 사라지는 건 아닙니다. "
                       f"'하락 이유가 일시적'이라는 근거와 반증 신호(미발동)를 확인하셨나요?")
    if sector_weights_after and sector and sector_weights_after.get(sector, 0) * 100 >= CONCENTRATION_PCT:
        out.append(f"⚠️ 쏠림 확인: 이 매수 후 {sector} 섹터가 계좌의 {sector_weights_after[sector]*100:.0f}%입니다. "
                   f"같은 이유로 같이 흔들리는 종목에 몰려 있는지 점검해 보세요.")
    return out


def check_sell(position: dict | None, sell_price: float, market_chg: float | None = None,
               recent_drop_pct: float | None = None, sell_all: bool = False) -> list[str]:
    out = []
    if not position:
        return out
    pl = (sell_price / position["avg"] - 1) * 100 if position.get("avg") else 0.0
    panic = (market_chg is not None and market_chg <= -3) or (recent_drop_pct is not None and recent_drop_pct <= PANIC_DROP_PCT)
    if panic:
        out.append(f"⚠️ 공황 구간 매도 확인: 지금은 시장/종목이 크게 빠진 구간입니다"
                   f"({'시장 '+format(market_chg,'+.1f')+'%' if market_chg is not None else ''}"
                   f"{' · 최근 '+format(recent_drop_pct,'+.1f')+'%' if recent_drop_pct is not None else ''}). "
                   f"이 매도는 사전에 정한 반증 신호(종가선·평단 -15%) 때문인가요, 아니면 불안 때문인가요? "
                   f"플레이북 기본 규칙: 급락 당일 공황매도 금지·분할 실행.")
    if sell_all and pl <= -20:
        out.append(f"ℹ️ 손실 확정 확인: {position['name']} 전량 매도는 평단 대비 {pl:+.1f}% 손실을 확정합니다. "
                   f"일부(예: 절반)만 정리하고 나머지는 반증 신호를 기준으로 두는 선택지도 있습니다.")
    return out
